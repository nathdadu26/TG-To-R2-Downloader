import os
import time
import math
import hashlib
import asyncio
import logging
import boto3

# Naye Python versions (3.12+) me asyncio.get_event_loop() ab loop khud-se create
# nahi karta agar koi loop already set/running na ho — pyrogram apne import ke
# time hi (pyrogram/sync.py) yeh call kar deta hai, isliye pyrogram import se
# PEHLE hi explicitly ek event loop create karke set kar dete hain.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client, filters
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import FloodWaitError
from motor.motor_asyncio import AsyncIOMotorClient

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Environment Variables Load Karein (.env file se)
load_dotenv()

# --- CONFIGURATION ---
API_ID = int(os.getenv("API_ID", "1234567"))
API_HASH = os.getenv("API_HASH", "your_api_hash")
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token")
STRING_SESSION = os.getenv("STRING_SESSION", "your_telethon_string_session")

# MongoDB Setup
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://username:password@cluster.mongodb.net/")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")

# Cloudflare R2 Credentials
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "your_account_id")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "your_access_key")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "your_secret_key")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "your_bucket_name")
R2_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
FOLDER_NAME = "telegram files"

# MINIMUM VIDEO DURATION (in seconds)
MIN_VIDEO_DURATION = 10

# MAXIMUM VIDEO SIZE (in MB) — isse bade videos skip ho jayenge (jaise duration wala check)
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "200"))

# Isse bada (aur MAX_VIDEO_SIZE_MB tak) koi bhi video "bada file" mana jayega
# aur usse akele (1 at a time) download kiya jayega. Isse chhote videos
# CONCURRENT_DOWNLOADS ki limit ke saath parallel chalte hain.
LARGE_FILE_THRESHOLD_MB = int(os.getenv("LARGE_FILE_THRESHOLD_MB", "100"))

# Bulk /download me ek saath kitne (chhoti) videos parallel download+upload honge
CONCURRENT_DOWNLOADS = int(os.getenv("CONCURRENT_DOWNLOADS", "5"))

# Render (or any host) requires a web port to be bound so the free-tier
# health checks / keep-alive pings have something to hit.
PORT = int(os.getenv("PORT", "8080"))

# --- CLIENT INITIALIZATIONS ---

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
hashes_collection = db["video_hashes"]

s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)

bot = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
userbot = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

# --- HELPER FUNCTIONS ---

def get_file_hash(file_path):
    """File ka SHA-256 hash calculate karta hai."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

async def is_duplicate(file_hash):
    result = await hashes_collection.find_one({"hash": file_hash})
    return result is not None

async def save_hash(file_hash, filename):
    try:
        await hashes_collection.insert_one({"hash": file_hash, "file_name": filename})
    except Exception as e:
        logger.error(f"MongoDB Insert Error: {e}")

def get_video_duration(msg):
    """Telethon Message se Video ki Duration (in seconds) safely extract karta hai."""
    if not msg or not msg.media:
        return None

    # Direct Video Media
    if getattr(msg, "video", None):
        for attr in getattr(msg.video, "attributes", []):
            if hasattr(attr, "duration"):
                return attr.duration
        if hasattr(msg.video, "duration"):
            return msg.video.duration

    # Document / File formatted Video
    if getattr(msg, "document", None):
        for attr in getattr(msg.document, "attributes", []):
            if attr.__class__.__name__ == "DocumentAttributeVideo":
                return getattr(attr, "duration", 0)

    return None

def get_video_size_mb(msg):
    """Telethon Message se video/document ka size (MB me) safely extract karta hai."""
    try:
        if getattr(msg, "file", None) and msg.file.size:
            return msg.file.size / (1024 * 1024)
    except Exception:
        pass

    # Fallback: raw document/video object se size nikalna
    if getattr(msg, "document", None) and getattr(msg.document, "size", None):
        return msg.document.size / (1024 * 1024)
    if getattr(msg, "video", None) and getattr(msg.video, "size", None):
        return msg.video.size / (1024 * 1024)

    return None

def is_valid_video(msg):
    """Check karta hai ki message valid video hai, duration >= 10s ho, aur size MAX_VIDEO_SIZE_MB se zyada na ho."""
    duration = get_video_duration(msg)
    if duration is None or duration < MIN_VIDEO_DURATION:
        return False

    size_mb = get_video_size_mb(msg)
    if size_mb is not None and size_mb > MAX_VIDEO_SIZE_MB:
        return False

    return True

def create_progress_bar(current, total):
    """Progress Bar visualization."""
    percentage = current / total if total > 0 else 0
    filled_length = int(10 * percentage)
    bar = "█" * filled_length + "░" * (10 - filled_length)
    return f"[{bar}] {percentage * 100:.1f}%"

def time_formatter(seconds):
    """Seconds ko readable format me convert karta hai."""
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

class RateLimitedStatusUpdater:
    """Telegram message update rate-limiter (Every 10s)."""
    def __init__(self, message, update_interval=10):
        self.message = message
        self.interval = update_interval
        self.last_update = 0

    async def update(self, text, force=False):
        now = time.time()
        if force or (now - self.last_update >= self.interval):
            try:
                await self.message.edit_text(text)
                self.last_update = now
            except Exception as e:
                logger.warning(f"Failed to update status message: {e}")

def upload_to_r2_with_progress(file_path, object_name, status_updater, base_text, loop):
    """R2 upload with progress callback and rate limiting."""
    file_size = os.path.getsize(file_path)
    uploaded = 0
    start_time = time.time()

    def progress_callback(bytes_amount):
        nonlocal uploaded
        uploaded += bytes_amount
        elapsed = time.time() - start_time
        speed = uploaded / elapsed if elapsed > 0 else 1
        eta = (file_size - uploaded) / speed if speed > 0 else 0
        
        progress_str = create_progress_bar(uploaded, file_size)
        text = (
            f"{base_text}\n\n"
            f"☁️ **Uploading to R2:**\n"
            f"{progress_str}\n"
            f"⚡ **Speed:** {uploaded / (1024 * 1024 * elapsed):.2f} MB/s\n"
            f"⏳ **ETA:** {time_formatter(eta)}"
        )
        asyncio.run_coroutine_threadsafe(status_updater.update(text), loop)

    try:
        s3_client.upload_file(file_path, R2_BUCKET_NAME, object_name, Callback=progress_callback)
        return True
    except Exception as e:
        logger.error(f"R2 Upload Error: {e}")
        return False

def upload_to_r2_silent(file_path, object_name):
    """Bulk task ke liye simple upload, bina per-byte progress update ke (rate limit bachane ke liye)."""
    try:
        s3_client.upload_file(file_path, R2_BUCKET_NAME, object_name)
        return True
    except Exception as e:
        logger.error(f"R2 Upload Error: {e}")
        return False

def build_bulk_progress_text(channel_name, processed, total, uploaded, skipped, failed, eta_seconds):
    """Bulk task ka ek single combined overall-progress message banata hai."""
    progress_str = create_progress_bar(processed, total)
    eta_text = time_formatter(eta_seconds) if processed > 0 else "Calculating..."
    return (
        f"⬇️ **Downloading From {channel_name}**\n\n"
        f"📦 **Total Videos Available:** `{processed}/{total}`\n"
        f"✅ **Uploaded to R2:** `{uploaded}`\n"
        f"⚠️ **Duplicates Skipped:** `{skipped}`\n"
        f"❌ **Failed Uploads:** `{failed}`\n"
        f"⏰ **ETA:** {eta_text}\n"
        f"{progress_str}"
    )

def build_bulk_final_report(channel_name, total, uploaded, skipped, failed):
    """Bulk task complete hone ke baad ka final summary."""
    return (
        "🎉 **Channel Download Task Completed!**\n\n"
        f"✉️ **Name:** {channel_name}\n"
        f"📦 **Total Videos Processed:** `{total}`\n"
        f"✅ **Uploaded to R2:** `{uploaded}`\n"
        f"⚠️ **Duplicates Skipped:** `{skipped}`\n"
        f"❌ **Failed Uploads:** `{failed}`"
    )

# --- BOT HANDLERS ---

@bot.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    welcome_text = (
        f"👋 **Hello {message.from_user.first_name}!**\n\n"
        "Main aapka **Video Downloader & Cloud Uploader Bot** hu.\n\n"
        "✨ **Features:**\n"
        "• Single Video / Bulk Channel Download\n"
        f"• Skipped videos < {MIN_VIDEO_DURATION} seconds duration\n"
        f"• Skipped videos > {MAX_VIDEO_SIZE_MB}MB size\n"
        f"• Chhoti files {CONCURRENT_DOWNLOADS}x parallel, badi files (>= {LARGE_FILE_THRESHOLD_MB}MB) ek-ek karke\n"
        "• Single Progress Message (auto-update)\n"
        "• Duplicate Detection via SHA-256 Hash\n\n"
        "Usage: `/download {link_ya_id}`"
    )
    await message.reply_text(welcome_text)

@bot.on_message(filters.video & filters.private)
async def handle_direct_video(client, message):
    # Check Video Duration
    if message.video.duration and message.video.duration < MIN_VIDEO_DURATION:
        await message.reply_text(f"⚠️ Video skipped: Duration is less than {MIN_VIDEO_DURATION} seconds.")
        return

    status_msg = await message.reply_text("📥 Starting download...")
    updater = RateLimitedStatusUpdater(status_msg, update_interval=10)
    
    start_time = time.time()
    
    async def pyrogram_progress(current, total):
        elapsed = time.time() - start_time
        speed = current / elapsed if elapsed > 0 else 1
        eta = (total - current) / speed if speed > 0 else 0
        
        progress_str = create_progress_bar(current, total)
        text = (
            "📥 **Downloading Single Video...**\n"
            f"{progress_str}\n"
            f"⚡ **Speed:** {current / (1024 * 1024 * elapsed):.2f} MB/s\n"
            f"⏳ **ETA:** {time_formatter(eta)}"
        )
        await updater.update(text)

    file_path = await client.download_media(message, progress=pyrogram_progress)
    filename = os.path.basename(file_path)
    file_hash = get_file_hash(file_path)

    if await is_duplicate(file_hash):
        await updater.update("⚠️ **Duplicate Video!** Skipping upload.", force=True)
        if os.path.exists(file_path):
            os.remove(file_path)
        return

    r2_key = f"{FOLDER_NAME}/{filename}"
    loop = asyncio.get_event_loop()
    
    success = await loop.run_in_executor(
        None, upload_to_r2_with_progress, file_path, r2_key, updater, "📤 Processing Upload...", loop
    )

    if success:
        await save_hash(file_hash, filename)
        await updater.update("✅ **Successfully Uploaded to Cloudflare R2!**", force=True)
    else:
        await updater.update("❌ **R2 Upload Failed!**", force=True)

    if os.path.exists(file_path):
        os.remove(file_path)


@bot.on_message(filters.command("download") & filters.private)
async def handle_download_command(client, message):
    if len(message.command) < 2:
        await message.reply_text("❌ **Usage:** `/download {Link ya Channel ID/Username}`")
        return

    target = message.command[1]
    status_msg = await message.reply_text("🔄 Processing request...")
    # Interval badha diya taaki bulk task me Telegram edit calls kam lagein aur rate limit na aaye
    updater = RateLimitedStatusUpdater(status_msg, update_interval=15)

    try:
        if "t.me/" in target or target.startswith("+"):
            await updater.update("🔗 Joining channel/group via Userbot...", force=True)
            try:
                await userbot(JoinChannelRequest(target))
            except Exception as e:
                logger.info(f"Join Notice: {e}")

        entity = await userbot.get_entity(target)
        channel_name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(target)
        await updater.update("🔍 Scanning channel for eligible videos...", force=True)

        # 1. Safe video filtering with Document/Video Duration & Size Check
        valid_messages = []
        async for msg in userbot.iter_messages(entity):
            if is_valid_video(msg):
                valid_messages.append(msg)

        total_videos = len(valid_messages)
        if total_videos == 0:
            await updater.update(
                f"⚠️ No eligible videos found (duration >= {MIN_VIDEO_DURATION}s and size <= {MAX_VIDEO_SIZE_MB}MB).",
                force=True,
            )
            return

        # Size ke hisaab se do groups banao: badi files akele download hongi,
        # chhoti files CONCURRENT_DOWNLOADS ki limit ke saath parallel
        large_messages = []
        small_messages = []
        for msg in valid_messages:
            size_mb = get_video_size_mb(msg)
            if size_mb is not None and size_mb >= LARGE_FILE_THRESHOLD_MB:
                large_messages.append(msg)
            else:
                small_messages.append(msg)

        logger.info(
            f"Found {total_videos} eligible videos "
            f"({len(small_messages)} small @ {CONCURRENT_DOWNLOADS}x parallel, "
            f"{len(large_messages)} large @ 1x sequential)."
        )

        # Shared state — sab parallel workers isi ko update karte hain (lock se protected)
        counters = {"processed": 0, "uploaded": 0, "skipped": 0, "failed": 0}
        lock = asyncio.Lock()
        semaphore_small = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
        semaphore_large = asyncio.Semaphore(1)
        loop = asyncio.get_event_loop()
        task_start_time = time.time()

        async def process_single_video(msg, semaphore):
            async with semaphore:
                try:
                    file_path = await userbot.download_media(msg)

                    if not file_path:
                        async with lock:
                            counters["failed"] += 1
                    else:
                        filename = f"vid_{msg.id}_{os.path.basename(file_path)}"
                        file_hash = get_file_hash(file_path)

                        if await is_duplicate(file_hash):
                            async with lock:
                                counters["skipped"] += 1
                            logger.info(f"Skipped duplicate video ID {msg.id}")
                        else:
                            r2_key = f"{FOLDER_NAME}/{filename}"
                            success = await loop.run_in_executor(
                                None, upload_to_r2_silent, file_path, r2_key
                            )

                            if success:
                                await save_hash(file_hash, filename)
                                async with lock:
                                    counters["uploaded"] += 1
                                logger.info(f"Uploaded video ID {msg.id} to R2.")
                            else:
                                async with lock:
                                    counters["failed"] += 1
                                logger.error(f"Failed uploading video ID {msg.id}")

                        if os.path.exists(file_path):
                            os.remove(file_path)

                except FloodWaitError as e:
                    logger.warning(f"FloodWait: sleeping {e.seconds}s (video ID {msg.id})")
                    await asyncio.sleep(e.seconds)
                    async with lock:
                        counters["failed"] += 1
                except Exception as e:
                    logger.error(f"Error processing video ID {msg.id}: {e}", exc_info=True)
                    async with lock:
                        counters["failed"] += 1
                finally:
                    async with lock:
                        counters["processed"] += 1
                        processed = counters["processed"]
                        uploaded = counters["uploaded"]
                        skipped = counters["skipped"]
                        failed = counters["failed"]

                    elapsed = time.time() - task_start_time
                    avg_per_video = elapsed / processed if processed else 0
                    # CONCURRENT_DOWNLOADS videos ek saath chal rahe hain, isliye ETA me
                    # concurrency factor bhi divide karte hain taaki andaza sahi rahe
                    remaining = total_videos - processed
                    eta_seconds = (avg_per_video * remaining) / CONCURRENT_DOWNLOADS

                    status_text = build_bulk_progress_text(
                        channel_name, processed, total_videos, uploaded, skipped, failed, eta_seconds
                    )
                    # force=False -> RateLimitedStatusUpdater khud decide karega kab actually edit bhejni hai
                    await updater.update(status_text)

        # 2. Chhoti files 5-parallel me, badi files ek-ek karke — dono groups ek saath launch
        #    hote hain, har group apni semaphore limit follow karta hai
        tasks = (
            [process_single_video(msg, semaphore_small) for msg in small_messages]
            + [process_single_video(msg, semaphore_large) for msg in large_messages]
        )
        await asyncio.gather(*tasks)

        final_report = build_bulk_final_report(
            channel_name, total_videos, counters["uploaded"], counters["skipped"], counters["failed"]
        )
        await updater.update(final_report, force=True)

    except Exception as e:
        logger.error(f"Command Error: {e}", exc_info=True)
        await updater.update(f"❌ **Error:** `{str(e)}`", force=True)


# --- TINY WEB SERVER (Render free-tier ke liye) ---
# Render ka free plan sirf Web Services par milta hai jo ek PORT par sunte hain.
# Yeh chhota server sirf ek health-check route deta hai jise UptimeRobot jaisi
# service har 10-14 minute me ping karke bot ko so-ne (sleep) se rokegi.
async def handle_health(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Health-check web server started on port {PORT}")

# --- START BOT ---
async def main():
    await hashes_collection.create_index("hash", unique=True)
    await start_web_server()
    await userbot.start()
    await bot.start()
    logger.info("🚀 Bot is running with Progress Tracker, Rate Limiters & Logger!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

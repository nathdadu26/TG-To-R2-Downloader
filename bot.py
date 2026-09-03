import os
import time
import math
import hashlib
import asyncio
import logging
import boto3
from dotenv import load_dotenv
from pyrogram import Client, filters
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
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

def is_valid_video(msg):
    """Check karta hai ki message valid video hai ya nahi aur >= 10 seconds hai ya nahi."""
    duration = get_video_duration(msg)
    if duration is not None and duration >= MIN_VIDEO_DURATION:
        return True
    return False

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
        "• Skipped videos < 10 seconds duration\n"
        "• Single Progress Message (10s auto-update)\n"
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

        # 1. Safe video filtering with Document/Video Duration Check
        valid_messages = []
        async for msg in userbot.iter_messages(entity):
            if is_valid_video(msg):
                valid_messages.append(msg)

        total_videos = len(valid_messages)
        if total_videos == 0:
            await updater.update("⚠️ No videos longer than 10 seconds found.", force=True)
            return

        logger.info(f"Found {total_videos} eligible videos to process.")

        uploaded_count = 0
        skipped_count = 0
        failed_count = 0
        loop = asyncio.get_event_loop()
        task_start_time = time.time()

        # 2. Iterate and process each video — sirf ek combined status message update hota hai (rate-limited)
        for idx, msg in enumerate(valid_messages, 1):
            file_path = await userbot.download_media(msg)

            if not file_path:
                failed_count += 1
            else:
                filename = f"vid_{msg.id}_{os.path.basename(file_path)}"
                file_hash = get_file_hash(file_path)

                if await is_duplicate(file_hash):
                    skipped_count += 1
                    logger.info(f"Skipped duplicate video ID {msg.id}")
                else:
                    r2_key = f"{FOLDER_NAME}/{filename}"
                    success = await loop.run_in_executor(
                        None, upload_to_r2_silent, file_path, r2_key
                    )

                    if success:
                        await save_hash(file_hash, filename)
                        uploaded_count += 1
                        logger.info(f"Uploaded video ID {msg.id} to R2.")
                    else:
                        failed_count += 1
                        logger.error(f"Failed uploading video ID {msg.id}")

                if os.path.exists(file_path):
                    os.remove(file_path)

            # ETA: ab tak ke average time per video se remaining videos ka andaza
            elapsed = time.time() - task_start_time
            avg_per_video = elapsed / idx
            eta_seconds = avg_per_video * (total_videos - idx)

            status_text = build_bulk_progress_text(
                channel_name, idx, total_videos, uploaded_count, skipped_count, failed_count, eta_seconds
            )
            # force=False -> RateLimitedStatusUpdater khud decide karega kab actually Telegram par edit bhejna hai
            await updater.update(status_text)

        final_report = build_bulk_final_report(
            channel_name, total_videos, uploaded_count, skipped_count, failed_count
        )
        await updater.update(final_report, force=True)

    except Exception as e:
        logger.error(f"Command Error: {e}", exc_info=True)
        await updater.update(f"❌ **Error:** `{str(e)}`", force=True)

# --- START BOT ---
async def main():
    await hashes_collection.create_index("hash", unique=True)
    await userbot.start()
    await bot.start()
    logger.info("🚀 Bot is running with Progress Tracker, Rate Limiters & Logger!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

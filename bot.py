import os
import asyncio
from datetime import datetime
import discord
from discord.ext import tasks
from aiohttp import web

from config import (
    DISCORD_TOKEN,
    DISCORD_USER_ID,
    GEMINI_REQUEST_DELAY,
    MAX_MESSAGE_CHARS,
    REPORT_HOUR,
    REPORT_MINUTE,
    IST,
    logger,
    validate_environment
)
from database import (
    init_database,
    get_message_status,
    set_message_status,
    save_job,
    get_bot_state,
    set_bot_state,
    get_stats
)
from gemini_service import analyze_job_with_retry
from report import create_daily_report

# ==========================================
# DISCORD INTENTS & CLIENT
# ==========================================
intents = discord.Intents.default()
intents.message_content = True

discord_client = discord.Client(intents=intents)

# ==========================================
# ASYNC QUEUE & TASK REFERENCES
# ==========================================
message_queue = asyncio.Queue()
message_worker_task = None

# ==========================================
# HEALTH CHECK HTTP SERVER (FOR RENDER FREE TIER)
# ==========================================
async def handle_health_check(request):
    """Responds to Render health checks to keep Web Service active on free tier."""
    return web.Response(text="DS-Job-Tracker is alive and running!")

async def start_health_server():
    """Starts a lightweight HTTP server on $PORT for Render compatibility."""
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"HEALTH_SERVER: HTTP health check listening on port {port}")

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def extract_message_content(message: discord.Message) -> str:
    """Extracts text content from message body, embeds, and attachment filenames/URLs."""
    parts = []

    if message.content and message.content.strip():
        parts.append(message.content.strip())

    # Extract embed information
    for embed in message.embeds:
        if embed.title:
            parts.append(f"Embed Title: {embed.title}")
        if embed.description:
            parts.append(f"Embed Description: {embed.description}")
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")

    # Extract attachment information
    for attachment in message.attachments:
        if attachment.filename:
            parts.append(f"Attachment Filename: {attachment.filename}")
        if attachment.url:
            parts.append(f"Attachment URL: {attachment.url}")

    full_content = "\n".join(parts).strip()

    if len(full_content) > MAX_MESSAGE_CHARS:
        logger.warning(f"Message {message.id} exceeded max length ({len(full_content)} chars). Truncating.")
        full_content = full_content[:MAX_MESSAGE_CHARS] + "\n[Content truncated due to MAX_MESSAGE_CHARS limit]"

    return full_content

async def send_long_message(destination, text: str):
    """Splits long text into valid Discord chunks (<= 1900 chars) and sends sequentially."""
    if not text:
        return

    MAX_CHUNK_SIZE = 1900
    if len(text) <= MAX_CHUNK_SIZE:
        await destination.send(text)
        return

    lines = text.split("\n")
    current_chunk = []
    current_length = 0

    for line in lines:
        if current_length + len(line) + 1 > MAX_CHUNK_SIZE:
            chunk_str = "\n".join(current_chunk)
            if chunk_str.strip():
                await destination.send(chunk_str)
            current_chunk = [line]
            current_length = len(line) + 1
        else:
            current_chunk.append(line)
            current_length += len(line) + 1

    if current_chunk:
        chunk_str = "\n".join(current_chunk)
        if chunk_str.strip():
            await destination.send(chunk_str)

# ==========================================
# BACKGROUND MESSAGE WORKER
# ==========================================
async def message_worker():
    """Sequential worker pulling messages from message_queue and invoking Gemini analysis."""
    logger.info("MESSAGE_WORKER: Single background worker started.")
    while True:
        try:
            item = await message_queue.get()
            msg_id = item["id"]
            content = item["content"]
            channel_id = item["channel_id"]
            guild_id = item["guild_id"]

            logger.info(f"MESSAGE_PROCESSING: Worker picked up message ID: {msg_id}")
            set_message_status(msg_id, "PROCESSING", channel_id, guild_id)

            try:
                # Synchronous Gemini call executed safely in thread pool
                result = await asyncio.to_thread(analyze_job_with_retry, content)

                if result.is_job and result.jobs:
                    for job in result.jobs:
                        save_job(
                            job=job,
                            company=result.company,
                            organizer=result.organizer,
                            original_post=content,
                            discord_message_id=msg_id
                        )
                        logger.info(f"JOB_SAVED: {job.title} | Company: {result.company} | Score: {job.relevance_score}/100")
                else:
                    logger.info(f"MESSAGE_COMPLETED: Message {msg_id} was identified as non-job and ignored.")

                set_message_status(msg_id, "COMPLETED", channel_id, guild_id)

            except Exception as e:
                logger.error(f"MESSAGE_FAILED: Processing message {msg_id} failed: {e}")
                set_message_status(msg_id, "FAILED", channel_id, guild_id, error=str(e))

            finally:
                message_queue.task_done()
                await asyncio.sleep(GEMINI_REQUEST_DELAY)

        except asyncio.CancelledError:
            logger.info("MESSAGE_WORKER: Task cancellation received. Exiting loop.")
            break
        except Exception as e:
            logger.error(f"MESSAGE_WORKER: Unexpected error in worker loop: {e}")
            await asyncio.sleep(2.0)

# ==========================================
# DAILY REPORT SCHEDULER TASK
# ==========================================
@tasks.loop(minutes=1)
async def daily_report_task():
    """Checks daily report schedule persistently using SQLite state and IST time."""
    try:
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")

        last_sent = get_bot_state("daily_report_last_sent")

        time_matches = (now.hour == REPORT_HOUR and now.minute == REPORT_MINUTE)
        past_report_time_today = (now.hour > REPORT_HOUR) or (now.hour == REPORT_HOUR and now.minute >= REPORT_MINUTE)

        should_send = False
        if last_sent != today_str:
            if time_matches or past_report_time_today:
                should_send = True

        if should_send:
            logger.info(f"REPORT_GENERATING: Triggering daily report for {today_str}...")
            user = await discord_client.fetch_user(DISCORD_USER_ID)
            report_text = create_daily_report(today_str)
            await send_long_message(user, report_text)
            set_bot_state("daily_report_last_sent", today_str)
            logger.info("REPORT_SENT: Daily report successfully delivered to user DM.")

    except Exception as e:
        logger.error(f"Error executing daily_report_task loop: {e}")

# ==========================================
# DISCORD EVENT HANDLERS
# ==========================================
@discord_client.event
async def on_ready():
    """Event triggered on Discord connection and reconnects."""
    global message_worker_task
    init_database()

    logger.info(f"BOT_READY: Logged in as {discord_client.user} (ID: {discord_client.user.id})")

    # Start worker safely (guaranteed single worker across reconnects)
    if message_worker_task is None or message_worker_task.done():
        message_worker_task = asyncio.create_task(message_worker())
        logger.info("Worker task initialized successfully.")

    # Start daily report scheduler safely
    if not daily_report_task.is_running():
        daily_report_task.start()
        logger.info("Daily report scheduler loop started.")

@discord_client.event
async def on_message(message: discord.Message):
    """Event handler for incoming Discord messages."""
    # Ignore messages sent by the bot itself
    if message.author == discord_client.user:
        return

    content = extract_message_content(message)
    if not content:
        return

    # Command Handling: !report
    if content.lower().strip() == "!report":
        logger.info(f"COMMAND_RECEIVED: !report requested by {message.author}")
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        report_text = create_daily_report(today_str)
        await send_long_message(message.author, report_text)
        return

    # Command Handling: !status
    if content.lower().strip() == "!status":
        logger.info(f"COMMAND_RECEIVED: !status requested by {message.author}")
        stats = get_stats()
        worker_state = "Running" if (message_worker_task and not message_worker_task.done()) else "Stopped"
        status_msg = (
            f"🤖 **BOT STATUS REPORT**\n"
            f"• Status: `Online`\n"
            f"• Worker Task: `{worker_state}`\n"
            f"• Queue Backlog: `{message_queue.qsize()}` messages\n"
            f"• Total DB Jobs: `{stats['total_jobs']}`\n"
            f"• Today's Job Roles: `{stats['todays_jobs']}`\n"
            f"• Processed Messages: `{stats['processed_messages']}`\n"
            f"• Last Daily Report: `{stats['last_report_date']}`"
        )
        await send_long_message(message.channel, status_msg)
        return

    # Message-level Idempotency Check
    existing_status = get_message_status(message.id)
    if existing_status in ["COMPLETED", "PROCESSING", "QUEUED"]:
        logger.info(f"DUPLICATE_SKIPPED: Message {message.id} already has status '{existing_status}'.")
        return

    # Mark QUEUED and push to asyncio.Queue
    set_message_status(
        discord_message_id=message.id,
        status="QUEUED",
        channel_id=message.channel.id,
        guild_id=message.guild.id if message.guild else None
    )

    await message_queue.put({
        "id": str(message.id),
        "content": content,
        "channel_id": str(message.channel.id),
        "guild_id": str(message.guild.id) if message.guild else None
    })

    logger.info(f"MESSAGE_QUEUED: Message {message.id} added to queue. Current Queue Size: {message_queue.qsize()}")

# ==========================================
# MAIN APPLICATION ENTRY POINT
# ==========================================
async def main():
    validate_environment()
    init_database()
    logger.info("BOT_START: Launching DS Job Tracker production service...")

    # Start lightweight HTTP server for Render Free Web Service compatibility
    await start_health_server()

    try:
        async with discord_client:
            await discord_client.start(DISCORD_TOKEN)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown signal received.")
    finally:
        if message_worker_task and not message_worker_task.done():
            message_worker_task.cancel()
        if daily_report_task.is_running():
            daily_report_task.stop()
        logger.info("Cleanup complete. Service cleanly shut down.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot process stopped by user.")
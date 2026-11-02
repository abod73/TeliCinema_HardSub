from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from telethon import TelegramClient, events
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import AppConfig, load_config
from subtitle_processor import SubtitleProcessingError, process_video_with_hardcoded_subtitles

logger = logging.getLogger("teli_cinema")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

@dataclass
class PendingJob:
    key: str
    chat_id: int
    video_message_id: int
    subtitle_message_id: int
    work_dir: Path
    input_video_path: Path
    input_subtitle_path: Path
    output_path: Path
    created_at: float = field(default_factory=time.time)
    status: str = "queued"

class HardSubBotService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.bot_app = Application.builder().token(config.BOT_TOKEN).build()
        self.user_client = TelegramClient(str(config.SESSION_FILE), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
        self.queue: asyncio.Queue[PendingJob] = asyncio.Queue()
        self.pending_videos: Dict[tuple[int, int], dict] = {}
        self.jobs: Dict[str, PendingJob] = {}
        self.processing_lock = asyncio.Lock()
        self.active_job_key: Optional[str] = None
        self.cancel_event: Optional[asyncio.Event] = None
        self._register_commands()
        self._register_group_handlers()

    def _register_commands(self):
        self.bot_app.add_handler(CommandHandler("start", self.cmd_start))
        self.bot_app.add_handler(CommandHandler("help", self.cmd_help))
        self.bot_app.add_handler(CommandHandler("status", self.cmd_status))
        self.bot_app.add_handler(CommandHandler("cancel", self.cmd_cancel))

    def _register_group_handlers(self):
        @self.user_client.on(events.NewMessage(chats=self.config.PRIVATE_GROUP_ID))
        async def on_group_message(event):
            try:
                await self._handle_group_message(event)
            except Exception as exc:
                logger.exception("Error handling group message: %s", exc)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_user.id != self.config.OWNER_ID:
            return
        text = (
            "✅ مرحبًا!\n"
            "أرسل الفيديو إلى المجموعة الخاصة، ثم أرسل ملف الترجمة كردّ مباشر على نفس الرسالة.\n"
            "💡 مثال: فيديو + رسالة SRT مرفقة كـ Reply.\n"
            "⚙️ استخدم /status للتحقق من الحالة أو /cancel لإلغاء العمل الحالي."
        )
        await update.message.reply_text(text)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_user.id != self.config.OWNER_ID:
            return
        help_text = (
            "📘 تعليمات التشغيل:\n"
            "1. أضف البوت والمستخدم الشخصي إلى المجموعة الخاصة.\n"
            "2. أرسل الفيديو إلى المجموعة.\n"
            "3. أرسل ملف .srt كرد مباشر على نفس فيديو.\n"
            "4. سيُرسل النظام الفيديو المحوّل إلى الخاص.\n"
            "5. استخدم /status أو /cancel حسب الحاجة."
        )
        await update.message.reply_text(help_text)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_user.id != self.config.OWNER_ID:
            return
        pending = self.queue.qsize()
        text = f"📊 حالة البوت: يعمل. عدد المهام المنتظرة: {pending}."
        await update.message.reply_text(text)

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_user.id != self.config.OWNER_ID:
            return
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.cancel_event.set()
            await update.message.reply_text("🛑 تم طلب إلغاء المهمة الحالية إن كانت قيد المعالجة.")
            return
        await update.message.reply_text("ℹ️ لا توجد مهمة جارية لإلغائها في الوقت الحالي.")

    def _is_video_message(self, msg) -> bool:
        if msg is None or msg.media is None:
            return False
        mime = getattr(msg.media, "mime_type", "") or ""
        if "video/" in mime.lower():
            return True
        if getattr(msg, "file", None) is not None:
            ext = Path(msg.file.name or "").suffix.lower() if getattr(msg.file, "name", None) else ""
            return ext in self.config.SUPPORTED_VIDEO_EXTENSIONS
        return False

    def _is_srt_message(self, msg) -> bool:
        if msg is None or msg.media is None:
            return False
        if getattr(msg.file, "name", None) is None:
            return False
        return Path(msg.file.name).suffix.lower() == ".srt"

    async def _handle_group_message(self, event):
        msg = event.message
        if msg is None:
            return
        if msg.chat_id != self.config.PRIVATE_GROUP_ID:
            return

        if self._is_video_message(msg):
            key = (msg.chat_id, msg.id)
            self.pending_videos[key] = {"message": msg, "downloaded_at": time.time()}
            logger.info("Video captured: chat=%s msg=%s", msg.chat_id, msg.id)
            await self.user_client.send_message(
                self.config.OWNER_ID,
                "✅ تم استلام الفيديو. أرسل ملف الترجمة كردّ مباشر على هذه الرسالة."
            )
            return

        if self._is_srt_message(msg):
            if not msg.is_reply:
                await self.user_client.send_message(
                    self.config.OWNER_ID,
                    "❌ الملف يجب أن يكون ردًّا مباشرًا على الفيديو الصحيح."
                )
                return

            reply_to = msg.reply_to_msg_id
            match_key = (msg.chat_id, reply_to)
            if match_key not in self.pending_videos:
                await self.user_client.send_message(
                    self.config.OWNER_ID,
                    "❌ لا يوجد فيديو مرتبط بهذا الرد. أعد إرسال الفيديو أولاً ثم الترجمة كردّ مباشر."
                )
                return

            video_msg = self.pending_videos[match_key]["message"]
            self.pending_videos.pop(match_key, None)

            job = await self._build_job(video_msg, msg)
            await self.queue.put(job)
            self.jobs[job.key] = job
            logger.info("Queued job: %s", job.key)
            await self.user_client.send_message(
                self.config.OWNER_ID,
                "⏳ جارٍ تنزيل الفيديو والترجمة..."
            )
            return

    async def _build_job(self, video_msg, subtitle_msg) -> PendingJob:
        job_id = uuid.uuid4().hex
        work_dir = self.config.JOBS_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=True)

        video_path = work_dir / "video_input"
        subtitle_path = work_dir / "subtitle_input.srt"
        output_path = work_dir / "output_hardsub.mp4"

        return PendingJob(
            key=job_id,
            chat_id=self.config.PRIVATE_GROUP_ID,
            video_message_id=video_msg.id,
            subtitle_message_id=subtitle_msg.id,
            work_dir=work_dir,
            input_video_path=video_path,
            input_subtitle_path=subtitle_path,
            output_path=output_path,
        )

    async def _download_media(self, source_msg, destination_path: Path) -> Path:
        if destination_path.exists():
            destination_path.unlink()
        downloaded = await source_msg.download_media(file=str(destination_path))
        if downloaded is None:
            raise RuntimeError("Download returned None.")
        return Path(downloaded)

    async def _process_job(self, job: PendingJob) -> None:
        self.active_job_key = job.key
        self.cancel_event = asyncio.Event()

        try:
            async with self.processing_lock:
                await self.user_client.send_message(self.config.OWNER_ID, "📥 جارٍ تنزيل الفيديو والترجمة...")
                video_channel_message = await self.user_client.get_messages(self.config.PRIVATE_GROUP_ID, ids=[job.video_message_id])
                if not video_channel_message:
                    raise RuntimeError("Could not fetch video message.")
                video_msg = video_channel_message[0]

                subtitle_channel_message = await self.user_client.get_messages(self.config.PRIVATE_GROUP_ID, ids=[job.subtitle_message_id])
                if not subtitle_channel_message:
                    raise RuntimeError("Could not fetch subtitle message.")
                subtitle_msg = subtitle_channel_message[0]

                await self._download_media(video_msg, job.input_video_path)
                await self._download_media(subtitle_msg, job.input_subtitle_path)

                # Validate file extensions and size before processing
                if job.input_video_path.suffix.lower() not in self.config.SUPPORTED_VIDEO_EXTENSIONS:
                    raise ValueError(f"Unsupported video extension: {job.input_video_path.suffix}")

                if job.input_subtitle_path.suffix.lower() != ".srt":
                    raise ValueError("Subtitle must be an SRT file.")

                logger.info("Starting hardcoded subtitle rendering for job %s", job.key)

                await self.user_client.send_message(self.config.OWNER_ID, "🎬 جارٍ دمج الترجمة العربية داخل الفيديو...")
                await process_video_with_hardcoded_subtitles(
                    input_video=job.input_video_path,
                    subtitle_srt=job.input_subtitle_path,
                    output_path=job.output_path,
                    config=self.config,
                    cancel_event=self.cancel_event,
                    progress_callback=lambda line: logger.info("FFmpeg: %s", line.strip()),
                )

                await self.user_client.send_message(self.config.OWNER_ID, "📤 اكتملت المعالجة، جارٍ إرسال الفيديو الناتج...")
                await self.user_client.send_file(
                    self.config.OWNER_ID,
                    str(job.output_path),
                    caption="✅ تم دمج الترجمة بنجاح.",
                )
                await self.user_client.send_message(self.config.OWNER_ID, "✅ تم دمج الترجمة بنجاح.")
        except asyncio.CancelledError:
            await self.user_client.send_message(self.config.OWNER_ID, "🛑 تم إلغاء المهمة الحالية.")
            logger.warning("Job cancelled: %s", job.key)
        except Exception as exc:
            logger.exception("Job failed: %s", exc)
            await self.user_client.send_message(self.config.OWNER_ID, f"❌ حدث خطأ أثناء المعالجة. التفاصيل: {exc}")
        finally:
            self.cancel_event = None
            self.active_job_key = None
            if job.work_dir.exists():
                for child in job.work_dir.iterdir():
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                try:
                    job.work_dir.rmdir()
                except OSError:
                    pass
            self.jobs.pop(job.key, None)

    async def run_worker(self):
        while True:
            job = await self.queue.get()
            try:
                await self._process_job(job)
            finally:
                self.queue.task_done()

    async def start(self):
        await self.user_client.start()
        await self.bot_app.initialize()
        await self.bot_app.start()
        await self.bot_app.updater.start_polling()
        asyncio.create_task(self.run_worker())
        logger.info("Bot started and listening.")

    async def stop(self):
        await self.bot_app.stop()
        await self.user_client.disconnect()
        logger.info("Bot stopped.")

async def main():
    config = load_config()
    service = HardSubBotService(config)
    await service.start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")
"""Dumaloq TG MVP: bounded, in-memory queue; one conversion at a time."""
import asyncio
import logging
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BotCommand, FSInputFile, Message
from dotenv import load_dotenv

from media import MAX_INPUT_BYTES, SIDE, MediaError, convert, probe, segments

log = logging.getLogger("dumaloq")


@dataclass
class Job:
    message: Message
    file_id: str
    mode: str


def create_dispatcher(bot: Bot, ffmpeg: str, ffprobe: str, allowed: set[int]):
    dp = Dispatcher()
    queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=8)
    busy: set[int] = set()
    modes: dict[int, str] = {}

    def authorized(message: Message) -> bool:
        return (message.chat.type == "private" and message.from_user is not None
                and (not allowed or message.from_user.id in allowed))

    @dp.message(Command("start", "help"))
    async def welcome(message: Message):
        if authorized(message):
            await message.answer(
                "Dumaloq TG • Видео → кружок\n\n"
                "Пришли видео или видеофайл до 20 МБ. Звук сохранится. "
                "Прямоугольное видео обрежу по центру; углы будут скрыты кругом.\n\n"
                "/trim — первые 60 секунд (по умолчанию)\n"
                "/split — несколько кружков по 60 секунд\n\n"
                "Максимум 10 минут. Настройку выбирай до отправки ролика. "
                "После перезапуска настройки и очередь сбрасываются."
            )

    @dp.message(Command("id"))
    async def show_id(message: Message):
        if authorized(message):
            await message.answer(f"Твой Telegram ID: {message.from_user.id}")

    @dp.message(Command("trim", "split"))
    async def set_mode(message: Message):
        if authorized(message):
            mode = message.text.split()[0].split("@")[0][1:]
            modes[message.from_user.id] = mode
            await message.answer("Режим: " + (
                "первые 60 секунд." if mode == "trim" else "разбиение на кружки по 60 секунд."
            ))

    @dp.message(F.video | F.document)
    async def receive(message: Message):
        if not authorized(message):
            return
        media = message.video or message.document
        if message.document:
            mime = media.mime_type or ""
            suffix = Path(media.file_name or "").suffix.lower()
            if not mime.startswith("video/") and suffix not in {
                ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"
            }:
                await message.answer("Пришли видео или видеофайл.")
                return
        if media.file_size is None or media.file_size > MAX_INPUT_BYTES:
            await message.answer("Нужен исходный файл размером до 20 МБ.")
            return
        user_id = message.from_user.id
        if user_id in busy:
            await message.answer("Твоё видео уже в очереди или обрабатывается. Дождись результата.")
            return
        if queue.full():
            await message.answer("Очередь заполнена. Попробуй чуть позже.")
            return
        # No awaits between capacity check and insertion.
        busy.add(user_id)
        queue.put_nowait(Job(message, media.file_id, modes.get(user_id, "trim")))
        await message.answer("Видео принято в очередь.")

    @dp.message()
    async def fallback(message: Message):
        if authorized(message):
            await message.answer("Пришли видео. Инструкция: /help")

    async def notify(message: Message, text: str):
        try:
            await message.answer(text)
        except TelegramAPIError:
            pass

    async def worker():
        while True:
            job = await queue.get()
            sent = 0
            try:
                await notify(job.message, "Обрабатываю видео…")
                with TemporaryDirectory(prefix="dumaloq-") as directory:
                    source = Path(directory) / "input"
                    await bot.download(job.file_id, destination=source, timeout=90)
                    if source.stat().st_size > MAX_INPUT_BYTES:
                        raise MediaError("Исходный файл превышает 20 МБ.")
                    info = await probe(source, ffprobe)
                    parts = segments(info.duration, job.mode)
                    for index, (start, duration) in enumerate(parts):
                        output = Path(directory) / f"part-{index}.mp4"
                        await convert(source, output, start, duration, ffmpeg)
                        result = await probe(output, ffprobe)
                        if (result.width != SIDE or result.height != SIDE
                                or result.duration > 60.1):
                            raise MediaError("Результат не прошёл проверку формата.")
                        await bot.send_video_note(
                            chat_id=job.message.chat.id,
                            video_note=FSInputFile(output), length=SIDE,
                            duration=min(60, math.ceil(result.duration)),
                            request_timeout=120,
                        )
                        sent += 1
                        output.unlink()
                await notify(job.message, f"Готово! Кружков: {sent}.")
            except asyncio.CancelledError:
                raise
            except MediaError as exc:
                await notify(job.message, f"{exc} Отправлено кружков: {sent}.")
            except asyncio.TimeoutError:
                await notify(job.message, f"Обработка заняла слишком много времени. Отправлено: {sent}.")
            except TelegramAPIError:
                log.warning("Telegram request failed; details omitted")
                await notify(job.message, f"Ошибка загрузки или отправки. Отправлено кружков: {sent}. Попробуй позже.")
            except Exception as exc:
                # Avoid logging tokens, message content, paths or file URLs.
                log.error("Job failed: %s", type(exc).__name__)
                await notify(job.message, f"Не удалось обработать файл. Отправлено кружков: {sent}.")
            finally:
                busy.discard(job.message.from_user.id)
                queue.task_done()

    return dp, worker


async def main():
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "replace_with_token_from_BotFather":
        raise SystemExit("Укажи BOT_TOKEN в файле .env (см. .env.example).")
    ffmpeg = os.getenv("FFMPEG_BIN", "ffmpeg")
    ffprobe = os.getenv("FFPROBE_BIN", "ffprobe")
    for binary in (ffmpeg, ffprobe):
        if not shutil.which(binary):
            raise SystemExit(f"Не найден {binary}. Установи FFmpeg или укажи путь в .env.")
    try:
        allowed = {int(value.strip()) for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
                   if value.strip()}
    except ValueError:
        raise SystemExit("ALLOWED_USER_IDS должен содержать числовые ID через запятую.")
    async with Bot(token=token) as bot:
        dp, worker = create_dispatcher(bot, ffmpeg, ffprobe, allowed)
        await bot.set_my_commands([
            BotCommand(command="start", description="Как пользоваться"),
            BotCommand(command="trim", description="Первые 60 секунд"),
            BotCommand(command="split", description="Разбить на кружки"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="id", description="Мой Telegram ID"),
        ])
        task = asyncio.create_task(worker())
        try:
            # Do not silently delete an existing webhook or discard pending messages.
            await dp.start_polling(bot, close_bot_session=False)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, SendVideoNote
from aiogram.types import Chat, Message, Update, User

from bot import create_dispatcher
from media import MAX_INPUT_BYTES, MediaError, MediaInfo


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.completed = asyncio.Event()

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage) and (
            method.text.startswith("Готово!") or "Отправлено кружков:" in method.text
        ):
            self.completed.set()
        return Message(
            message_id=len(self.calls),
            date=datetime.now(timezone.utc),
            chat=Chat(id=int(method.chat_id), type="private"),
        )

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        yield b""


def incoming(text=None, video=None, document=None, user_id=42, chat_type="private"):
    return Update(
        update_id=100,
        message=Message(
            message_id=10, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Tester"),
            text=text, video=video, document=document,
        ),
    )


def video(size=1024):
    return dict(file_id="test-video", file_unique_id="unique-test-video",
                width=640, height=640, duration=65, file_size=size)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = RecordingSession()
        self.bot = Bot("123456:TEST_TOKEN_NOT_A_REAL_SECRET", session=self.session)

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def test_rejects_oversized_input_before_downloading(self):
        dp, _ = create_dispatcher(self.bot, "ffmpeg", "ffprobe", set())
        await dp.feed_update(self.bot, incoming(video=video(MAX_INPUT_BYTES + 1)))
        self.assertIn("20 МБ", self.session.calls[-1].text)
        self.assertEqual(len(self.session.calls), 1)

    async def test_denies_user_outside_allowlist_and_groups(self):
        dp, _ = create_dispatcher(self.bot, "ffmpeg", "ffprobe", {99})
        await dp.feed_update(self.bot, incoming(text="/start"))
        dp2, _ = create_dispatcher(self.bot, "ffmpeg", "ffprobe", set())
        await dp2.feed_update(self.bot, incoming(video=video(), chat_type="group"))
        self.assertEqual(self.session.calls, [])

    async def test_split_sends_two_notes_and_removes_temporary_files(self):
        dp, worker = create_dispatcher(self.bot, "ffmpeg", "ffprobe", set())
        paths = []

        async def download(file_id, destination, timeout):
            paths.append(Path(destination))
            Path(destination).write_bytes(b"fake-input")

        async def transcode(source, target, start, duration, binary):
            paths.append(Path(target))
            Path(target).write_bytes(b"fake-output")

        self.bot.download = AsyncMock(side_effect=download)
        metadata = [MediaInfo(65, 640, 640, True),
                    MediaInfo(60, 640, 640, True), MediaInfo(5, 640, 640, True)]
        with patch("bot.probe", new=AsyncMock(side_effect=metadata)), \
             patch("bot.convert", new=AsyncMock(side_effect=transcode)) as conversion:
            await dp.feed_update(self.bot, incoming(text="/split"))
            await dp.feed_update(self.bot, incoming(video=video()))
            await dp.feed_update(self.bot, incoming(video=video()))
            self.assertIn("уже", self.session.calls[-1].text)
            task = asyncio.create_task(worker())
            try:
                await asyncio.wait_for(self.session.completed.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        notes = [m for m in self.session.calls if isinstance(m, SendVideoNote)]
        self.assertEqual([m.duration for m in notes], [60, 5])
        self.assertTrue(all(m.length == 640 for m in notes))
        self.assertEqual(conversion.await_count, 2)
        self.assertEqual(conversion.await_args_list[1].args[2:4], (60, 5))
        self.assertTrue(paths)
        self.assertTrue(all(not p.exists() for p in paths))

    async def test_failed_job_cleans_up_and_releases_user(self):
        dp, worker = create_dispatcher(self.bot, "ffmpeg", "ffprobe", set())
        paths = []

        async def download(file_id, destination, timeout):
            paths.append(Path(destination))
            Path(destination).write_bytes(b"invalid-video")

        self.bot.download = AsyncMock(side_effect=download)
        with patch("bot.probe", new=AsyncMock(side_effect=MediaError("Повреждённое видео."))):
            await dp.feed_update(self.bot, incoming(video=video()))
            task = asyncio.create_task(worker())
            try:
                await asyncio.wait_for(self.session.completed.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(all(not p.exists() for p in paths))
        self.assertIn("Повреждённое видео", self.session.calls[-1].text)
        await dp.feed_update(self.bot, incoming(video=video()))
        self.assertIn("принято", self.session.calls[-1].text)

    async def test_id_command(self):
        dp, _ = create_dispatcher(self.bot, "ffmpeg", "ffprobe", set())
        await dp.feed_update(self.bot, incoming(text="/id"))
        self.assertEqual(self.session.calls[-1].text, "Твой Telegram ID: 42")


if __name__ == "__main__":
    unittest.main()

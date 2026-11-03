import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from media import MediaError, convert, probe, run_process, segments


class SegmentsTests(unittest.TestCase):
    def test_trim_preserves_short_clip(self):
        self.assertEqual(segments(12.4, "trim"), [(0, 12.4)])

    def test_trim_caps_long_clip(self):
        self.assertEqual(segments(120, "trim"), [(0, 60)])

    def test_split_preserves_remainder(self):
        self.assertEqual(segments(125, "split"), [(0, 60), (60, 60), (120, 5)])

    def test_exact_minute_does_not_create_empty_clip(self):
        self.assertEqual(segments(120, "split"), [(0, 60), (60, 60)])
        self.assertEqual(len(segments(120.001, "split")), 2)

    def test_reject_invalid_durations(self):
        for duration in (0, -1, float("inf"), float("nan"), 601):
            with self.subTest(duration=duration), self.assertRaises(MediaError):
                segments(duration, "split")


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_success(self):
        self.assertEqual(await run_process(sys.executable, "-c", "print('ok')"), b"ok\r\n" if os.name == "nt" else b"ok\n")

    async def test_failure_hides_diagnostics(self):
        with self.assertRaises(MediaError) as result:
            await run_process(sys.executable, "-c", "import sys; sys.stderr.write('private'); sys.exit(1)")
        self.assertNotIn("private", str(result.exception))

    async def test_timeout_terminates_process(self):
        with self.assertRaises(asyncio.TimeoutError):
            await run_process(sys.executable, "-c", "import time; time.sleep(30)", timeout=0.1)

    async def test_probe_rejects_audio_only(self):
        with patch("media.run_process", new=AsyncMock(return_value=b'{"streams": []}')):
            with self.assertRaises(MediaError):
                await probe(Path("input"))

    async def test_probe_falls_back_to_container_duration(self):
        data = {"streams": [{"codec_type": "video", "width": 800, "height": 800},
                            {"codec_type": "audio"}], "format": {"duration": "5.5"}}
        with patch("media.run_process", new=AsyncMock(return_value=json.dumps(data).encode())):
            info = await probe(Path("input"))
        self.assertEqual(info.duration, 5.5)
        self.assertTrue(info.audio)


@unittest.skipUnless(shutil.which(os.getenv("FFMPEG_BIN", "ffmpeg")) and
                     shutil.which(os.getenv("FFPROBE_BIN", "ffprobe")),
                     "FFmpeg and FFprobe are required for real media tests")
class FFmpegTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_transcoding_with_and_without_audio(self):
        ffmpeg = os.getenv("FFMPEG_BIN", "ffmpeg")
        ffprobe = os.getenv("FFPROBE_BIN", "ffprobe")
        with TemporaryDirectory() as directory:
            for audio in (False, True):
                with self.subTest(audio=audio):
                    source = Path(directory) / f"input-{audio}.mp4"
                    target = Path(directory) / f"output-{audio}.mp4"
                    command = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                               "testsrc2=size=480x320:rate=24"]
                    if audio:
                        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
                    await run_process(*command, "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source))
                    await convert(source, target, 0.5, 1, ffmpeg)
                    result = await probe(target, ffprobe)
                    self.assertEqual((result.width, result.height), (640, 640))
                    self.assertEqual(result.audio, audio)
                    self.assertAlmostEqual(result.duration, 1, delta=0.1)


if __name__ == "__main__":
    unittest.main()

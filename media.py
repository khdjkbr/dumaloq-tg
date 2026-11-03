"""Media processing independent of Telegram. Requires FFmpeg and FFprobe."""
import asyncio
import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_INPUT_BYTES = 20_000_000
MAX_OUTPUT_BYTES = 49_000_000
MAX_DURATION = 600
SIDE = 640


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    audio: bool


async def run_process(*args: str, timeout: float = 180) -> bytes:
    # Use ordinary OS pipes: works on Windows without asyncio named-pipe support.
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    reader = asyncio.create_task(asyncio.to_thread(process.communicate, timeout=timeout))
    try:
        stdout, _stderr = await asyncio.shield(reader)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        await asyncio.to_thread(process.communicate)
        raise asyncio.TimeoutError from exc
    except asyncio.CancelledError:
        if process.poll() is None:
            process.kill()
        # Drain the same reader before its temporary directory is removed.
        try:
            await reader
        except subprocess.TimeoutExpired:
            await asyncio.to_thread(process.communicate)
        raise
    if process.returncode:
        # Do not leak input paths, metadata or subprocess diagnostics to users.
        raise MediaError("Не удалось прочитать или обработать видео.")
    return stdout


async def probe(path: Path, binary: str = "ffprobe") -> MediaInfo:
    raw = await run_process(
        binary, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path), timeout=30,
    )
    try:
        data = json.loads(raw)
        streams = data["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video"
                     and not s.get("disposition", {}).get("attached_pic"))
        duration = float(video.get("duration") or data["format"]["duration"])
        width, height = int(video["width"]), int(video["height"])
        if not math.isfinite(duration) or duration <= 0 or min(width, height) <= 0:
            raise ValueError
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise MediaError("Файл не содержит корректного видео с известной длительностью.") from exc
    return MediaInfo(duration, width, height,
                     any(s.get("codec_type") == "audio" for s in streams))


def segments(duration: float, mode: str) -> list[tuple[float, float]]:
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
        raise MediaError("Для первой версии максимальная длительность — 10 минут.")
    if mode not in {"trim", "split"}:
        raise ValueError("Unknown mode")
    if mode == "trim":
        return [(0, min(60, duration))]
    # Ignore sub-frame container rounding at exact minute boundaries.
    count = max(1, math.ceil((duration - 0.02) / 60))
    return [(i * 60, min(60, duration - i * 60)) for i in range(count)]


async def convert(source: Path, target: Path, start: float, duration: float,
                  binary: str = "ffmpeg") -> None:
    # FFmpeg autorotation is enabled. Normalize display aspect ratio before crop.
    filters = (
        "scale=trunc(ih*dar/2)*2:ih,setsar=1,"
        "crop=min(iw\\,ih):min(iw\\,ih),scale=640:640,fps=30,format=yuv420p"
    )
    await run_process(
        binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(start), "-i", str(source), "-t", str(duration),
        "-map", "0:V:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", filters, "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-maxrate", "2500k", "-bufsize", "5000k",
        "-threads", "2", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-map_metadata", "-1", "-movflags", "+faststart", str(target),
    )
    if not target.exists() or not 0 < target.stat().st_size < MAX_OUTPUT_BYTES:
        raise MediaError("Полученный файл пустой или слишком большой.")

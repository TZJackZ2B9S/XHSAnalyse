"""图片格式归一化与 HDR 检测。"""

import asyncio
from pathlib import Path

import aiofiles

from gsuid_core.logger import logger

_PROCESS_TIMEOUT = 30.0


async def _run_command(*args: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=_PROCESS_TIMEOUT)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            logger.warning("[XHSAnalyse] 图片转码超时")
            return False
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            logger.debug(f"[XHSAnalyse] 图片转码失败：{detail[-300:]}")
            return False
        return True
    except OSError as error:
        logger.debug(f"[XHSAnalyse] 图片转码命令不可用：{error}")
        return False


async def is_jpeg(path: Path) -> bool:
    async with aiofiles.open(path, "rb") as file:
        header = await file.read(2)
    return header == b"\xff\xd8"


async def ensure_jpeg(path: Path) -> Path | None:
    """将 WebP/HEIF 等图片转为 JPEG，成功后返回新路径。"""

    if await is_jpeg(path):
        return path
    output = path.with_suffix(".converted.jpg")
    ffmpeg_args = (
        "ffmpeg",
        "-y",
        "-threads",
        "1",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output),
    )
    if await _run_command(*ffmpeg_args):
        return output
    heif_args = ("heif-convert", "--quiet", "-q", "95", str(path), str(output))
    if await _run_command(*heif_args):
        return output
    return None


async def contains_hdr(path: Path) -> bool:
    """检测 Ultra HDR JPEG；自合成 Live Photo 通过 MotionPhoto 标记排除。"""

    async with aiofiles.open(path, "rb") as file:
        data = await file.read()
    if b"GCamera:MotionPhoto" in data:
        return False
    return b"hdrgm" in data or b"GainMap" in data

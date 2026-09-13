"""小红书媒体流式下载与缓存。"""

import re
import time
import asyncio
import hashlib
import secrets
from pathlib import Path

import httpx
import aiofiles

from gsuid_core.logger import logger

from ..resource.RESOURCE_PATH import CACHE_PATH

_CHUNK_SIZE = 1024 * 1024
_CACHE_TTL_SECONDS = 24 * 60 * 60
_FILE_SEND_TTL_SECONDS = 30 * 60
_CACHE_PREFIX = "xhs_"
_cleanup_tasks: set[asyncio.Task[None]] = set()


def sanitize_filename(value: str, max_bytes: int = 100) -> str:
    """生成适合跨平台保存的文件名片段。"""

    cleaned = re.sub(r'[\\/:*?"<>|]', "_", value)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_.-")
    output = ""
    for char in cleaned:
        if len((output + char).encode()) > max_bytes:
            break
        output += char
    return output or "unknown"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()[:8]


def media_filename(note_title: str, author: str, index: int, total: int, url: str, suffix: str) -> str:
    suffix_text = f"_{index + 1}" if total > 1 else ""
    return f"xhs_{sanitize_filename(note_title)}_{sanitize_filename(author, 48)}{suffix_text}_{short_hash(url)}{suffix}"


def cache_path(name: str) -> Path:
    return CACHE_PATH / f"{_CACHE_PREFIX}{name}"


async def cleanup_cache(max_age_seconds: int = _CACHE_TTL_SECONDS) -> int:
    """清理过期缓存，返回删除数量。"""

    if not CACHE_PATH.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    paths = await asyncio.to_thread(lambda: tuple(CACHE_PATH.glob(f"{_CACHE_PREFIX}*")))
    for path in paths:
        stat = await asyncio.to_thread(path.stat)
        if stat.st_mtime < cutoff:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            removed += 1
    if removed:
        logger.info(f"[XHSAnalyse] 清理 {removed} 个过期媒体缓存")
    return removed


async def cleanup_stale_cache() -> None:
    """Core 启动前清理上次进程残留的媒体文件。"""

    await cleanup_cache(_FILE_SEND_TTL_SECONDS)


async def _remove_later(path: Path) -> None:
    await asyncio.sleep(_FILE_SEND_TTL_SECONDS)
    await asyncio.to_thread(path.unlink, missing_ok=True)


def schedule_file_cleanup(path: Path) -> None:
    """为 file:// 发送保留读取窗口，避免适配器异步读取时文件被删除。"""

    task = asyncio.create_task(_remove_later(path), name=f"XHSAnalyse:remove-file:{path.name}")
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)


async def download_media(
    client: httpx.AsyncClient,
    url: str,
    *,
    suffix: str,
    max_bytes: int,
    retries: int = 3,
    headers: dict[str, str] | None = None,
    output_logs: bool = False,
) -> Path | None:
    """流式下载媒体到缓存目录，超过大小限制时删除半成品。"""

    output = cache_path(f"{secrets.token_hex(8)}{suffix}")
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.xiaohongshu.com/",
        "Origin": "https://www.xiaohongshu.com",
    }
    if headers:
        request_headers.update(headers)

    for attempt in range(retries):
        total = 0
        try:
            if output_logs:
                logger.info(f"[XHSAnalyse] 开始下载媒体（第 {attempt + 1}/{retries} 次，类型 {suffix}）")
            async with client.stream("GET", url, headers=request_headers) as response:
                response.raise_for_status()
                content_length_text = response.headers.get("content-length")
                content_length = int(content_length_text) if content_length_text is not None else 0
                if content_length > max_bytes:
                    logger.warning(f"[XHSAnalyse] 媒体超过大小上限，跳过：{url}")
                    return None
                async with aiofiles.open(output, "wb") as file:
                    async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("媒体超过大小上限")
                        await file.write(chunk)
            if total > 0:
                if output_logs:
                    logger.info(f"[XHSAnalyse] 媒体下载完成：{total / 1024 / 1024:.1f} MB")
                return output
            raise ValueError("媒体响应为空")
        except (httpx.HTTPError, OSError, ValueError) as error:
            await asyncio.to_thread(output.unlink, missing_ok=True)
            if attempt + 1 >= retries:
                logger.warning(f"[XHSAnalyse] 媒体下载失败：{url}：{error}")
                return None
            await asyncio.sleep(min(float(attempt + 1), 3.0))
    return None


async def download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    retries: int = 3,
) -> bytes | None:
    path = await download_media(client, url, suffix=".bin", max_bytes=max_bytes, retries=retries)
    if path is None:
        return None
    try:
        async with aiofiles.open(path, "rb") as file:
            return await file.read()
    finally:
        await asyncio.to_thread(path.unlink, missing_ok=True)

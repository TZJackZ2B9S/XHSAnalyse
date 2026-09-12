"""媒体下载、格式处理与消息段构建。"""

import asyncio
from pathlib import Path
from dataclasses import dataclass

import httpx

from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.segment import MessageSegment

from .image import ensure_jpeg
from .motion import build_motion_photo
from .download import download_media, media_filename
from ..parse.models import MediaItem, NoteResult
from ..config.xhs_config import XhsSettings

_LOCAL_FILE_HOST = "localhost"
_MEDIA_CONCURRENCY = 3


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    """已下载并处理完成的媒体文件。"""

    path: Path
    item: MediaItem
    index: int
    is_video: bool


def local_file_uri(path: Path) -> str:
    """生成带主机名的 file URI，避免部分适配器补成 https。"""

    return f"file://{_LOCAL_FILE_HOST}{path.as_uri().removeprefix('file://')}"


async def _download_single(
    client: httpx.AsyncClient,
    item: MediaItem,
    index: int,
    total: int,
    result: NoteResult,
    settings: XhsSettings,
) -> PreparedMedia | None:
    is_video = item.is_video
    suffix = ".mp4" if is_video else ".jpg"
    cache_url = f"{item.url}|{item.live_url}" if item.is_live else item.url
    filename = media_filename(result.title, result.author, index, total, cache_url, suffix)
    target = await download_media(
        client,
        item.url,
        suffix=suffix,
        max_bytes=settings.max_media_size,
        retries=settings.fetch_retries,
    )
    if target is None:
        return None

    if item.is_live and item.live_url and settings.convert_live_photo:
        video_path = await download_media(
            client,
            item.live_url,
            suffix=".mp4",
            max_bytes=settings.max_media_size,
            retries=settings.fetch_retries,
        )
        if video_path is None:
            target.unlink(missing_ok=True)
            return None
        output = target.with_name(filename)
        try:
            created = await build_motion_photo(target, video_path, output)
        finally:
            video_path.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
        if not created:
            logger.warning(f"[XHSAnalyse] Live Photo 合成失败：{item.url}")
            return None
        return PreparedMedia(output, item, index, False)

    if not is_video:
        jpeg_path = await ensure_jpeg(target)
        if jpeg_path is None:
            logger.warning(f"[XHSAnalyse] 图片转码失败：{item.url}")
            target.unlink(missing_ok=True)
            return None
        if jpeg_path != target:
            target.unlink(missing_ok=True)
            target = jpeg_path

    output = target.with_name(filename)
    target.replace(output)
    return PreparedMedia(output, item, index, is_video)


async def prepare_media(
    client: httpx.AsyncClient,
    result: NoteResult,
    settings: XhsSettings,
) -> tuple[PreparedMedia, ...]:
    """下载全部媒体；单个失败不会中断其他媒体。"""

    semaphore = asyncio.Semaphore(_MEDIA_CONCURRENCY)

    async def guarded(index: int, item: MediaItem) -> PreparedMedia | None:
        async with semaphore:
            return await _download_single(client, item, index, len(result.media), result, settings)

    prepared = await asyncio.gather(
        *(guarded(index, item) for index, item in enumerate(result.media))
    )
    return tuple(item for item in prepared if item is not None)


def media_to_message(media: PreparedMedia, *, video_send_type: str) -> Message:
    if media.is_video and video_send_type == "file":
        return Message(type="video", data=local_file_uri(media.path))
    if media.is_video:
        return MessageSegment.video(media.path)
    return MessageSegment.image(media.path)


def build_info_text(result: NoteResult, media: tuple[PreparedMedia, ...]) -> str:
    lines = [f"标题: {result.title}", f"作者: {result.author}"]
    if result.publish_time:
        lines.append(f"发布时间: {result.publish_time}")
    if result.type == "video" and result.video_quality:
        lines.append(f"视频画质: {result.video_quality}")
    if result.cookie_expired and result.type == "video":
        lines.append("提示: 本次未使用登录态，视频可能只有 720p，请检查 Cookie")
    if result.desc:
        lines.append(result.desc)
    if result.has_live_photo:
        lines.append("该图集包含 Live 图，查看原图并保存到相册即可查看")
    if result.has_hdr_image or any(item.item.is_hdr for item in media):
        lines.append("该图集包含 HDR 图片，查看原图保存到相册即可查看 HDR 效果")
    return "\n".join(lines)


def build_forward_message(result: NoteResult, media: tuple[PreparedMedia, ...], info_text: str) -> Message:
    """构建与 Yunzai 版本一致的信息/封面节点加媒体节点。"""

    images = [item for item in media if not item.is_video]
    cover = next((item for item in images if item.item.is_cover), None)
    if cover is None and result.type == "video" and images:
        cover = images[0]

    nodes: list[Message] = []
    if cover is not None:
        nodes.append(MessageSegment.node([MessageSegment.text(info_text), MessageSegment.image(cover.path)]))
    else:
        nodes.append(MessageSegment.text(info_text))

    for item in media:
        if cover is not None and item.path == cover.path:
            continue
        nodes.append(media_to_message(item, video_send_type="base64"))
    return MessageSegment.node(nodes)


def cleanup_media(media: tuple[PreparedMedia, ...]) -> None:
    for item in media:
        item.path.unlink(missing_ok=True)

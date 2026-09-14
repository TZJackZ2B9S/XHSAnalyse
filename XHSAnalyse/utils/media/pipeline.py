"""媒体下载、格式处理与消息段构建。"""

import asyncio
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

import httpx

from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.server import on_core_start_before
from gsuid_core.segment import MessageSegment

from .image import ensure_jpeg
from .motion import build_motion_photo
from .download import download_media, media_filename, cleanup_stale_cache, schedule_file_cleanup
from ..parse.models import MediaItem, NoteResult
from ...xhs_config.xhs_config import XhsSettings

_LOCAL_FILE_HOST = "localhost"
_MEDIA_CONCURRENCY = 5


@on_core_start_before
async def cleanup_media_cache() -> None:
    await cleanup_stale_cache()


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    """已下载并处理完成的媒体文件。"""

    path: Path
    item: MediaItem
    index: int
    is_video: bool


def local_file_uri(path: Path) -> str:
    """生成带主机名的本地 file URI，供 Bot 端按原始媒体协议读取。"""

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
    suffix = ".mp4" if is_video else ".jpg" if item.is_live else ".heic" if item.is_hdr else ".jpg"
    cache_url = f"{item.url}|{item.live_url}" if item.is_live else item.url
    filename = media_filename(result.title, result.author, index, total, cache_url, suffix)
    target = await download_media(
        client,
        item.url,
        suffix=suffix,
        max_bytes=settings.max_media_size,
        retries=settings.fetch_retries,
        headers={"Cookie": settings.cookie} if settings.cookie else None,
        output_logs=settings.output_logs,
    )
    if target is None:
        return None

    output: Path | None = None
    try:
        if item.is_live and item.live_url and settings.convert_live_photo:
            video_path = await download_media(
                client,
                item.live_url,
                suffix=".mp4",
                max_bytes=settings.max_media_size,
                retries=settings.fetch_retries,
                headers={"Cookie": settings.cookie} if settings.cookie else None,
                output_logs=settings.output_logs,
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
                output.unlink(missing_ok=True)
                return None
            return PreparedMedia(output, item, index, False)

        if not is_video and not item.is_hdr:
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
    except (OSError, RuntimeError, ValueError):
        target.unlink(missing_ok=True)
        if output is not None:
            output.unlink(missing_ok=True)
        raise


async def prepare_media(
    client: httpx.AsyncClient,
    result: NoteResult,
    settings: XhsSettings,
    on_media_ready: Callable[[PreparedMedia], Awaitable[None]] | None = None,
) -> tuple[PreparedMedia, ...]:
    """并发下载全部媒体；单个失败不会中断其他媒体。

    ``on_media_ready`` 会在单个媒体完成格式处理后立即调用。回调应只安排
    后续工作，不要等待其它媒体；返回值仍按笔记原始顺序排列。
    """

    semaphore = asyncio.Semaphore(_MEDIA_CONCURRENCY)

    async def guarded(index: int, item: MediaItem) -> PreparedMedia | None:
        prepared: PreparedMedia | None
        async with semaphore:
            try:
                prepared = await _download_single(client, item, index, len(result.media), result, settings)
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning(f"[XHSAnalyse] 跳过第 {index + 1} 个媒体：{error}")
                return None
        if prepared is not None and on_media_ready is not None:
            await on_media_ready(prepared)
        return prepared

    prepared = await asyncio.gather(*(guarded(index, item) for index, item in enumerate(result.media)))
    return tuple(item for item in prepared if item is not None)


def media_to_message(
    media: PreparedMedia,
    *,
    video_send_type: str,
    image_send_type: str = "framework",
) -> Message:
    if media.is_video and video_send_type == "file":
        schedule_file_cleanup(media.path)
        return MessageSegment.video(local_file_uri(media.path))
    if media.is_video:
        return MessageSegment.video(media.path)
    if image_send_type == "file":
        # Core 支持原生透传 image + file://，因此无需经过通用 file 段。
        # 这样单图和合并转发都保持图片语义，同时保留 HEIF/HDR/Live 原文件。
        return MessageSegment.image(local_file_uri(media.path))
    return MessageSegment.image(media.path)


def build_info_text(result: NoteResult, media: tuple[PreparedMedia, ...]) -> str:
    lines = [f"标题: {result.title}", f"作者: {result.author}"]
    if result.publish_time:
        lines.append(f"发布时间: {result.publish_time}")
    if result.ip_location:
        lines.append(f"IP归属地: {result.ip_location}")
    if result.type == "video" and result.video_quality:
        quality = result.video_quality
        if quality.lower() == "origin":
            quality = "原画（原始视频流）"
        lines.append(f"视频画质: {quality}")
        actual_height = next(
            (item.height or item.width for item in result.media if item.is_video),
            0,
        )
        if result.target_video_height > 0 and actual_height > 0 and actual_height < result.target_video_height:
            lines.append(
                f"提示: 目标画质 {result.target_video_height}p，源视频最高仅 {actual_height}p，已按实际最高画质下载"
            )
    if result.cookie_expired and result.type == "video":
        lines.append("提示: 本次未使用 Cookies，视频通常最高 720p；有 Cookies 也受源视频实际清晰度限制")
    if result.desc:
        lines.append(result.desc)
    if result.has_live_photo:
        lines.append("该图集包含 Live 图，查看原图并保存到相册即可查看")
    if result.has_hdr_image or any(item.item.is_hdr for item in media):
        lines.append("该图集包含 HDR 图片，查看原图保存到相册即可查看 HDR 效果")
    return "\n".join(lines)


def build_note_message(info_text: str, card_image: bytes | None = None) -> Message:
    """构建独立发送的卡片消息；没有卡片时发送文案。"""

    return MessageSegment.image(card_image) if card_image is not None else MessageSegment.text(info_text)


def build_media_message(
    media: tuple[PreparedMedia, ...],
    *,
    video_send_type: str = "base64",
    image_send_type: str = "framework",
) -> Message:
    """构建单条媒体消息或合并转发消息。"""

    if len(media) == 1:
        return media_to_message(
            media[0],
            video_send_type=video_send_type,
            image_send_type=image_send_type,
        )
    return MessageSegment.node(
        [
            media_to_message(
                item,
                video_send_type=video_send_type,
                image_send_type=image_send_type,
            )
            for item in media
        ]
    )


def select_media_for_delivery(
    result: NoteResult,
    media: tuple[PreparedMedia, ...],
) -> tuple[PreparedMedia, ...]:
    """视频笔记只发送视频流，封面仅用于卡片；其他笔记保留全部媒体。"""

    if result.type != "video":
        return media
    videos = tuple(item for item in media if item.is_video)
    return videos or media


def cleanup_media(
    media: tuple[PreparedMedia, ...],
    *,
    video_send_type: str = "base64",
    image_send_type: str = "framework",
) -> None:
    """清理本次处理产生的媒体文件。

    ``file`` 模式的媒体需要等待适配器从 ``file://`` 路径读取文件。Core
    的 ``Bot.send`` 只负责将消息放入异步发送队列，调用返回时适配器可能
    尚未读取文件，因此图片和视频都通过 :func:`schedule_file_cleanup`
    延迟删除；其余媒体在发送入队后即可删除。
    """

    for item in media:
        if (item.is_video and video_send_type == "file") or (
            not item.is_video and image_send_type == "file"
        ):
            schedule_file_cleanup(item.path)
        else:
            item.path.unlink(missing_ok=True)

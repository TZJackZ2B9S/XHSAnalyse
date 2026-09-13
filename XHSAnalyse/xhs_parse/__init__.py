"""小红书链接解析、媒体准备与转发。"""

import re
import asyncio

import httpx

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import ai_return

from ..utils.card import render_note_card
from ..utils.parse.note import NoteParseError, parse_note, enrich_author_profile
from ..utils.parse.urls import extract_urls, is_short_link, extract_note_id
from ..utils.parse.models import NoteResult
from ..utils.media.pipeline import (
    PreparedMedia,
    cleanup_media,
    prepare_media,
    build_info_text,
    build_note_message,
    build_media_message,
)
from ..xhs_config.xhs_config import XhsSettings, get_settings

sv = SV("小红书解析")

_processing_users: set[str] = set()
_processing_lock = asyncio.Lock()
_RN_COMMAND_RE = re.compile(r"^rn(?:\s|https?://|$)", re.IGNORECASE)


def _flatten_message_data(value: object) -> str:
    """提取分享卡片、JSON/XML 和合并转发中的文本，不记录原始卡片内容。"""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(
            part
            for key, item in value.items()
            for part in (_flatten_message_data(key), _flatten_message_data(item))
            if part
        )
    if isinstance(value, (list, tuple)):
        return " ".join(part for item in value for part in (_flatten_message_data(item),) if part)
    return ""


def _event_link_text(ev: Event) -> str:
    """兼容普通文本和平台分享卡片，统一交给 URL 提取器处理。"""

    parts = [ev.raw_text, ev.text]
    for message in ev.content:
        if message.type in {"text", "json", "xml", "node", "markdown", "share"}:
            data_text = _flatten_message_data(message.data)
            if data_text:
                parts.append(data_text)
    return " ".join(part.strip() for part in parts if part and part.strip())


def _sorted_urls(urls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            urls,
            key=lambda url: (0 if extract_note_id(url) else 1 if is_short_link(url) else 2),
        )
    )


async def _claim_processing(user_id: str) -> bool:
    async with _processing_lock:
        if user_id in _processing_users:
            return False
        _processing_users.add(user_id)
        return True


async def _release_processing(user_id: str) -> None:
    async with _processing_lock:
        _processing_users.discard(user_id)


async def _parse_first_valid(
    client: httpx.AsyncClient,
    urls: tuple[str, ...],
    settings: XhsSettings,
) -> NoteResult:
    last_error: NoteParseError | None = None
    for url in _sorted_urls(urls):
        try:
            if settings.output_logs:
                logger.info(f"[XHSAnalyse] 开始解析链接：{url[:160]}")
            result = await parse_note(
                client,
                url,
                settings.cookie,
                prefer_original_image=settings.prefer_original_image,
                max_video_height=settings.target_video_height,
                prefer_hdr_video=settings.prefer_hdr_video,
                fallback_without_cookie=settings.fallback_without_cookie,
            )
            if settings.render_card:
                result = await enrich_author_profile(client, result, cookie=settings.cookie)
            return result
        except NoteParseError as error:
            last_error = error
            if "无法提取笔记 ID" not in str(error) and "无法提取笔记数据" not in str(error):
                raise
            logger.warning(f"[XHSAnalyse] 跳过无效候选链接：{url[:120]}（{error}）")
    raise last_error or NoteParseError("无法提取笔记 ID")


async def _handle_urls(
    bot: Bot,
    ev: Event,
    urls: tuple[str, ...],
    *,
    notify: bool,
) -> bool:
    settings = get_settings()
    if not await _claim_processing(ev.user_id):
        if notify:
            await bot.send("正在处理上一条小红书链接，请稍后重试")
        return False
    processing_ids: list[str | int] | None = None
    card_image: bytes | None = None
    media: tuple[PreparedMedia, ...] = ()
    try:
        if notify:
            recall_ids = await bot.send("检测到小红书链接，正在解析...", wait_recall=True)
            if recall_ids is not None:
                processing_ids = []
                processing_ids.extend(recall_ids)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
            proxy=settings.proxy or None,
            trust_env=False,
        ) as client:
            result = await _parse_first_valid(client, urls, settings)
            if settings.output_logs:
                logger.info(
                    f"[XHSAnalyse] 笔记解析成功：{result.note_id}，类型={result.type}，媒体数={len(result.media)}"
                )
            media = await prepare_media(client, result, settings)
            if settings.output_logs:
                logger.info(f"[XHSAnalyse] 媒体准备完成：成功 {len(media)}/{len(result.media)} 个")
            if settings.render_card:
                cover = next((item for item in media if not item.is_video), None)
                if cover is not None:
                    card_image = await render_note_card(
                        result,
                        cover.path,
                        client,
                        render_scale=settings.render_scale,
                    )
        if not media:
            if notify:
                await bot.send("未能下载任何媒体，请稍后重试")
            return False
        info_text = build_info_text(result, media)
        author_stats = "；".join(
            value
            for value in (
                f"关注 {result.author_follows}" if result.author_follows else "",
                f"粉丝 {result.author_fans}" if result.author_fans else "",
                f"获赞与收藏 {result.author_like_and_collect}" if result.author_like_and_collect else "",
            )
            if value
        )
        ai_text = info_text
        if author_stats:
            ai_text += f"\n作者资料: {author_stats}"
        ai_return(ai_text)
        await bot.send(build_note_message(info_text, card_image))
        await bot.send(build_media_message(media, video_send_type=settings.video_send_type))
        return True
    except (NoteParseError, httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
        logger.warning(f"[XHSAnalyse] 解析失败：{error}")
        if notify:
            await bot.send(str(error))
        return False
    finally:
        cleanup_media(media, video_send_type=settings.video_send_type)
        await bot.unsend(processing_ids)
        await _release_processing(ev.user_id)


@sv.on_command(
    "rn",
    block=True,
    prefix=False,
    to_ai="""解析小红书分享链接或笔记链接，下载无水印图片、视频和 Live 图。
当用户发送小红书链接并要求解析、下载或去水印时调用。

Args:
    text: 小红书分享链接或笔记链接，可直接填写 URL；多个链接可用空格分隔。
          例如：rn https://xhslink.cn/o/xxxx；
          https://www.xiaohongshu.com/explore/0123456789abcdef01234567；
          或同时提供两个分享链接。
""",
    covers=["小红书笔记解析", "小红书无水印下载", "小红书视频图片"],
    aliases=["小红书·解析链接", "小红书·下载笔记", "XHS·去水印"],
)
async def xhs_parse(bot: Bot, ev: Event) -> None:
    urls = extract_urls(ev.text)
    if not urls:
        await bot.send("请发送小红书分享链接或笔记链接，例如：rn https://xhslink.cn/o/xxxx")
        return
    await _handle_urls(bot, ev, urls, notify=True)


@sv.on_message()
async def xhs_detect_links(bot: Bot, ev: Event) -> None:
    """自动解析普通消息中的小红书链接；命令消息由命令触发器处理。"""

    settings = get_settings()
    if not settings.detect_links:
        return
    text = _event_link_text(ev).strip()
    if not text or _RN_COMMAND_RE.match(text):
        return
    urls = extract_urls(text)
    if urls:
        await _handle_urls(bot, ev, urls, notify=False)

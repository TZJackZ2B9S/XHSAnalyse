"""小红书链接解析、媒体准备与转发。"""

import asyncio

import httpx

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from xhs_core.parse.note import NoteParseError, parse_note
from xhs_core.parse.urls import extract_urls, is_short_link, extract_note_id
from xhs_core.parse.models import NoteResult
from xhs_core.media.pipeline import (
    cleanup_media,
    prepare_media,
    build_info_text,
    build_forward_message,
)

from ..xhs_config.xhs_config import get_settings

sv = SV("小红书解析")

_processing_users: set[str] = set()
_processing_lock = asyncio.Lock()


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


async def _parse_first_valid(client: httpx.AsyncClient, urls: tuple[str, ...], cookie: str) -> NoteResult:
    last_error: NoteParseError | None = None
    for url in _sorted_urls(urls):
        try:
            return await parse_note(client, url, cookie)
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
    processing_ids: list[str] | None = None
    try:
        if notify:
            processing_ids = await bot.send("检测到小红书链接，正在解析...", wait_recall=True)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True) as client:
            result = await _parse_first_valid(client, urls, settings.cookie)
            media = await prepare_media(client, result, settings)
        if not media:
            if notify:
                await bot.send("未能下载任何媒体，请稍后重试")
            return False
        info_text = build_info_text(result, media)
        forward = build_forward_message(result, media, info_text)
        try:
            await bot.send(forward)
        finally:
            cleanup_media(media)
        await bot.unsend(processing_ids)
        return True
    except (NoteParseError, httpx.HTTPError) as error:
        logger.warning(f"[XHSAnalyse] 解析失败：{error}")
        if notify:
            await bot.send(str(error))
        return False
    except Exception:
        logger.exception("[XHSAnalyse] 处理链接时发生未预期异常")
        if notify:
            await bot.send("小红书解析失败，请稍后重试")
        return False
    finally:
        await _release_processing(ev.user_id)


@sv.on_command(
    ("小红书", "xhs", "xhs解析", "xhs下载"),
    block=True,
    to_ai="""解析小红书分享链接或笔记链接，下载无水印图片、视频和 Live 图。
当用户发送小红书链接并要求解析、下载或去水印时调用。

Args:
    text: 小红书 xhslink.com/xhslink.cn 分享链接或 xiaohongshu.com 笔记链接。
""",
    covers=["小红书笔记解析", "小红书无水印下载", "小红书视频图片"],
    aliases=["小红书·解析链接", "小红书·下载笔记", "XHS·去水印"],
)
async def xhs_parse(bot: Bot, ev: Event) -> None:
    urls = extract_urls(ev.text)
    if not urls:
        await bot.send("请发送小红书分享链接或笔记链接")
        return
    await _handle_urls(bot, ev, urls, notify=True)


@sv.on_message()
async def xhs_detect_links(bot: Bot, ev: Event) -> None:
    settings = get_settings()
    if not settings.detect_links:
        return
    text = ev.raw_text.strip()
    if not text or text.startswith(("小红书 ", "xhs ", "xhs解析 ", "xhs下载 ")):
        return
    urls = extract_urls(text)
    if urls:
        await _handle_urls(bot, ev, urls, notify=False)

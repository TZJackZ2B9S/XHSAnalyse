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
from ..utils.parse.note import (
    NoteParseError,
    user_hint,
    parse_first_valid,
    should_report_failure,
    apply_refreshed_cookie,
)
from ..utils.parse.urls import extract_urls, extract_note_urls
from ..utils.parse.message import event_link_text
from ..utils.parse.session import extract_web_session, read_activated_session
from ..utils.media.pipeline import (
    PreparedMedia,
    cleanup_media,
    prepare_media,
    build_info_text,
    build_note_message,
    build_media_message,
    select_media_for_delivery,
)
from ..xhs_config.xhs_config import get_settings

sv = SV("小红书解析")

_processing_users: set[str] = set()
_processing_lock = asyncio.Lock()
_RN_COMMAND_RE = re.compile(r"^rn(?:\s|https?://|$)", re.IGNORECASE)

_UNEXPECTED_HINT = "❌ 解析小红书链接时出错，请稍后重试，详情见 Core 日志。"


def _persist_cookie(cookie: str) -> None:
    """把激活到的新 web_session 写回插件配置，立即生效并持久化。"""

    from ..xhs_config.xhs_config import XHSAnalyseConfig

    if XHSAnalyseConfig.set_config("cookie", cookie):
        logger.info("[XHSAnalyse] 新的 web_session 已写回插件配置")
    else:
        logger.warning("[XHSAnalyse] web_session 写回插件配置失败")


def _persist_refreshed_session(cookie: str, *, activated_before: str) -> str:
    """把本次解析期间激活到的新会话写回配置，返回应继续使用的 Cookie。

    两个条件：配置里本来就有 Cookies，且缓存里的会话**确实是本次解析期间**
    产生的（与解析前记录的 ``activated_before`` 不同）。前者保证无 Cookies
    模式激活到的游客会话只留在本地缓存，不会把用户显式留空的配置改成带
    Cookies 模式；后者保证用户手工导入的新 Cookies 正常生效时，不会被缓存
    里的旧激活值顶掉，也保证只有真正发生的续期才会改动配置。
    """

    if not cookie:
        return cookie
    current = read_activated_session()
    if not current or current == activated_before:
        return cookie
    new_cookie = apply_refreshed_cookie(cookie)
    if new_cookie is None:
        return cookie
    _persist_cookie(new_cookie)
    return new_cookie


async def _claim_processing(user_id: str) -> bool:
    async with _processing_lock:
        if user_id in _processing_users:
            return False
        _processing_users.add(user_id)
        return True


async def _release_processing(user_id: str) -> None:
    async with _processing_lock:
        _processing_users.discard(user_id)


async def _handle_urls(
    bot: Bot,
    ev: Event,
    urls: tuple[str, ...],
    *,
    notify: bool,
) -> bool:
    settings = get_settings()
    cookie = settings.cookie
    # 无 Cookies 模式同样要做会话保障：4K 档位依赖有效 web_session，而游客
    # 会话不需要登录即可激活，所以这里不能因为 cookie 为空就跳过续期逻辑。
    refresh_cookie = settings.auto_refresh_session
    if not await _claim_processing(ev.user_id):
        # 自动解析路径保持静默：群里连发多条链接时逐条提示只会变成刷屏。
        if notify:
            await bot.send("⏳ 正在处理上一条小红书链接，请稍后重试。")
        return False
    processing_ids: list[str | int] | None = None
    media: tuple[PreparedMedia, ...] = ()
    card_task: asyncio.Task[bool] | None = None
    card_sent = False
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
            # 解析前的缓存快照：用来判断本次解析期间是否真的激活过新会话。
            activated_before = read_activated_session()
            result = await parse_first_valid(
                client,
                urls,
                cookie,
                prefer_original_image=settings.prefer_original_image,
                max_video_height=settings.target_video_height,
                prefer_hdr_video=settings.prefer_hdr_video,
                fallback_without_cookie=settings.fallback_without_cookie,
                enrich_profile=settings.render_card,
                output_logs=settings.output_logs,
                refresh_session=refresh_cookie,
                proxy=settings.proxy,
            )
            if refresh_cookie:
                cookie = _persist_refreshed_session(cookie, activated_before=activated_before)
            if settings.output_logs:
                # 判据是「本次真的带上了被承认的会话」，而不是「配置里有没有
                # 内容」：Cookie 串可能只含 a1 等设备键，那种情况下解析其实是
                # 匿名进行的。
                cookie_state = "已使用" if extract_web_session(cookie) and not result.cookie_expired else "未使用"
                logger.info(
                    f"[XHSAnalyse] 笔记解析成功：{result.note_id}，类型={result.type}，媒体数={len(result.media)}，"
                    f"Cookies={cookie_state}"
                )
                if result.type == "video":
                    video = next((item for item in result.media if item.is_video), None)
                    if video is not None:
                        logger.info(
                            f"[XHSAnalyse] 视频档位选择：目标 {settings.video_quality} "
                            f"({settings.target_video_height}p)，实际 {result.video_quality or '未知'}，"
                            f"流尺寸 {video.width}x{video.height}"
                        )

            async def render_and_send_card(cover: PreparedMedia) -> bool:
                card = await render_note_card(
                    result,
                    cover.path,
                    client,
                    render_scale=settings.render_scale,
                    no_cookie_mode=not extract_web_session(cookie) or result.cookie_expired,
                )
                if card is None:
                    return False
                await bot.send(build_note_message("", card))
                return True

            cover_index = next(
                (index for index, item in enumerate(result.media) if not item.is_video),
                None,
            )

            async def on_media_ready(prepared: PreparedMedia) -> None:
                nonlocal card_task
                if (
                    not settings.render_card
                    or prepared.is_video
                    or prepared.index != cover_index
                    or card_task is not None
                ):
                    return
                card_task = asyncio.create_task(
                    render_and_send_card(prepared),
                    name=f"XHSAnalyse:render-card:{result.note_id}",
                )

            media = await prepare_media(client, result, settings, on_media_ready=on_media_ready)
            if settings.render_card and card_task is None:
                fallback_cover = next((item for item in media if not item.is_video), None)
                if fallback_cover is not None:
                    card_task = asyncio.create_task(
                        render_and_send_card(fallback_cover),
                        name=f"XHSAnalyse:render-card:{result.note_id}",
                    )
            if card_task is not None:
                card_sent = await card_task
            if settings.output_logs:
                logger.info(f"[XHSAnalyse] 媒体准备完成：成功 {len(media)}/{len(result.media)} 个")
        if not media:
            # 媒体全下失败通常是 CDN 或磁盘问题，自动路径静默，与失败发言策略一致。
            if notify:
                await bot.send("❌ 笔记解析成功，但没能下载到任何媒体，请稍后重试。")
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
        if not card_sent:
            await bot.send(build_note_message(info_text))
        delivery_media = select_media_for_delivery(result, media)
        await bot.send(
            build_media_message(
                delivery_media,
                video_send_type=settings.video_send_type,
                image_send_type=settings.image_send_type,
            )
        )
        return True
    except (NoteParseError, httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
        logger.warning(f"[XHSAnalyse] 解析失败：{error}")
        # 自动路径（notify=False）只报与链接本身有关的失败：非笔记链接、
        # 本地磁盘/权限问题都在此静默，避免群里刷屏。
        if should_report_failure(error, notify=notify):
            # 非解析类异常（代理、编码等）的原文可能含凭据或服务器路径，
            # 只写日志，群里给固定文案。
            await bot.send(user_hint(error) if isinstance(error, NoteParseError) else _UNEXPECTED_HINT)
        return False
    finally:
        cleanup_media(
            media,
            video_send_type=settings.video_send_type,
            image_send_type=settings.image_send_type,
        )
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
    text = event_link_text(ev).strip()
    if not text or _RN_COMMAND_RE.match(text):
        return
    # 主页、搜索页等非笔记链接在 xiaohongshu.com 之下同样能被正则匹配到，
    # 自动路径必须先把它们滤掉，否则群里每贴一次主页链接就报一次错。
    urls = extract_note_urls(text)
    if urls:
        await _handle_urls(bot, ev, urls, notify=False)

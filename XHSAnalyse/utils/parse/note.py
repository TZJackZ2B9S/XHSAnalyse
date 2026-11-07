"""小红书笔记页解析。"""

import re
import json
import math
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from urllib.parse import unquote

import httpx

from gsuid_core.logger import logger

from .urls import explore_url, is_short_link, extract_note_id
from .video import get_best_video_url
from .models import MediaItem, NoteResult
from .session import (
    ensure_web_session,
    extract_web_session,
    replace_web_session,
    activate_web_session,
    read_activated_session,
    record_session_verdict,
    apply_refreshed_session_needed,
)
from .streams import (
    text,
    field,
    as_text,
    as_array,
    as_object,
    is_hdr_stream,
    normalize_url,
    select_live_stream,
)

UA_MOBILE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.6778.200 Safari/537.36 HeyTapBrowser/51.8.8"
)
# 小红书会根据设备 UA 下发不同的视频流清单。移动端页面通常只暴露 720p，
# 桌面端页面才会在 ``media`` / ``mediaV2`` 中附带 1080p、2K 和 4K 签名流。
# 使用桌面 UA 与网页端请求保持一致，清晰度选择才能作用于完整候选集合。
UA_NOTE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
ACCEPT_MOBILE = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
_IMAGE_TOKEN_RE = re.compile(r"/\d+/[a-f0-9]+/([^!]+)")
_CHINA_TZ = timezone(timedelta(hours=8))


CODE_NOTE_ID = "NOTE_ID"
CODE_LOGIN = "NEED_LOGIN"
CODE_COOKIE = "NEED_COOKIE"
CODE_CAPTCHA = "NEED_CAPTCHA"
CODE_UNAVAILABLE = "NOTE_UNAVAILABLE"
CODE_UNKNOWN = "UNKNOWN"

# 页面类别：由 classify_page 判定，用于取代「拿展示文案当控制流」的旧写法。
_PAGE_OK = "OK"
_PAGE_LOGIN = "LOGIN"
_PAGE_CAPTCHA = "CAPTCHA"
_PAGE_UNAVAILABLE = "UNAVAILABLE"


class NoteParseError(RuntimeError):
    """笔记解析失败。

    ``code`` 供上层判断该失败是否值得继续尝试其它候选链接，
    避免把面向用户的展示文案当成控制流判别式。
    """

    def __init__(self, message: str, code: str) -> None:
        # code 必填：CODE_UNKNOWN 属于可跳过的错误，设默认值会让未标码的
        # 新失败点静默降级为「可跳过」，在候选遍历里被吞掉。
        super().__init__(message)
        self.code = code


# 「这条候选链接本身取不到内容」——多链接场景下值得继续尝试下一个候选。
# 登录墙与单条下架都属此类：同一条消息里可能混有仅该条受限的笔记，其余候选仍可解析。
# CODE_COOKIE 也归入：无 token 直链缺配置是候选自身形态问题，不该阻断同消息里的短链。
# 风控（captcha）不列入：它是出口 IP 级别的限制，继续重试只会加重风控。
SKIPPABLE_CODES = frozenset({CODE_NOTE_ID, CODE_LOGIN, CODE_COOKIE, CODE_UNAVAILABLE, CODE_UNKNOWN})

# 全部候选都失败时展示哪条错误。取信息量最大的一条，避免同一组候选
# 仅因用户书写顺序不同就给出不同结论。
# CODE_CAPTCHA 不列入：它不可跳过，parse_first_valid 会直接上抛，
# 永远不会进入候选汇总。
ERROR_PRIORITY = (
    CODE_LOGIN,
    CODE_COOKIE,
    CODE_UNAVAILABLE,
    CODE_NOTE_ID,
    CODE_UNKNOWN,
)


def pick_error(errors: list[NoteParseError]) -> NoteParseError:
    """按固定优先级挑选最值得展示的失败原因。"""

    for code in ERROR_PRIORITY:
        for error in errors:
            if error.code == code:
                return error
    return errors[0]


def sorted_urls(urls: tuple[str, ...]) -> tuple[str, ...]:
    """按「含笔记 ID 的直链 > 分享短链 > 其它」排序候选。"""

    return tuple(
        sorted(
            urls,
            key=lambda url: 0 if extract_note_id(url) else 1 if is_short_link(url) else 2,
        )
    )


async def parse_first_valid(
    client: httpx.AsyncClient,
    urls: tuple[str, ...],
    cookie: str = "",
    *,
    prefer_original_image: bool = True,
    max_video_height: int = 0,
    prefer_hdr_video: bool = True,
    fallback_without_cookie: bool = True,
    enrich_profile: bool = False,
    output_logs: bool = False,
    refresh_session: bool = True,
    proxy: str = "",
) -> NoteResult:
    """依次尝试候选链接，返回第一个解析成功的结果。

    只有「这条候选链接自身取不到内容」才继续尝试下一个；命中出口 IP 级风控时
    直接上抛，避免继续冲击风控。全部失败时按 :data:`ERROR_PRIORITY` 挑选
    最值得展示的错误，使结果不随用户书写顺序变化。
    """

    errors: list[NoteParseError] = []
    # 会话刷新只做一次，贯穿所有候选与作者画像请求：放在候选循环前可以
    # 避免候选间重复检测/重复激活，也保证 enrich 用的是新会话。
    # 空 Cookie 同样要进这条路径：4K 档位依赖有效 web_session，而游客会话
    # 不需要登录即可激活，``ensure_web_session`` 会走无 Cookie 分支自己处理。
    effective_cookie = cookie
    if refresh_session:
        effective_cookie, refreshed = await ensure_web_session(client, cookie, proxy=proxy)
        if refreshed:
            logger.info("[XHSAnalyse] web_session 已自动续期，使用新会话继续解析")
    for url in sorted_urls(urls):
        try:
            if output_logs:
                logger.info(f"[XHSAnalyse] 开始解析链接：{url[:160]}")
            result = await parse_note(
                client,
                url,
                effective_cookie,
                prefer_original_image=prefer_original_image,
                max_video_height=max_video_height,
                prefer_hdr_video=prefer_hdr_video,
                fallback_without_cookie=fallback_without_cookie,
                session_recovery=refresh_session,
                proxy=proxy,
            )
            if enrich_profile:
                result = await enrich_author_profile(client, result, cookie=effective_cookie)
            if refresh_session and extract_web_session(effective_cookie):
                # 把真实解析结果回灌给会话管理层：首页判据在登录墙下不可用，
                # 只有解析本身能证明当前会话到底有没有被服务端承认。判据用
                # 「确实带了 web_session」而非 Cookie 串非空——后者可能只有
                # a1 等设备键，那种解析是匿名进行的，不能当作会话有效。
                record_session_verdict(not result.cookie_expired)
            return result
        except NoteParseError as error:
            errors.append(error)
            # 失败不回灌会话判定：``CODE_LOGIN`` 既可能是 Cookies 被拒，也可能是
            # 这条笔记本身要求登录（此时匿名重试同样撞墙）。后者与会话有效性
            # 无关，回灌成「失效」会白拉一次浏览器。会话失效的唯一可靠证据来自
            # 成功路径——带 Cookies 被拒、匿名才取到（``cookie_expired``）。
            if error.code not in SKIPPABLE_CODES:
                raise
            logger.warning(f"[XHSAnalyse] 跳过无效候选链接：{url[:120]}（{error}）")
    if not errors:
        raise NoteParseError("无法提取笔记 ID", CODE_NOTE_ID)
    raise pick_error(errors)


# 仅收列措辞需要覆盖 _page_error 的码；其余码的 message 本身就是面向用户的文案，
# 由 user_hint 统一补上前缀，避免两处各写一份、其中一份永不展示。
FAILURE_HINTS = {
    CODE_NOTE_ID: "链接里没有识别到小红书笔记 ID，请确认链接是否完整。",
    CODE_COOKIE: "这条链接需要先在插件里配置小红书 Cookies 才能解析，请改用小红书 App 的分享链接再试。",
    CODE_UNKNOWN: "未能从这条链接取到笔记数据，请确认链接是否有效。",
}


def _is_automatic_failure_worth_reporting(code: str) -> bool:
    """自动解析路径（非用户显式命令）是否值得在群里报这条失败。

    分享短链展开后可能落到主页、搜索页或活动页，这类链接本就不是笔记，
    自动路径应当静默跳过，否则群里每贴一次就会收到一条错误提示。

    风控（``CODE_CAPTCHA``）是出口 IP 级的问题，一旦命中，群里每条带链接的
    消息都会得到同一条提示，因此自动路径保持静默；显式命令仍然照常提示。
    """

    return code not in {CODE_NOTE_ID, CODE_UNKNOWN, CODE_CAPTCHA}


def should_report_failure(error: BaseException, *, notify: bool) -> bool:
    """判断这次失败是否要在群里发声。

    显式命令（``notify=True``）一律发声，用户需要知道结果。自动解析路径只报
    与链接内容有关的失败：磁盘满、权限不足、代理不可用这类与链接无关的环境
    问题会持续存在，对群里的每条消息都重复播报没有意义。

    注意 :class:`TimeoutError` 是 :class:`OSError` 的子类，因此自动路径下的
    超时同样静默——这类问题的排查入口是 Core 日志。
    """

    if notify:
        return True
    if isinstance(error, NoteParseError):
        return _is_automatic_failure_worth_reporting(error.code)
    if isinstance(error, (OSError, httpx.TransportError)):
        return False
    return True


def user_hint(error: NoteParseError) -> str:
    """把解析失败翻译成面向群聊的提示文案。"""

    hint = FAILURE_HINTS.get(error.code)
    return f"❌ {hint if hint is not None else error}"


async def resolve_short_link(client: httpx.AsyncClient, url: str, cookie: str = "") -> str:
    headers = {"User-Agent": UA_MOBILE}
    if cookie:
        headers["Cookie"] = cookie
    response = await client.get(url, headers=headers, follow_redirects=False)
    location = response.headers.get("location")
    if location is None:
        return url
    joined = str(httpx.URL(url).join(location))
    return normalize_url(joined) or joined


def build_ci_image_url(url: str) -> tuple[str, str] | None:
    """把带展示参数的 CDN 图片地址还原为 CI 原图地址。"""

    match = _IMAGE_TOKEN_RE.search(url)
    if not match or not match.group(1):
        return None
    token = match.group(1)
    return f"https://ci.xiaohongshu.com/{token}", token


def _extract_initial_state(document: str) -> dict[str, object] | None:
    marker = document.find("__INITIAL_STATE__")
    if marker < 0:
        return None
    end = document.find("</script>", marker)
    if end < 0:
        return None
    snippet = document[marker:end]
    assignment = snippet.find("={")
    if assignment < 0:
        return None
    payload = (
        snippet[assignment + 1 :]
        .replace(":undefined", ":null")
        .replace(": undefined", ": null")
        .replace("new Map([])", "{}")
        .replace("new Set([])", "{}")
    )
    try:
        state = json.loads(payload)
    except json.JSONDecodeError:
        return None
    state_obj = as_object(state)
    return state_obj


def extract_note_from_html(document: str) -> dict[str, object] | None:
    """从 ``__INITIAL_STATE__`` 中取出笔记对象。

    笔记不可用时小红书仍会返回 ``noteDetailMap`` 条目，但其中 ``note`` 是空对象，
    外层只剩 ``comments``/``currentTime`` 两个壳字段。因此这里要求取出的对象
    确实带笔记标识或媒体列表，否则视为未取到，交由调用方走失败分支。
    """

    state_obj = _extract_initial_state(document)
    if state_obj is None:
        return None

    direct = field(field(field(state_obj, "noteData"), "data"), "noteData")
    direct_obj = as_object(direct)
    if direct_obj is not None and text(direct_obj, "noteId"):
        return _attach_profile_user(direct_obj, state_obj)

    note_map = as_object(field(field(state_obj, "note"), "noteDetailMap"))
    if note_map:
        first = next(iter(note_map.values()), None)
        first_obj = as_object(first)
        if first_obj is not None:
            candidate = as_object(field(first_obj, "note"))
            # 空 dict 为假值，`or` 会退回外层容器；这里显式区分「没有 note 字段」
            # 与「note 是空对象」，后者说明笔记不可用。
            if candidate is not None:
                return _attach_profile_user(candidate, state_obj) if _is_real_note(candidate) else None
            if _is_real_note(first_obj):
                return _attach_profile_user(first_obj, state_obj)
    return None


def _is_real_note(note: dict[str, object]) -> bool:
    """判断提取出的对象是否是真正的笔记，而不是不可用时的空壳。"""

    if text(note, "noteId"):
        return True
    return bool(as_array(field(note, "imageList"))) or bool(field(note, "video"))


def format_timestamp(value: object) -> str:
    raw = as_text(value).strip()
    if not raw:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    try:
        timestamp = float(raw)
    except ValueError:
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        if timestamp < 1_000_000_000_000:
            timestamp *= 1000
        try:
            moment = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return ""
    return moment.astimezone(_CHINA_TZ).strftime("%Y-%m-%d %H:%M")


def _publish_time(note: dict[str, object]) -> str:
    for key in ("time", "lastUpdateTime", "createTime", "publishTime", "postTime"):
        if key in note:
            formatted = format_timestamp(note[key])
            if formatted:
                return formatted
    return ""


def _image_url(image: dict[str, object]) -> str:
    return normalize_url(field(image, "urlDefault", "url_default", "url", "urlPre", "url_pre"))


def _avatar_url(user: dict[str, object]) -> str:
    avatar = field(user, "avatar", "avatarUrl", "avatar_url", "image")
    avatar_obj = as_object(avatar)
    if avatar_obj is not None:
        avatar = field(avatar_obj, "urlDefault", "url_default", "url", "urlPre", "url_pre")
    return normalize_url(avatar)


def _count(value: object, *keys: str) -> str:
    return as_text(field(value, *keys)).strip()


def _profile_count(value: object, *keys: str) -> str:
    count = _count(value, *keys)
    return count if count not in {"", "-", "--", "—"} else ""


def _precise_profile_counts(payload: object) -> dict[str, str]:
    """读取作者资料接口返回的统计数值。

    平台对部分账号只下发模糊文案（``100+``、``1千+``、``1万+``），此时
    原样保留模糊值展示，强求精确整数反而会把这类账号的资料整体丢掉。
    """

    candidates = [payload, field(payload, "data"), field(field(payload, "data"), "user")]
    mapping = {
        "follows": ("follows", "followingCount", "following_count", "followCount"),
        "fans": ("fans", "fansCount", "fans_count", "followerCount", "follower_count"),
        "like_and_collect": (
            "interactions",
            "interaction",
            "likeAndCollect",
            "like_and_collect",
            "likeCollectCount",
            "like_collect_count",
        ),
    }
    values: dict[str, str] = {}
    for candidate in candidates:
        obj = as_object(candidate)
        if obj is None:
            continue
        for name, keys in mapping.items():
            if name in values:
                continue
            raw = field(obj, *keys)
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                continue
            text_value = str(raw).strip()
            # 精确整数或平台模糊文案（100+ / 1千+ / 10.5万 / 1.2k）都可接受；
            # 其余占位符（-、--）与解析不出的形态继续丢弃。
            if re.fullmatch(r"\d[\d,]*", text_value) or re.fullmatch(r"\d+(?:\.\d+)?[万千kKwW]?\+?", text_value):
                values[name] = text_value.replace(",", "")
    return values


def extract_author_profile_from_html(document: str) -> dict[str, str]:
    """从作者主页状态中提取公开资料统计。"""

    state = _extract_initial_state(document)
    if state is None:
        return {}
    user_state = as_object(field(state, "user")) or {}
    page_data = as_object(field(user_state, "userPageData")) or {}
    basic_info = as_object(field(page_data, "basicInfo")) or {}
    values: dict[str, str] = {}
    red_id = as_text(field(basic_info, "redId", "red_id")).strip()
    if red_id:
        values["red_id"] = red_id
    interactions = as_array(field(page_data, "interactions")) or []
    for raw_item in interactions:
        item = as_object(raw_item)
        if item is None:
            continue
        kind = as_text(field(item, "type")).strip()
        name = as_text(field(item, "name")).strip()
        count = _profile_count(item, "count", "i18nCount")
        if not count:
            continue
        if kind == "follows" or name == "关注":
            values["follows"] = count
        elif kind == "fans" or name == "粉丝":
            values["fans"] = count
        elif name == "获赞与收藏" or kind == "interaction":
            values["like_and_collect"] = count
    return values


def _attach_profile_user(note: dict[str, object], state: dict[str, object]) -> dict[str, object]:
    profile = as_object(field(state, "profile"))
    profile_user = as_object(field(profile, "userInfo", "user_info"))
    if profile_user is None:
        return note
    enriched = dict(note)
    enriched["_profileUserInfo"] = profile_user
    return enriched


def _live_stream(image: dict[str, object]) -> object | None:
    marker = field(image, "livePhoto", "live_photo")
    if isinstance(marker, bool):
        if not marker:
            return None
        live = None
    else:
        live = as_object(marker)
        if marker is None and not field(image, "livePhotoUrl", "live_photo_url", "liveVideoUrl", "live_video_url"):
            return None
        if isinstance(marker, str) and marker.strip().lower() not in {"1", "true", "yes"}:
            return None
    if live is not None:
        live_media = as_object(field(live, "media"))
        stream = field(live_media, "stream") if live_media is not None else None
        if stream is not None:
            return stream
        stream = field(live, "stream")
        if stream is not None:
            return stream
    media = as_object(field(image, "media"))
    stream = field(media, "stream") if media is not None else None
    return stream if stream is not None else field(image, "stream")


def _image_is_hdr(image: dict[str, object], source_url: str) -> bool:
    """从 UHDR 路径、图片元数据和图片流标记识别 HDR 静态图。"""

    urls = [source_url, *(as_text(field(image, key)) for key in ("url", "urlDefault", "urlPre"))]
    if any(re.search(r"(?:notes_)?uhdr|hdr", value, re.IGNORECASE) for value in urls if value):
        return True
    if is_hdr_stream(image):
        return True
    stream = as_object(field(image, "stream"))
    if stream is not None:
        for value in stream.values():
            entries = as_array(value) or [value]
            if any(is_hdr_stream(entry) for entry in entries):
                return True
    info_list = as_array(field(image, "infoList", "info_list")) or []
    return any(is_hdr_stream(info) for info in info_list)


def collect_media(
    note_id: str,
    note: dict[str, object],
    *,
    prefer_original_image: bool = True,
    max_video_height: int = 0,
    prefer_hdr_video: bool = True,
    share_url: str = "",
) -> NoteResult:
    title = as_text(field(note, "displayTitle", "title")) or "未知标题"
    user = as_object(field(note, "user")) or {}
    author = as_text(field(user, "nickname", "nickName", "nick_name")) or "未知作者"
    profile = as_object(field(note, "_profileUserInfo", "profileUserInfo", "profile_user_info")) or {}
    profile_container = as_object(field(note, "profile"))
    profile = as_object(field(profile_container, "userInfo", "user_info")) or profile_container or profile
    interact = as_object(field(note, "interactInfo", "interact_info", "interact")) or {}
    note_type = as_text(field(note, "type"))
    images = as_array(field(note, "imageList")) or []
    media: list[MediaItem] = []

    for raw_image in images:
        image = as_object(raw_image)
        if image is None:
            continue
        source_url = _image_url(image)
        if not source_url:
            continue
        ci = build_ci_image_url(source_url) if prefer_original_image else None
        url = ci[0] if ci else source_url
        live_choice = select_live_stream(_live_stream(image))
        if live_choice is not None and live_choice.url:
            media.append(MediaItem(url=url, is_live=True, live_url=live_choice.url, is_hdr=live_choice.is_hdr))
        else:
            media.append(MediaItem(url=url, is_hdr=_image_is_hdr(image, source_url)))

    has_main_video = False
    video_quality: str | None = None
    if note_type == "video" and len(media) <= 1:
        video = get_best_video_url(
            note,
            max_height=max_video_height,
            prefer_hdr=prefer_hdr_video,
        )
        if video is not None:
            media.append(
                MediaItem(
                    url=video.url,
                    is_video=True,
                    quality=video.quality,
                    is_hdr=video.is_hdr,
                    width=video.width,
                    height=video.height,
                )
            )
            has_main_video = True
            video_quality = video.quality

    return NoteResult(
        note_id=note_id,
        title=title,
        author=author,
        desc=as_text(field(note, "desc")),
        ip_location=as_text(field(note, "ipLocation", "ip_location")).strip(),
        publish_time=_publish_time(note),
        type="video" if has_main_video else "image",
        video_quality=video_quality,
        media=tuple(media),
        target_video_height=max_video_height,
        liked_count=_count(interact, "likedCount", "liked_count", "likeCount", "like_count"),
        comment_count=_count(interact, "commentCount", "comment_count"),
        collected_count=_count(interact, "collectedCount", "collected_count", "collectCount", "collect_count"),
        share_count=_count(interact, "shareCount", "share_count"),
        author_id=as_text(field(user, "userId", "user_id", "redId", "red_id", "id")),
        author_avatar=_avatar_url(user),
        share_url=share_url,
        author_follows=(
            _profile_count(profile, "follows", "followingCount", "following_count", "followCount")
            or _profile_count(user, "follows", "followingCount", "following_count", "followCount")
        ),
        author_fans=(
            _profile_count(profile, "fans", "fansCount", "fans_count", "followerCount", "follower_count")
            or _profile_count(user, "fans", "fansCount", "fans_count", "followerCount", "follower_count")
        ),
        author_like_and_collect=(
            _profile_count(
                profile,
                "likeAndCollect",
                "like_and_collect",
                "likeCollectCount",
                "like_collect_count",
            )
            or _profile_count(
                user,
                "likeAndCollect",
                "like_and_collect",
                "likeCollectCount",
                "like_collect_count",
            )
        ),
    )


async def _enrich_author_profile(
    client: httpx.AsyncClient,
    result: NoteResult,
    *,
    cookie: str,
) -> NoteResult:
    profile_url = httpx.URL(f"https://www.xiaohongshu.com/user/profile/{result.author_id}")
    source_url = httpx.URL(result.share_url) if result.share_url else None
    if source_url is not None:
        params = [
            (key, value) for key, value in source_url.params.multi_items() if key in {"xsec_token", "xsec_source"}
        ]
        if params:
            profile_url = profile_url.copy_with(params=params)
    headers = {
        "User-Agent": UA_MOBILE,
        "Accept": ACCEPT_MOBILE,
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        response = await client.get(
            profile_url,
            headers=headers,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        logger.debug(f"[XHSAnalyse] 作者主页资料获取失败：{error}")
        return result
    profile = extract_author_profile_from_html(response.text)
    if any("+" in profile.get(key, "") for key in ("follows", "fans", "like_and_collect")):
        api_url = "https://www.xiaohongshu.com/api/sns/web/v1/user/otherinfo"
        api_params: dict[str, str] = {"target_user_id": result.author_id}
        if source_url is not None:
            api_params.update(
                {key: value for key, value in source_url.params.multi_items() if key in {"xsec_token", "xsec_source"}}
            )
        try:
            api_response = await client.get(api_url, params=api_params, headers=headers)
        except httpx.HTTPError:
            api_response = None
        if api_response is not None and api_response.is_success:
            try:
                profile.update(_precise_profile_counts(api_response.json()))
            except ValueError:
                pass
    if not profile:
        return result
    return replace(
        result,
        author_red_id=profile.get("red_id", result.author_red_id),
        author_follows=profile.get("follows", result.author_follows),
        author_fans=profile.get("fans", result.author_fans),
        author_like_and_collect=profile.get("like_and_collect", result.author_like_and_collect),
    )


async def enrich_author_profile(
    client: httpx.AsyncClient,
    result: NoteResult,
    *,
    cookie: str = "",
) -> NoteResult:
    """补充作者主页资料，并优先采用接口返回的精确整数统计。"""

    if not result.author_id:
        return result
    return await _enrich_author_profile(client, result, cookie=cookie)


def apply_refreshed_cookie(cookie: str) -> str | None:
    """若会话管理模块刚激活过新会话，把它合并回给定 Cookie 串。

    返回 ``None`` 表示没有需要写回的新会话；调用方据此决定是否落盘。
    """

    if not apply_refreshed_session_needed(cookie):
        return None
    return replace_web_session(cookie, read_activated_session())


async def _recover_session_and_retry(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    *,
    note_id: str,
    resolved_url: str,
    prefer_original_image: bool,
    max_video_height: int,
    prefer_hdr_video: bool,
    proxy: str,
) -> NoteResult | None:
    """Cookies 被拒时续期一次会话并重试同一页面。

    会话失效是「带 Cookies 取不到笔记」最常见的原因，而续期只需一次浏览器
    激活。成功时返回用新会话解析出的结果（``cookie_expired`` 保持 False：
    这次请求确实带上了被承认的会话），失败返回 ``None``，调用方继续走原有
    的匿名回落路径，因此这条补救不会改变任何既有失败行为。
    """

    session = await activate_web_session(proxy)
    if not session:
        return None
    if session == extract_web_session(headers["Cookie"]):
        # 激活返回的仍是同一个会话：重试不会得到不同结果，省掉这次请求。
        logger.debug("[XHSAnalyse] 激活返回的会话与当前一致，跳过重试")
        return None
    # 只替换 web_session，保留 a1 / webId / gid 等其余键：它们参与小红书的
    # 设备与风控画像，整串丢掉会让同一份 Cookies 在中途变成另一个设备。
    headers["Cookie"] = replace_web_session(headers["Cookie"], session)
    response = await client.get(base_url, headers=headers)
    if effective_page(response) != _PAGE_OK:
        return None
    note = extract_note_from_html(response.text)
    if note is None:
        return None
    logger.info("[XHSAnalyse] web_session 已自动续期，用新会话重新取回笔记")
    return collect_media(
        note_id,
        note,
        prefer_original_image=prefer_original_image,
        max_video_height=max_video_height,
        prefer_hdr_video=prefer_hdr_video,
        share_url=resolved_url,
    )


async def parse_note(
    client: httpx.AsyncClient,
    input_url: str,
    cookie: str = "",
    *,
    prefer_original_image: bool = True,
    max_video_height: int = 0,
    prefer_hdr_video: bool = True,
    fallback_without_cookie: bool = True,
    session_recovery: bool = False,
    proxy: str = "",
) -> NoteResult:
    """解析分享短链或笔记链接。

    会话有效性检查由 :func:`parse_first_valid` 统一处理；本函数假设传入的
    ``cookie`` 已经是可用会话，避免候选遍历中重复检测/激活。

    ``session_recovery`` 打开时，带 Cookies 的请求被拒会先续期一次会话并
    重试，续期失败才退回无 Cookies 解析。首页判据失效后这是唯一能证明会话
    状态的证据来源，也让「会话失效 → 立刻换新的 → 继续解析」在同一次解析内
    闭环，而不是把这条笔记降级成 720p。
    """

    resolved_url = await resolve_short_link(client, input_url, cookie) if is_short_link(input_url) else input_url
    note_id = extract_note_id(resolved_url)
    if not note_id:
        raise NoteParseError("无法提取笔记 ID", CODE_NOTE_ID)
    has_token = "xsec_token" in resolved_url
    base_url = explore_url(resolved_url, note_id)
    headers = {
        "User-Agent": UA_NOTE,
        "Accept": ACCEPT_MOBILE,
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if cookie:
        headers["Cookie"] = cookie

    cookie_expired = False
    # 匿名重试后仍撞登录墙，说明这条笔记本身要求登录，与 Cookies 是否有效无关。
    anonymous_login_wall = False
    response: httpx.Response | None = None
    if has_token:
        for attempt in range(2 if cookie else 1):
            response = await client.get(base_url, headers=headers)
            page = effective_page(response)
            if page == _PAGE_OK:
                note = extract_note_from_html(response.text)
                if note is not None:
                    result = collect_media(
                        note_id,
                        note,
                        prefer_original_image=prefer_original_image,
                        max_video_height=max_video_height,
                        prefer_hdr_video=prefer_hdr_video,
                        share_url=resolved_url,
                    )
                    return replace(result, cookie_expired=cookie_expired)
            if page != _PAGE_OK or not cookie:
                break
            if attempt == 0:
                await asyncio.sleep(1)
        if response is not None and cookie and effective_page(response) not in {_PAGE_CAPTCHA, _PAGE_UNAVAILABLE}:
            # 带 Cookies 没能取到笔记，先续期一次会话再重试：会话失效是这条
            # 路径最常见的原因，续期成功就能在同一次解析里拿回高清档位。
            # 续期不属于「无登录回退」——它修复的是 Cookies 本身，所以不受
            # fallback_without_cookie 约束：关掉了匿名回退的用户同样需要它。
            if session_recovery:
                recovered = await _recover_session_and_retry(
                    client,
                    base_url,
                    headers,
                    note_id=note_id,
                    resolved_url=resolved_url,
                    prefer_original_image=prefer_original_image,
                    max_video_height=max_video_height,
                    prefer_hdr_video=prefer_hdr_video,
                    proxy=proxy,
                )
                if recovered is not None:
                    return replace(recovered, cookie_expired=False)
            if not fallback_without_cookie:
                raise _page_error(response, has_token=has_token, cookie=cookie)
            # 续期不可用或没换到会话，再删掉 cookie 重试一次：登录墙是 Cookies
            # 失效的典型表现，必须允许回退，否则「Cookies 失败后无登录回退」
            # 形同虚设。命中风控或笔记已下架时不再匿名重试——两者都与 Cookies
            # 无关，风控下匿名重试还会加重风控。
            headers.pop("Cookie", None)
            response = await client.get(base_url, headers=headers)
            if effective_page(response) == _PAGE_OK:
                note = extract_note_from_html(response.text)
                if note is not None:
                    result = collect_media(
                        note_id,
                        note,
                        prefer_original_image=prefer_original_image,
                        max_video_height=max_video_height,
                        prefer_hdr_video=prefer_hdr_video,
                        share_url=resolved_url,
                    )
                    return replace(result, cookie_expired=True)
            anonymous_login_wall = effective_page(response) == _PAGE_LOGIN

    if not has_token and cookie:
        response = await client.get(base_url, headers=headers)
        if effective_page(response) == _PAGE_OK:
            note = extract_note_from_html(response.text)
            if note is not None:
                return collect_media(
                    note_id,
                    note,
                    prefer_original_image=prefer_original_image,
                    max_video_height=max_video_height,
                    prefer_hdr_video=prefer_hdr_video,
                    share_url=resolved_url,
                )
        # 无 token 直链同样要有匿名回落：否则 Cookies 失效时会误报成
        # 「本轮 Cookies 未能通过校验」，而该链接匿名本来就能取到内容。
        # 会话续期与「无登录回退」相互独立，因此排在 fallback 判定之前。
        if effective_page(response) not in {_PAGE_CAPTCHA, _PAGE_UNAVAILABLE}:
            if session_recovery:
                recovered = await _recover_session_and_retry(
                    client,
                    base_url,
                    headers,
                    note_id=note_id,
                    resolved_url=resolved_url,
                    prefer_original_image=prefer_original_image,
                    max_video_height=max_video_height,
                    prefer_hdr_video=prefer_hdr_video,
                    proxy=proxy,
                )
                if recovered is not None:
                    return replace(recovered, cookie_expired=False)
            if not fallback_without_cookie:
                raise _page_error(response, has_token=has_token, cookie=cookie)
            headers.pop("Cookie", None)
            response = await client.get(base_url, headers=headers)
            if effective_page(response) == _PAGE_OK:
                note = extract_note_from_html(response.text)
                if note is not None:
                    result = collect_media(
                        note_id,
                        note,
                        prefer_original_image=prefer_original_image,
                        max_video_height=max_video_height,
                        prefer_hdr_video=prefer_hdr_video,
                        share_url=resolved_url,
                    )
                    return replace(result, cookie_expired=True)
            anonymous_login_wall = effective_page(response) == _PAGE_LOGIN

    raise _page_error(
        response,
        has_token=has_token,
        cookie=cookie,
        anonymous_login_wall=anonymous_login_wall,
    )


def _page_error(
    response: httpx.Response | None,
    *,
    has_token: bool,
    cookie: str,
    anonymous_login_wall: bool = False,
) -> NoteParseError:
    """把最终页面状态翻译成带错误码的异常。

    ``anonymous_login_wall`` 表示删掉 Cookies 重试后**仍然**撞上登录墙，
    此时换 Cookies 不可能解决问题，必须换一套说辞。
    """

    if response is not None:
        page = effective_page(response)
        if page == _PAGE_LOGIN:
            if cookie and not anonymous_login_wall:
                return NoteParseError(
                    "本轮 Cookies 未能通过小红书校验，请重新获取 Cookies 或改用分享链接",
                    CODE_LOGIN,
                )
            return NoteParseError(
                "该笔记需要登录才能浏览，当前无法取得内容；"
                "可在插件配置中填写小红书 Cookies 后重试，或确认 Cookies 是否仍然有效",
                CODE_LOGIN,
            )
        if page == _PAGE_CAPTCHA:
            return NoteParseError("被小红书风控拦截，请稍后重试或更新 Cookie", CODE_CAPTCHA)
        if page == _PAGE_UNAVAILABLE:
            reported = note_error_message(response.url)
            if reported:
                return NoteParseError(
                    f"小红书提示「{reported}」，这条笔记目前取不到内容（可能已删除、设为私密或仅限特定用户查看）",
                    CODE_UNAVAILABLE,
                )
            return NoteParseError(
                "小红书提示「当前笔记暂时无法浏览」(笔记下架或风控)，请稍后重试",
                CODE_UNAVAILABLE,
            )
    if not has_token and not cookie:
        return NoteParseError(
            "直接链接需要配置 Cookie，请使用分享链接或在小红书配置中填写 Cookie",
            CODE_COOKIE,
        )
    return NoteParseError("无法提取笔记数据，请确认链接有效", CODE_UNKNOWN)


def classify_page(response: httpx.Response) -> str:
    """判定响应属于哪一类页面。

    小红书对需要登录态才能浏览的笔记返回 ``302 → /login``，对命中风控的请求
    返回 ``302 → /website-login/captcha``。这两个标记只出现在重定向后的 URL 上，
    不在正文里，因此必须结合 ``response.url`` 判定，只看正文会全部漏判。
    """

    # 调用方未跟随重定向时，登录墙/验证码/风控页只体现在 status 与 location 上，
    # 必须一并识别，否则会被降级成「链接无效」。
    if response.is_redirect:
        location = response.headers.get("location", "")
        if location:
            return classify_url(response.url.join(location), document="")
    return classify_url(response.url, document=response.text)


def is_forged_unavailable(response: httpx.Response) -> bool:
    """判断「笔记不可用」是否是请求方自己拼出来的。

    ``undertake_note_error`` 与 ``error_code`` 都位于 URL 上，任何人都能自行
    添加。真实不可用时服务端会把我们 302 走（history 非空）、且落地页里取不到
    笔记；伪造时请求直接落在目标页面上、页面里往往还能正常取到笔记。
    命中任一伪造特征即不采信这些参数。

    两个参数必须用同一套判据：只防其中一个的话，另一个就是现成的绕过通道。
    """

    if not (has_note_error(response.url) or has_error_code(response.url)):
        return False
    if extract_note_from_html(response.text) is not None:
        # 笔记明明取得到，标记不可能是平台下发的。
        return True
    return not response.history and not response.is_redirect


def effective_page(response: httpx.Response) -> str:
    """供解析流程使用的页面类别：把伪造的不可用标记降级回正常。

    :func:`classify_page` 保持纯 URL/正文判定，便于单测；
    「标记是否可信」需要响应历史，只在真实请求流程里判断。
    """

    page = classify_page(response)
    if page == _PAGE_UNAVAILABLE and is_forged_unavailable(response):
        return _PAGE_OK
    return page


def classify_url(url: httpx.URL, *, document: str) -> str:
    """按 URL 与正文判定页面类别。跟随/未跟随重定向共用同一套判据。"""

    path = url.path
    if path.rstrip("/").endswith("/login"):
        return _PAGE_LOGIN
    if "website-login/captcha" in path or "website-login/captcha" in document or "请通过验证" in document:
        return _PAGE_CAPTCHA
    if re.search(r"/404/sec", path, re.IGNORECASE) or has_error_code(url) or has_note_error(url):
        return _PAGE_UNAVAILABLE
    return _PAGE_OK


def has_note_error(url: httpx.URL) -> bool:
    """判断链接是否带小红书下发的笔记不可用标记。

    不可用的笔记会 302 到正常的 ``/explore`` 首页并附带
    ``undertake_note_error=该内容暂时无法查看``，落地页本身完全正常，
    只有这个参数能说明笔记取不到。
    """

    return bool(note_error_message(url))


def note_error_message(url: httpx.URL) -> str:
    """取出小红书对这条笔记下发的不可用提示原文。

    该参数在 URL 里被双重编码，httpx 解析 query 时只解一层，这里再解一层
    才能拿到中文文案。文案会被清洗后再用于群聊播报。
    """

    for key, value in url.params.multi_items():
        if key.lower().replace("_", "") != "undertakenoteerror":
            continue
        message = unquote(value).strip()
        if message:
            return _sanitize_platform_message(message)
    return ""


def _sanitize_platform_message(message: str) -> str:
    """限制平台文案的长度与字符，避免被当作群消息注入载体。

    该文案源自 URL，任何人都能构造，直接原样播报等于把机器人的嘴借出去：
    可以塞 @全体成员、CQ 码、换行、外部链接诱导。

    黑名单容易被变体绕过（全角 ＠、零宽字符、无 scheme 的 www.//），
    因此这里保留白名单：只放行中日韩文字、字母数字与少量安全标点。
    """

    single_line = " ".join(message.split())
    # 各类链接形态一并剥离：带 scheme、hxxp 变体、无 scheme 的 www./协议相对
    stripped = re.sub(r"(?:(?:https?|hxxp|ftp)://|www\.|//)\S+", "", single_line)
    kept = [char for char in stripped if _is_safe_message_char(char)]
    return "".join(kept).strip()[:40]


def _is_safe_message_char(char: str) -> bool:
    """平台文案只允许中日韩文字、字母数字与少量标点。

    刻意不放行 ``:`` ``=`` ``[`` ``]``：CQ 码形如 ``[CQ:at,qq=all]``，
    去掉这些字符后适配器就无法再解析成控制指令。
    """

    code = ord(char)
    if char.isalnum():
        return True
    if 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7A3:
        return True
    return char in " ，。、！？；（）《》…—,.!?;()%-"


def has_error_code(url: httpx.URL) -> bool:
    """判断 URL 是否带有表示失败的非零 error_code。

    分享链接常带 ``error_code=0`` 表示无错误，因此按数值比较而非字符串匹配。
    参数名有 ``error_code`` / ``errorCode`` 两种写法，归一后比较；``0``、``00``、
    ``0.0`` 都视为无错误。
    """

    for key, value in url.params.multi_items():
        if key.lower().replace("_", "") != "errorcode":
            continue
        try:
            code_value = float(value.split(";")[0])
        except ValueError:
            # 空值、非数值（`?error_code=`）不对应任何错误码，视为无此参数；
            # 判为「笔记下架」会与事实不符。
            continue
        if math.isfinite(code_value) and code_value != 0:
            return True
    return False

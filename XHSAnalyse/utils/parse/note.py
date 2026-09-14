"""小红书笔记页解析。"""

import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import replace

import httpx

from gsuid_core.logger import logger

from .urls import explore_url, is_short_link, extract_note_id
from .video import get_best_video_url
from .models import MediaItem, NoteResult
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
ACCEPT_MOBILE = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
_IMAGE_TOKEN_RE = re.compile(r"/\d+/[a-f0-9]+/([^!]+)")
_CHINA_TZ = timezone(timedelta(hours=8))


class NoteParseError(RuntimeError):
    """笔记解析失败。"""


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
    )
    try:
        state = json.loads(payload)
    except json.JSONDecodeError:
        return None
    state_obj = as_object(state)
    return state_obj


def extract_note_from_html(document: str) -> dict[str, object] | None:
    """从 ``__INITIAL_STATE__`` 中取出笔记对象。"""

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
            note = as_object(field(first_obj, "note"))
            return _attach_profile_user(note or first_obj, state_obj)
    return None


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
    """读取作者资料接口中的原始整数统计，避免使用页面上的模糊文案。"""

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
            if re.fullmatch(r"\d[\d,]*", text_value):
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
                {
                    key: value
                    for key, value in source_url.params.multi_items()
                    if key in {"xsec_token", "xsec_source"}
                }
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


async def parse_note(
    client: httpx.AsyncClient,
    input_url: str,
    cookie: str = "",
    *,
    prefer_original_image: bool = True,
    max_video_height: int = 0,
    prefer_hdr_video: bool = True,
    fallback_without_cookie: bool = True,
) -> NoteResult:
    """解析分享短链或笔记链接。"""

    resolved_url = await resolve_short_link(client, input_url, cookie) if is_short_link(input_url) else input_url
    note_id = extract_note_id(resolved_url)
    if not note_id:
        raise NoteParseError("无法提取笔记 ID")
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
    response: httpx.Response | None = None
    if has_token:
        for attempt in range(2 if cookie else 1):
            response = await client.get(base_url, headers=headers)
            document = response.text
            captcha = _is_captcha(document)
            blocked = _is_blocked(response)
            if not captcha and not blocked:
                note = extract_note_from_html(document)
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
            if captcha or blocked or not cookie:
                break
            if attempt == 0:
                await asyncio.sleep(1)
        if (
            fallback_without_cookie
            and response is not None
            and cookie
            and not _is_captcha(response.text)
            and not _is_blocked(response)
        ):
            cookie_expired = True
            headers.pop("Cookie", None)
            response = await client.get(base_url, headers=headers)
            if not _is_captcha(response.text) and not _is_blocked(response):
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

    if not has_token and cookie:
        response = await client.get(base_url, headers=headers)
        if not _is_captcha(response.text) and not _is_blocked(response):
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

    if response is not None and _is_captcha(response.text):
        raise NoteParseError("被小红书风控拦截，请稍后重试或更新 Cookie")
    if response is not None and _is_blocked(response):
        raise NoteParseError("小红书提示「当前笔记暂时无法浏览」(笔记下架或风控)，请稍后重试")
    if not has_token and not cookie:
        raise NoteParseError("直接链接需要配置 Cookie，请使用分享链接或在小红书配置中填写 Cookie")
    raise NoteParseError("无法提取笔记数据，请确认链接有效")


def _is_captcha(document: str) -> bool:
    return "website-login/captcha" in document or "请通过验证" in document


def _is_blocked(response: httpx.Response) -> bool:
    target = str(response.url)
    return bool(re.search(r"/404/sec", target, re.IGNORECASE) or re.search(r"[?&]error_code=\d+", target))

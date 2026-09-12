"""小红书笔记页解析。"""

import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import replace

import httpx

from .urls import explore_url, is_short_link, extract_note_id
from .video import get_best_video_url
from .models import MediaItem, NoteResult
from .streams import (
    text,
    field,
    as_text,
    as_array,
    as_object,
    normalize_url,
    select_live_stream,
)

UA_MOBILE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.6778.200 Safari/537.36 HeyTapBrowser/51.8.8"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_MOBILE = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
_IMAGE_TOKEN_RE = re.compile(r"/\d+/[a-f0-9]+/([^!]+)")
_CHINA_TZ = timezone(timedelta(hours=8))


class NoteParseError(RuntimeError):
    """笔记解析失败。"""


async def resolve_short_link(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, headers={"User-Agent": UA_MOBILE}, follow_redirects=False)
    location = response.headers.get("location", "")
    if not location:
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


def extract_note_from_html(document: str) -> dict[str, object] | None:
    """从 ``__INITIAL_STATE__`` 中取出笔记对象。"""

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
    if state_obj is None:
        return None

    direct = field(field(field(state_obj, "noteData"), "data"), "noteData")
    direct_obj = as_object(direct)
    if direct_obj is not None and text(direct_obj, "noteId"):
        return direct_obj

    note_map = as_object(field(field(state_obj, "note"), "noteDetailMap"))
    if note_map:
        first = next(iter(note_map.values()), None)
        first_obj = as_object(first)
        if first_obj is not None:
            note = as_object(field(first_obj, "note"))
            return note or first_obj
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
        formatted = format_timestamp(note.get(key))
        if formatted:
            return formatted
    return ""


def _image_url(image: dict[str, object]) -> str:
    return normalize_url(field(image, "urlDefault", "url_default", "url", "urlPre", "url_pre"))


def _live_stream(image: dict[str, object]) -> object | None:
    live = as_object(field(image, "livePhoto", "live_photo"))
    if live is not None:
        live_media = as_object(field(live, "media"))
        stream = field(live_media, "stream") if live_media is not None else None
        if stream is not None:
            return stream
    media = as_object(field(image, "media"))
    stream = field(media, "stream") if media is not None else None
    return stream if stream is not None else field(image, "stream")


def collect_media(
    note_id: str,
    note: dict[str, object],
    *,
    prefer_original_image: bool = True,
    max_video_height: int = 0,
    prefer_hdr_video: bool = True,
) -> NoteResult:
    title = as_text(field(note, "displayTitle", "title")) or "未知标题"
    user = as_object(field(note, "user")) or {}
    author = as_text(field(user, "nickname", "nickName")) or "未知作者"
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
            media.append(MediaItem(url=url, is_hdr=bool(re.search(r"uhdr|hdr", url, re.IGNORECASE))))

    has_main_video = False
    video_quality: str | None = None
    if note_type == "video" and len(media) <= 1:
        video = get_best_video_url(
            note,
            max_height=max_video_height,
            prefer_hdr=prefer_hdr_video,
        )
        if video is not None:
            media.append(MediaItem(url=video.url, is_video=True, quality=video.quality, is_hdr=video.is_hdr))
            has_main_video = True
            video_quality = video.quality

    return NoteResult(
        note_id=note_id,
        title=title,
        author=author,
        desc=as_text(field(note, "desc")),
        publish_time=_publish_time(note),
        type="video" if has_main_video else "image",
        video_quality=video_quality,
        media=tuple(media),
        target_video_height=max_video_height,
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
) -> NoteResult:
    """解析分享短链或笔记链接。"""

    resolved_url = await resolve_short_link(client, input_url) if is_short_link(input_url) else input_url
    note_id = extract_note_id(resolved_url)
    if not note_id:
        raise NoteParseError("无法提取笔记 ID")
    has_token = "xsec_token" in resolved_url
    base_url = explore_url(resolved_url, note_id)
    headers = {
        "User-Agent": UA_MOBILE,
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

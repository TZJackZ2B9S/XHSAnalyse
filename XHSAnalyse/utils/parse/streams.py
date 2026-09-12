"""小红书媒体流的候选归一化与画质选择。"""

import re
from dataclasses import dataclass

_NAMED_QUALITY_HEIGHTS = {
    "8k": 7680,
    "4k": 3840,
    "2k": 2560,
}


@dataclass(frozen=True, slots=True)
class StreamChoice:
    """选中的媒体流。"""

    url: str
    width: int
    height: int
    bitrate: int
    size: int
    is_hdr: bool
    codec: str
    quality: str


def as_object(value: object) -> dict[str, object] | None:
    """把 JSON 对象收窄为字符串键字典。"""

    if not isinstance(value, dict):
        return None
    return value


def as_array(value: object) -> list[object] | None:
    return value if isinstance(value, list) else None


def as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def field(value: object, *keys: str) -> object | None:
    """按顺序读取对象中的第一个非空字段。"""

    obj = as_object(value)
    if obj is None:
        return None
    for key in keys:
        item = obj.get(key)
        if item is not None:
            return item
    return None


def number(value: object, *keys: str) -> int:
    raw = field(value, *keys)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(float(raw))
        except ValueError:
            return 0
    return 0


def text(value: object, *keys: str) -> str:
    return as_text(field(value, *keys))


def normalize_url(value: object) -> str:
    """归一化抓包和页面中常见的转义 URL。"""

    raw = as_text(value).strip().replace("\\/", "/").replace("&amp;", "&")
    if raw.startswith("http://"):
        raw = f"https://{raw[7:]}"
    return raw if raw.startswith(("https://", "http://")) else ""


def stream_url(stream: object) -> str:
    direct = normalize_url(field(stream, "master_url", "masterUrl", "url", "play_url", "playUrl"))
    if direct:
        return direct
    backups = field(stream, "backup_urls", "backupUrls", "backup_url", "backupUrl")
    values = as_array(backups) or [backups]
    for value in values:
        url = normalize_url(value)
        if url:
            return url
    return ""


def is_hdr_stream(stream: object) -> bool:
    hdr = field(stream, "hdr_type", "hdrType", "hdr", "is_hdr", "isHdr")
    if isinstance(hdr, bool) and hdr:
        return True
    if isinstance(hdr, (int, float)) and hdr > 0:
        return True
    description = " ".join(
        part
        for part in (
            text(stream, "stream_desc", "streamDesc"),
            text(stream, "quality_type", "qualityType"),
            text(field(stream, "opaque1"), "quality_grade", "qualityGrade"),
            text(field(stream, "opaque1"), "quality_grade_name", "qualityGradeName"),
            text(stream, "video_profile", "videoProfile"),
        )
        if part
    )
    return bool(re.search(r"hdr|dolby|hlg|vivid", description, re.IGNORECASE))


def quality_resolution(stream: object) -> int:
    description = " ".join(
        part
        for part in (
            text(stream, "quality", "resolution", "quality_type", "qualityType"),
            text(stream, "stream_desc", "streamDesc"),
            text(field(stream, "opaque1"), "quality_grade", "qualityGrade"),
            text(field(stream, "opaque1"), "quality_grade_name", "qualityGradeName"),
        )
        if part
    ).lower()
    match = re.search(r"(8k|4320p|4k|2160p|2k|1440p|1080p|720p|576p|540p|480p)", description)
    if match:
        label = match.group(1)
        return _NAMED_QUALITY_HEIGHTS.get(label, int(label.removesuffix("p")))
    for label, resolution in (("uhd", 2160), ("qhd", 1440), ("fhd", 1080), ("hd", 720)):
        if re.search(rf"\b{label}\b", description):
            return resolution
    return 0


def is_direct_media_url(url: str) -> bool:
    return bool(re.search(r"\.(?:mp4|m4v|mov|webm|m3u8)(?:[?#]|$)", url, re.IGNORECASE))


@dataclass(frozen=True, slots=True)
class _Candidate:
    stream: dict[str, object]
    url: str
    group: str


def _flatten(stream: object) -> list[_Candidate]:
    obj = as_object(stream)
    if obj is None:
        return []
    result: list[_Candidate] = []
    for group, value in obj.items():
        values = as_array(value) or [value]
        for item in values:
            item_obj = as_object(item)
            if item_obj is None:
                continue
            url = stream_url(item_obj)
            if url:
                result.append(_Candidate(item_obj, url, group))
    return result


def _score(candidate: _Candidate, live: bool) -> tuple[int, int, int, int, int, int, int]:
    stream = candidate.stream
    width = number(stream, "width", "w")
    height = number(stream, "height", "h")
    quality_height = quality_resolution(stream)
    pixels = width * height or int(quality_height * quality_height * 0.5625) or max(width, height)
    bitrate = number(stream, "video_bitrate", "videoBitrate", "avg_bitrate", "avgBitrate", "bitrate")
    size = number(stream, "size", "file_size", "fileSize")
    hdr = int(is_hdr_stream(stream))
    codec = int(bool(re.search(r"hevc|h265|av1", text(stream, "video_codec", "videoCodec", "codec"), re.IGNORECASE)))
    direct = int(is_direct_media_url(candidate.url))
    return direct, hdr, pixels, max(width, height, quality_height), bitrate, size, codec


def _candidate_height(candidate: _Candidate) -> int:
    stream = candidate.stream
    width = number(stream, "width", "w")
    height = number(stream, "height", "h")
    return max(width, height) or quality_resolution(stream) or number(stream, "quality", "resolution")


def select_stream(
    stream: object,
    *,
    video_only: bool = False,
    max_height: int = 0,
    prefer_hdr: bool = True,
) -> StreamChoice | None:
    """选择画质上限内的最高流；源不足目标时自动取实际最高画质。"""

    candidates = _flatten(stream)
    if video_only:
        candidates = [item for item in candidates if re.search(r"mp4|m4v|mov|webm|m3u8", item.url, re.IGNORECASE)]
        if not candidates:
            return None
    if not candidates:
        return None
    direct = [item for item in candidates if is_direct_media_url(item.url)]
    if video_only and not direct:
        return None
    pool = direct or candidates
    if max_height > 0:
        within_limit = [item for item in pool if 0 < _candidate_height(item) <= max_height]
        unknown_height = [item for item in pool if _candidate_height(item) <= 0]
        if within_limit:
            pool = within_limit
        elif unknown_height:
            pool = unknown_height
    if not prefer_hdr:
        non_hdr = [item for item in pool if not is_hdr_stream(item.stream)]
        if non_hdr:
            pool = non_hdr
    best = max(pool, key=lambda item: _score(item, False))
    selected = best.stream
    width = number(selected, "width", "w")
    height = number(selected, "height", "h")
    resolution = max(width, height) or quality_resolution(selected) or number(selected, "quality", "resolution")
    hdr = is_hdr_stream(selected)
    quality = f"{resolution}p{' HDR' if hdr else ''}" if resolution else text(selected, "quality_type", "qualityType")
    return StreamChoice(
        url=best.url,
        width=width,
        height=height,
        bitrate=number(selected, "video_bitrate", "videoBitrate", "avg_bitrate", "avgBitrate", "bitrate"),
        size=number(selected, "size", "file_size", "fileSize"),
        is_hdr=hdr,
        codec=text(selected, "video_codec", "videoCodec", "codec") or best.group,
        quality=quality,
    )


def select_live_stream(stream: object) -> StreamChoice | None:
    """选择 Live Photo 的视频流，保留 EF 兼容流回退。"""

    selected = select_stream(stream)
    if selected is not None:
        return selected
    obj = as_object(stream)
    if obj is None:
        return None
    ef_items: list[tuple[int, int, object]] = []
    for group, value in obj.items():
        if not re.fullmatch(r"EF\d+", group):
            continue
        values = as_array(value) or [value]
        for item in values:
            item_obj = as_object(item)
            if item_obj is None or not stream_url(item_obj):
                continue
            bitrate = number(item_obj, "video_bitrate", "videoBitrate")
            size = number(item_obj, "size")
            ef_items.append((bitrate, size, item_obj))
    if not ef_items:
        return None
    _, _, best = max(ef_items, key=lambda item: (item[0], item[1]))
    url = stream_url(best)
    return StreamChoice(
        url=url,
        width=number(best, "width", "w"),
        height=number(best, "height", "h"),
        bitrate=number(best, "video_bitrate", "videoBitrate"),
        size=number(best, "size"),
        is_hdr=is_hdr_stream(best),
        codec=text(best, "video_codec", "videoCodec"),
        quality="",
    )

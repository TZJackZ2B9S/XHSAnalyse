"""笔记页中主视频的画质选择。"""

import json

from .streams import (
    StreamChoice,
    text,
    field,
    number,
    as_array,
    as_object,
    stream_url,
    normalize_url,
    quality_resolution,
)


def _is_hdr(stream: object) -> bool:
    hdr = field(stream, "hdrType", "hdr_type", "hdr", "isHdr", "is_hdr")
    if isinstance(hdr, bool):
        return hdr
    if isinstance(hdr, (int, float)):
        return hdr > 0
    description = " ".join(
        part
        for part in (
            text(stream, "streamDesc", "stream_desc"),
            text(stream, "qualityType", "quality_type"),
            text(stream, "videoProfile", "video_profile"),
        )
        if part
    ).lower()
    return any(label in description for label in ("hdr", "dolby", "hlg", "vivid"))


def _resolution(stream: object) -> int:
    width = number(stream, "width", "w")
    height = number(stream, "height", "h")
    if width and height:
        return min(width, height)
    return max(width, height) or quality_resolution(stream)


def _stream_rank(
    stream: object,
    *,
    prefer_hdr: bool,
    ef4: bool = False,
    origin: bool = False,
) -> tuple[int, int, int, int, int, int]:
    """按分辨率优先选择，再在同档位内考虑 HDR、兼容流和编码。"""

    return (
        _resolution(stream),
        int(prefer_hdr and _is_hdr(stream)),
        int(not ef4),
        int(not origin),
        _is_h265(stream),
        number(stream, "videoBitrate", "video_bitrate"),
    )


def _choice(url: str, stream: object, *, hdr: bool, quality: str) -> StreamChoice:
    return StreamChoice(
        url=url,
        width=number(stream, "width", "w"),
        height=number(stream, "height", "h"),
        bitrate=number(stream, "videoBitrate", "video_bitrate"),
        size=0,
        is_hdr=hdr,
        codec=text(stream, "videoCodec", "video_codec"),
        quality=quality,
    )


def _json_object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return as_object(parsed)


def _stream_sources(video: dict[str, object]) -> tuple[dict[str, object], ...]:
    """读取页面流和 mediaV2 原始流；mediaV2 优先。"""

    media = as_object(field(video, "media"))
    media_v2 = _json_object(field(video, "mediaV2", "media_v2"))
    sources: list[dict[str, object]] = []
    for container in (media_v2, media):
        stream = as_object(field(container, "stream"))
        if stream is not None:
            sources.append(stream)
    return tuple(sources)


def _origin_metadata(video: dict[str, object]) -> dict[str, object] | None:
    media = as_object(field(video, "media"))
    media_v2 = _json_object(field(video, "mediaV2", "media_v2"))
    for container in (media_v2, media):
        metadata = as_object(field(container, "video"))
        if metadata is not None and _resolution(metadata) > 0:
            return metadata
    return None


def get_best_video_url(
    note_data: object,
    *,
    max_height: int = 0,
    prefer_hdr: bool = True,
) -> StreamChoice | None:
    """从原始流多档候选中按配置选择，源不足时取实际最高画质。"""

    note = as_object(note_data)
    video = as_object(field(note, "video"))
    if video is None:
        return None
    consumer = as_object(field(video, "consumer"))
    origin_key = text(consumer, "originVideoKey", "origin_video_key")
    origin_url = normalize_url(f"https://sns-video-bd.xhscdn.com/{origin_key}") if origin_key else ""
    origin_metadata = _origin_metadata(video)

    standard: list[tuple[dict[str, object], str, str]] = []
    ef: list[tuple[dict[str, object], str, str]] = []
    seen_urls: set[str] = set()
    for stream in _stream_sources(video):
        for group, value in stream.items():
            entries = as_array(value) or [value]
            for entry in entries:
                item = as_object(entry)
                if item is None:
                    continue
                url = stream_url(item)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                target = ef if group.startswith("EF") and group[2:].isdigit() else standard
                target.append((item, url, group))

    all_streams = [*standard, *ef]
    # ``originVideoKey`` 是源视频的最高画质。当 mediaV2 只暴露 720p 等
    # 低档签名流时，仍要把带尺寸元数据的原始流加入候选，使 4K 档位不会
    # 被错误降级；同分辨率时优先使用签名流，避免不必要的裸地址 403。
    if origin_url and origin_metadata is not None:
        all_streams.append((origin_metadata, origin_url, "ORIGIN"))
    if not all_streams and origin_url:
        if origin_metadata is None:
            return StreamChoice(origin_url, 0, 0, 0, 0, False, "", "origin")
        resolution = _resolution(origin_metadata)
        if resolution:
            hdr = _is_hdr(origin_metadata)
            return _choice(origin_url, origin_metadata, hdr=hdr, quality=f"{resolution}p{' HDR' if hdr else ''}")
        return StreamChoice(origin_url, 0, 0, 0, 0, _is_hdr(origin_metadata), "", "origin")

    if max_height > 0:
        limited = [item for item in all_streams if 0 < _resolution(item[0]) <= max_height]
        unknown = [item for item in all_streams if _resolution(item[0]) <= 0]
        all_streams = limited or unknown or all_streams

    if not all_streams:
        return None
    best = max(
        all_streams,
        key=lambda item: _stream_rank(
            item[0],
            prefer_hdr=prefer_hdr,
            ef4=_is_ef4(item[0], item[2]),
            origin=item[2] == "ORIGIN",
        ),
    )
    resolution = _resolution(best[0]) or number(best[0], "height")
    if resolution:
        hdr = _is_hdr(best[0])
        return _choice(best[1], best[0], hdr=hdr, quality=f"{resolution}p{' HDR' if hdr else ''}")
    return StreamChoice(best[1], 0, 0, 0, 0, False, "", "origin")


def _is_h265(stream: object) -> int:
    codec = text(stream, "videoCodec", "video_codec").lower()
    return int("hevc" in codec or "h265" in codec)


def _is_ef4(stream: object, group: str) -> bool:
    return group == "EF4" or text(stream, "videoCodec", "video_codec") == "EF4"

"""笔记页中主视频的画质选择。"""

from .streams import (
    StreamChoice,
    text,
    field,
    number,
    as_array,
    as_object,
    normalize_url,
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
    width = number(stream, "width")
    height = number(stream, "height")
    return min(width, height) if width and height else max(width, height)


def _stream_rank(stream: object) -> tuple[int, int, int]:
    return (
        _resolution(stream),
        _is_h265(stream),
        number(stream, "videoBitrate", "video_bitrate"),
    )


def _choice(url: str, stream: object, *, hdr: bool, quality: str) -> StreamChoice:
    return StreamChoice(
        url=url,
        width=number(stream, "width"),
        height=number(stream, "height"),
        bitrate=number(stream, "videoBitrate", "video_bitrate"),
        size=0,
        is_hdr=hdr,
        codec=text(stream, "videoCodec", "video_codec"),
        quality=quality,
    )


def get_best_video_url(note_data: object) -> StreamChoice | None:
    """按原插件规则选择网页笔记主视频。"""

    note = as_object(note_data)
    video = as_object(field(note, "video"))
    if video is None:
        return None
    consumer = as_object(field(video, "consumer"))
    origin_key = text(consumer, "originVideoKey")
    origin_url = f"https://sns-video-bd.xhscdn.com/{origin_key}" if origin_key else ""
    media = as_object(field(video, "media"))
    stream = as_object(field(media, "stream"))
    if stream is None:
        return None

    standard: list[tuple[dict[str, object], str, str]] = []
    ef: list[tuple[dict[str, object], str, str]] = []
    for group, value in stream.items():
        entries = as_array(value) or [value]
        for entry in entries:
            item = as_object(entry)
            if item is None:
                continue
            backups = field(item, "backupUrls", "backup_urls")
            backup_list = as_array(backups)
            backup_url = backup_list[0] if backup_list else None
            url = normalize_url(backup_url or field(item, "masterUrl", "master_url"))
            if not url:
                continue
            target = ef if group.startswith("EF") and group[2:].isdigit() else standard
            target.append((item, url, group))

    all_streams = [*standard, *ef]
    hdr_streams = [item for item in all_streams if _is_hdr(item[0])]
    if hdr_streams:
        best = max(hdr_streams, key=lambda item: _stream_rank(item[0]))
        resolution = _resolution(best[0]) or number(best[0], "height")
        return _choice(best[1], best[0], hdr=True, quality=f"{resolution}p HDR")
    if origin_url:
        return StreamChoice(origin_url, 0, 0, 0, 0, False, "", "origin")
    non_ef4 = [item for item in all_streams if not _is_ef4(item[0], item[2])]
    pool = non_ef4 or all_streams
    if not pool:
        return None
    best = max(pool, key=lambda item: _stream_rank(item[0]))
    resolution = _resolution(best[0]) or number(best[0], "height")
    return _choice(best[1], best[0], hdr=False, quality=f"{resolution}p")


def _is_h265(stream: object) -> int:
    codec = text(stream, "videoCodec", "video_codec").lower()
    return int("hevc" in codec or "h265" in codec)


def _is_ef4(stream: object, group: str) -> bool:
    return group == "EF4" or text(stream, "videoCodec", "video_codec") == "EF4"

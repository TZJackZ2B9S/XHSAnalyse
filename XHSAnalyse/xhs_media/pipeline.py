"""兼容入口：媒体处理实现在 ``xhs_core.media.pipeline``。"""

from xhs_core.media.pipeline import (
    PreparedMedia,
    cleanup_media,
    prepare_media,
    local_file_uri,
    build_info_text,
    media_to_message,
    build_forward_message,
)

__all__ = [
    "PreparedMedia",
    "build_forward_message",
    "build_info_text",
    "cleanup_media",
    "local_file_uri",
    "media_to_message",
    "prepare_media",
]

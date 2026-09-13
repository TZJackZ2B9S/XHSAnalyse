"""小红书笔记与媒体解析结果模型。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaItem:
    """一条可下载的小红书媒体。"""

    url: str
    is_video: bool = False
    is_live: bool = False
    live_url: str | None = None
    quality: str | None = None
    is_cover: bool = False
    width: int = 0
    height: int = 0
    is_hdr: bool = False


@dataclass(frozen=True, slots=True)
class NoteResult:
    """从笔记页解析出的规范化结果。"""

    note_id: str
    title: str
    author: str
    desc: str
    publish_time: str
    type: str
    video_quality: str | None
    media: tuple[MediaItem, ...]
    cookie_expired: bool = False
    source: str = "http"
    target_video_height: int = 0
    liked_count: str = ""
    comment_count: str = ""
    collected_count: str = ""
    share_count: str = ""
    author_id: str = ""
    author_avatar: str = ""
    share_url: str = ""
    author_red_id: str = ""
    author_follows: str = ""
    author_fans: str = ""
    author_like_and_collect: str = ""

    @property
    def has_live_photo(self) -> bool:
        return any(item.is_live for item in self.media)

    @property
    def has_hdr_image(self) -> bool:
        return any(item.is_hdr and not item.is_video for item in self.media)

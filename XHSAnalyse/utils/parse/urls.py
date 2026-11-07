"""小红书链接识别与短链处理。"""

import re
import html
from urllib.parse import unquote, urlsplit, urlunsplit

from .streams import normalize_url

_URL_RE = re.compile(
    r"https?://(?:xhslink\.(?:com|cn)|(?:www\.|m\.)?(?:xiaohongshu|rednote)\.com)/[^\s\"'<>\\\]}，。！？、)）]*",
    re.IGNORECASE,
)
# 笔记 ID 只在 path 上匹配：query 里的 redirectPath / note 等参数也会带上
# /explore/<id> 片段，按全串搜索会把登录跳转、主页参数误判成笔记链接。
_NOTE_ID_PATTERNS = (
    re.compile(r"/explore/([a-f0-9]{24})", re.IGNORECASE),
    re.compile(r"/discovery/item/([a-f0-9]{24})", re.IGNORECASE),
    re.compile(r"/note/([a-f0-9]{24})", re.IGNORECASE),
    # 官方第三种分享形态：作者主页前缀 + 作品 ID
    re.compile(r"/user/profile/[a-z0-9]+/([a-f0-9]{24})", re.IGNORECASE),
)
# explore_url 需要在这些标记之前截断 path 才能拼出合法笔记地址。
_NOTE_PATH_MARKER_RE = re.compile(
    r"/(?:explore|note|discovery/item)/|/user/profile/[a-z0-9]+/",
    re.IGNORECASE,
)


def extract_urls(message: str) -> tuple[str, ...]:
    """从消息中提取去重的小红书链接，兼容转义与 URL 编码。"""

    decoded = html.unescape(unquote(message.replace("\\/", "/").replace("\\u0026", "&")))
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(decoded):
        url = match.group(0).rstrip("，。！？、)）]}】")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return tuple(urls)


def extract_note_id(url: str) -> str:
    """从链接的 path 中提取笔记 ID。"""

    path = urlsplit(url).path
    for pattern in _NOTE_ID_PATTERNS:
        match = pattern.search(path)
        if match:
            return match.group(1)
    return ""


def is_short_link(url: str) -> bool:
    return bool(re.match(r"^https?://xhslink\.(?:com|cn)(?:/|$)", url, re.IGNORECASE))


def is_note_link(url: str) -> bool:
    """判断链接是否可能是笔记：含笔记 ID 的直链，或需要展开的分享短链。

    小红书主页、搜索页、活动页等都落在 ``xiaohongshu.com`` 之下，正则一样能
    匹配到，但它们不是笔记，自动解析路径应当静默跳过而不是报错。
    """

    return bool(extract_note_id(url)) or is_short_link(url)


def extract_note_urls(message: str) -> tuple[str, ...]:
    """从消息中提取值得解析的笔记链接，滤掉同域下的非笔记页面。"""

    return tuple(url for url in extract_urls(message) if is_note_link(url))


def explore_url(base_url: str, note_id: str) -> str:
    normalized = normalize_url(base_url)
    if not normalized:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    parsed = urlsplit(normalized)
    marker = _NOTE_PATH_MARKER_RE.search(parsed.path)
    root = parsed.path[: marker.start()] if marker else parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{root}/explore/{note_id}", parsed.query, parsed.fragment))

"""小红书链接识别与短链处理。"""

import re
import html
from urllib.parse import unquote, urlsplit, urlunsplit

from .streams import normalize_url

_URL_RE = re.compile(
    r"https?://(?:xhslink\.(?:com|cn)|(?:www\.)?xiaohongshu\.com)/[^\s\"'<>\\\]}，。！？、)）]*",
    re.IGNORECASE,
)
_NOTE_ID_PATTERNS = (
    re.compile(r"/explore/([a-f0-9]{24})", re.IGNORECASE),
    re.compile(r"/discovery/item/([a-f0-9]{24})", re.IGNORECASE),
    re.compile(r"/note/([a-f0-9]{24})", re.IGNORECASE),
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
    for pattern in _NOTE_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return ""


def is_short_link(url: str) -> bool:
    return bool(re.match(r"^https?://xhslink\.(?:com|cn)(?:/|$)", url, re.IGNORECASE))


def explore_url(base_url: str, note_id: str) -> str:
    normalized = normalize_url(base_url)
    if not normalized:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    parsed = urlsplit(normalized)
    marker = re.search(r"/(?:explore|note|discovery/item)/", parsed.path, re.IGNORECASE)
    root = parsed.path[: marker.start()] if marker else parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{root}/explore/{note_id}", parsed.query, parsed.fragment))

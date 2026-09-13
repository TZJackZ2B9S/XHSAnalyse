"""小红书卡片渲染。

Pillow 负责封面适配和氛围渐隐，pytakumi 只负责文字布局。这样可以
避免 HTML 渲染器对 ``object-fit``、CSS mask 的实现差异导致封面变形。
"""

import re
import html
import math
import base64
import asyncio
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import lru_cache

import httpx
import qrcode
from PIL import Image, ImageDraw, ImageChops, ImageFilter, ImageEnhance
from qrcode.constants import ERROR_CORRECT_M
from qrcode.image.pil import PilImage
from qrcode.exceptions import DataOverflowError

from gsuid_core.logger import logger
from gsuid_core.utils.html_render import render_html_to_bytes

from .parse.models import NoteResult

_WIDTH = 720
_HEIGHT = 1549
_MIN_CARD_HEIGHT = 1080
_COVER_TOP = 260
_FOOTER_SPACE = 430
_TOP_CLEAR_START = 238
_TOP_CLEAR_END = 338
_BOTTOM_CLEAR_START = 1010
_BOTTOM_CLEAR_END = 1248
_CORNER_RADIUS = 24
_CHINA_TZ = timezone(timedelta(hours=8))
_TEMPLATE_PATH = Path(__file__).with_name("card_template.html")
_ICON_PATH = Path(__file__).parents[2] / "ICON.png"


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _crop_to_card(source: Image.Image, width: int, height: int) -> Image.Image:
    target_width, target_height = width, height
    ratio = target_width / target_height
    source_width, source_height = source.size
    if source_width / source_height > ratio:
        crop_width = int(source_height * ratio)
        left = (source_width - crop_width) // 2
        box = (left, 0, left + crop_width, source_height)
    else:
        crop_height = int(source_width / ratio)
        top = max(0, (source_height - crop_height) // 3)
        box = (0, top, source_width, top + crop_height)
    return source.crop(box).resize((target_width, target_height), Image.Resampling.LANCZOS).convert("RGBA")


def _card_geometry(source_size: tuple[int, int]) -> tuple[int, int, int]:
    """返回逻辑像素下的卡片高度、封面高度和封面顶部位置。"""

    source_width, source_height = source_size
    if source_width <= 0 or source_height <= 0:
        raise ValueError("封面尺寸无效")
    cover_height = max(1, round(_WIDTH * source_height / source_width))
    card_height = max(_MIN_CARD_HEIGHT, _COVER_TOP + cover_height + _FOOTER_SPACE)
    return card_height, cover_height, _COVER_TOP


def _cover_mask(width: int, height: int) -> Image.Image:
    """封面上下渐隐，保持主体完整并自然融入氛围背景。"""

    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(height):
        position = y / max(1, height - 1)
        if position < 0.24:
            alpha = _smoothstep(position / 0.24)
        elif position <= 0.72:
            alpha = 1.0
        else:
            alpha = 1.0 - _smoothstep((position - 0.72) / 0.28)
        draw.line((0, y, width, y), fill=round(alpha * 255))
    return mask


def _foreground_mask(width: int, height: int) -> Image.Image:
    scale_y = height / _HEIGHT
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(height):
        logical_y = y / scale_y
        if logical_y < _TOP_CLEAR_START:
            alpha = 0.0
        elif logical_y < _TOP_CLEAR_END:
            alpha = _smoothstep((logical_y - _TOP_CLEAR_START) / (_TOP_CLEAR_END - _TOP_CLEAR_START))
        elif logical_y <= _BOTTOM_CLEAR_START:
            alpha = 1.0
        elif logical_y < _BOTTOM_CLEAR_END:
            alpha = 1.0 - _smoothstep((logical_y - _BOTTOM_CLEAR_START) / (_BOTTOM_CLEAR_END - _BOTTOM_CLEAR_START))
        else:
            alpha = 0.0
        draw.line((0, y, width, y), fill=round(alpha * 255))
    return mask


def _color_veil(width: int, height: int) -> Image.Image:
    scale_y = height / _HEIGHT
    veil = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(veil)
    for y in range(height):
        logical_y = y / scale_y
        if logical_y < _TOP_CLEAR_START:
            alpha = 170 - 38 * _smoothstep(logical_y / _TOP_CLEAR_START)
        elif logical_y < _TOP_CLEAR_END:
            alpha = 132 * (1.0 - _smoothstep((logical_y - _TOP_CLEAR_START) / (_TOP_CLEAR_END - _TOP_CLEAR_START)))
        elif logical_y > _BOTTOM_CLEAR_END:
            alpha = 110 + (245 - 110) * _smoothstep((logical_y - _BOTTOM_CLEAR_END) / (_HEIGHT - _BOTTOM_CLEAR_END))
        elif logical_y > _BOTTOM_CLEAR_START:
            alpha = 110 * _smoothstep((logical_y - _BOTTOM_CLEAR_START) / (_BOTTOM_CLEAR_END - _BOTTOM_CLEAR_START))
        else:
            alpha = 0
        draw.line((0, y, width, y), fill=(255, 255, 255, round(alpha)))
    return veil


def _build_background(
    path: Path,
    render_scale: float,
    card_height: int | None = None,
) -> bytes:
    width = max(1, round(_WIDTH * render_scale))
    source = Image.open(path).convert("RGB")
    resolved_height, cover_height, cover_top = _card_geometry(source.size)
    logical_height = card_height or resolved_height
    height = max(1, round(logical_height * render_scale))
    cover_height = max(1, round(cover_height * render_scale))
    cover_top = round(cover_top * render_scale)
    ambient = _crop_to_card(source, width, height).filter(ImageFilter.GaussianBlur(76 * render_scale))
    ambient = ImageEnhance.Color(ambient).enhance(1.10)
    ambient = ImageEnhance.Contrast(ambient).enhance(0.96)
    ambient = ambient.convert("RGBA")
    cover = source.resize((width, cover_height), Image.Resampling.LANCZOS).convert("RGBA")
    cover_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    cover.putalpha(_cover_mask(width, cover_height))
    cover_layer.alpha_composite(cover, (0, cover_top))
    cover_layer.putalpha(ImageChops.multiply(cover_layer.getchannel("A"), _foreground_mask(width, height)))
    result = Image.alpha_composite(ambient, cover_layer)
    result = Image.alpha_composite(result, _color_veil(width, height)).convert("RGB")
    output = BytesIO()
    # 保留完整像素数据；卡片尺寸本身受控，不额外做 PNG 压缩。
    result.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _crop_rendered_card(content: bytes, width: int, height: int) -> bytes:
    """将按 100% 精度渲染的卡片缩放到低精度目标尺寸。

    pytakumi 在 ``device_pixel_ratio < 1`` 时会把 CSS 内容绘制到视口左侧，
    右侧留下未绘制的黑/透明区域。低精度模式统一先按 100% 完整渲染，
    再缩放整张卡片，不能只裁左侧，否则标题和右侧元素会被截断。
    """

    image = Image.open(BytesIO(content)).convert("RGBA")
    target_width = max(1, width)
    target_height = max(1, height)
    resized = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
    output = BytesIO()
    resized.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _data_uri(content: bytes, mime: str = "image/png") -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _apply_card_corners(content: bytes, render_scale: float) -> bytes:
    """将卡片外的四个角清成透明，避免渲染器页面底色露出来。"""

    image = Image.open(BytesIO(content)).convert("RGBA")
    width, height = image.size
    radius = min(round(_CORNER_RADIUS * render_scale), width // 2, height // 2)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        fill=255,
    )
    image.putalpha(mask)
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


@lru_cache(maxsize=128)
def _qr_html(value: str, render_scale: float) -> str:
    if not value:
        return '<div class="qr-placeholder"></div>'
    box_size = max(1, round(8 * render_scale))
    border = max(1, round(2 * render_scale))
    code = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    code.add_data(value)
    code.make(fit=True)
    image = code.make_image(image_factory=PilImage, fill_color="#222222", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return f'<img class="qr" src="{_data_uri(output.getvalue())}">'


def _tags(desc: str) -> str:
    values = re.findall(r"#[^#\s\[\]]+", desc)
    if not values:
        return "小红书笔记"
    return " ".join(dict.fromkeys(values[:5]))


def _description_preview(desc: str, limit: int = 32) -> str:
    """清理正文中的话题并限制底部单行摘要长度。"""

    text = re.sub(r"#[^#\s\[\]]+", "", desc)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "暂无正文"
    if len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _value(value: str) -> str:
    return html.escape(value or "0")


def _compact_count(number: int) -> str:
    """把精确整数压缩成卡片可读的中文单位。"""

    if number >= 100_000_000:
        value = number / 100_000_000
        return f"{value:.1f}".rstrip("0").rstrip(".") + "亿"
    if number >= 10_000:
        value = number / 10_000
        return f"{value:.1f}".rstrip("0").rstrip(".") + "万"
    return str(number)


def _author_count(value: str) -> str:
    """只显示精确统计；页面返回 ``10+`` 等区间文案时不伪造数值。"""

    raw = (value or "").strip().replace(",", "")
    if not raw or "+" in raw:
        return ""
    if re.fullmatch(r"\d+", raw):
        return _compact_count(int(raw))
    if re.fullmatch(r"\d+(?:\.\d+)?(?:万|亿)", raw):
        return raw
    return ""


def _video_meta(result: NoteResult) -> str:
    """将视频画质整理为卡片右下角的简短标识。"""

    quality = (result.video_quality or "").strip()
    resolution = quality.split(maxsplit=1)[0] if quality else ""
    if resolution.lower() == "origin":
        resolution = "原画"
    named_resolution = resolution.lower()
    if named_resolution in {"8k", "4k", "2k"}:
        resolution = named_resolution.upper()
    else:
        match = re.fullmatch(r"(\d{3,4})p", named_resolution)
        if match:
            resolution = {
                "4320": "8K",
                "2160": "4K",
                "1440": "2K",
            }.get(match.group(1), f"{match.group(1)}P")
        else:
            video = next((item for item in result.media if item.is_video), None)
            if video is not None:
                pixels = min(video.width, video.height) if video.width and video.height else 0
                resolution = f"{pixels}P" if pixels else "视频"
    hdr = "HDR" if "hdr" in quality.lower() or any(item.is_video and item.is_hdr for item in result.media) else ""
    return " ".join(part for part in (resolution or "视频", hdr) if part)


_AUTHOR_STAT_ICONS = {
    "关注": (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="#666" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="8" cy="7" r="4"/><path d="M1.5 21v-2a4 4 0 0 1 4-4h5a4 4 0 0 1 4 4v2"/>'
        '<path d="M19 8v6M22 11h-6"/></svg>'
    ),
    "粉丝": (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="#666" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="9" cy="7" r="4"/><path d="M2 21v-2a4 4 0 0 1 4-4h6a4 4 0 0 1 4 4v2"/>'
        '<path d="M16 3.2a4 4 0 0 1 0 7.6M18 15h1a4 4 0 0 1 4 4v2"/></svg>'
    ),
    "获赞与收藏": (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="#666" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M20.8 8.7c0 5.4-8.8 10.3-8.8 10.3S3.2 14.1 3.2 8.7A4.7 4.7 0 0 1 12 6.1a4.7 4.7 0 0 1 8.8 2.6Z"/>'
        "</svg>"
    ),
}


def _author_stats_html(result: NoteResult) -> str:
    values = (
        ("关注", result.author_follows),
        ("粉丝", result.author_fans),
        ("获赞与收藏", result.author_like_and_collect),
    )
    items = [(label, _author_count(value) or "—") for label, value in values if value]
    rendered = [
        f'<span class="author-stat"><span class="author-stat-head">'
        f'<span class="author-stat-icon">{_AUTHOR_STAT_ICONS[label]}</span>'
        f'<span class="author-stat-label">{label}</span></span>'
        f"<strong>{html.escape(value)}</strong></span>"
        for label, value in items
    ]
    return f'<div class="author-stats">{"".join(rendered)}</div>' if rendered else ""


@lru_cache(maxsize=8)
def _live_icon_uri(render_scale: float) -> str:
    """生成实况图标：点阵外圈、粗线中圈和实心中心。"""

    target_size = max(1, round(23 * render_scale))
    source_size = max(100, target_size * 4)
    geometry_scale = source_size / 100
    image = Image.new("RGBA", (source_size, source_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = source_size / 2
    outer_radius = 37 * geometry_scale
    dot_radius = 3 * geometry_scale
    for index in range(21):
        angle = index * 2 * math.pi / 21
        x = center + outer_radius * math.cos(angle)
        y = center + outer_radius * math.sin(angle)
        draw.ellipse(
            (x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius),
            fill="#4a4a4a",
        )
    draw.ellipse(
        (25 * geometry_scale, 25 * geometry_scale, 75 * geometry_scale, 75 * geometry_scale),
        outline="#4a4a4a",
        width=max(1, round(6 * geometry_scale)),
    )
    draw.ellipse(
        (40 * geometry_scale, 40 * geometry_scale, 60 * geometry_scale, 60 * geometry_scale),
        fill="#4a4a4a",
    )
    image = image.resize((target_size, target_size), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return _data_uri(output.getvalue())


@lru_cache(maxsize=8)
def _location_icon_uri(render_scale: float) -> str:
    """生成小尺寸位置图标，避免 HTML 渲染器放大内联 SVG。"""

    target_size = max(1, round(18 * render_scale))
    source_size = max(96, target_size * 4)
    scale = source_size / 96
    image = Image.new("RGBA", (source_size, source_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    points: list[tuple[float, float]] = []

    def cubic(
        start: tuple[float, float],
        control_one: tuple[float, float],
        control_two: tuple[float, float],
        end: tuple[float, float],
        steps: int = 20,
    ) -> None:
        for index in range(steps + 1):
            t = index / steps
            inverse = 1 - t
            points.append(
                (
                    (
                        inverse**3 * start[0]
                        + 3 * inverse**2 * t * control_one[0]
                        + 3 * inverse * t**2 * control_two[0]
                        + t**3 * end[0]
                    )
                    * scale,
                    (
                        inverse**3 * start[1]
                        + 3 * inverse**2 * t * control_one[1]
                        + 3 * inverse * t**2 * control_two[1]
                        + t**3 * end[1]
                    )
                    * scale,
                )
            )

    # 两段贝塞尔曲线加顶部圆弧，勾勒出标准地图针脚轮廓。
    cubic((48, 88), (20, 64), (18, 55), (20, 35))
    for index in range(1, 21):
        angle = math.pi - math.pi * index / 20
        points.append(((48 + 28 * math.cos(angle)) * scale, (35 - 28 * math.sin(angle)) * scale))
    cubic((76, 35), (78, 55), (76, 64), (48, 88))
    draw.line(points, fill="#666666", width=max(1, round(7 * scale)), joint="curve")
    inner = (36 * scale, 25 * scale, 60 * scale, 49 * scale)
    draw.ellipse(inner, outline="#666666", width=max(1, round(6 * scale)))
    image = image.resize((target_size, target_size), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return _data_uri(output.getvalue())


def _avatar_html(uri: str, *, mini: bool = False) -> str:
    class_name = "avatar mini-avatar" if mini else "avatar"
    if uri:
        return f'<img class="{class_name}" src="{uri}">'
    return f'<div class="{class_name}"></div>'


@lru_cache(maxsize=8)
def _brand_icon_uri(render_scale: float) -> str:
    """读取插件图标并按卡片精度缩放，供右上角品牌标识使用。"""

    target_size = max(1, round(78 * render_scale))
    with Image.open(_ICON_PATH) as source:
        icon = source.convert("RGBA").resize((target_size, target_size), Image.Resampling.LANCZOS)
    output = BytesIO()
    icon.save(output, format="PNG", optimize=True)
    return _data_uri(output.getvalue())


async def _download_avatar(client: httpx.AsyncClient | None, url: str) -> str:
    if client is None or not url:
        return ""
    try:
        response = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=httpx.Timeout(10.0, connect=3.0),
        )
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("头像超过 2 MB")
        mime = response.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
        if not mime.startswith("image/"):
            return ""
        return _data_uri(response.content, mime)
    except (httpx.HTTPError, OSError, ValueError) as error:
        logger.debug(f"[XHSAnalyse] 作者头像下载失败：{error}")
        return ""


def _template(
    result: NoteResult,
    media_count: int,
    avatar_uri: str,
    render_scale: float,
    card_height: int = _HEIGHT,
) -> str:
    title = html.escape(result.title or "未知标题")
    author = html.escape(result.author or "未知作者")
    publish_time = html.escape(result.publish_time or "小红书笔记")
    location = html.escape(result.ip_location.strip())
    location_html = (
        f'<span class="location"><img class="location-icon" width="18" height="18" '
        f'src="{_location_icon_uri(render_scale)}" alt="">'
        f'<span class="location-name">{location}</span></span>'
        if location
        else ""
    )
    media_label = "图文 · 含实况" if result.has_live_photo else ("视频笔记" if result.type == "video" else "图文笔记")
    author_id = html.escape(result.author_red_id or result.author_id or result.note_id)
    generated_at = datetime.now(_CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    values = {
        "AUTHOR": author,
        "BRAND_ICON": _brand_icon_uri(render_scale),
        "AVATAR": _avatar_html(avatar_uri),
        "MINI_AVATAR": _avatar_html(avatar_uri, mini=True),
        "PUBLISH_TIME": publish_time,
        "LOCATION": location_html,
        "TITLE": title,
        "TAGS": html.escape(_tags(result.desc)),
        "MEDIA_LABEL": media_label,
        "DESC_PREVIEW": html.escape(_description_preview(result.desc)),
        "MEDIA_META": _video_meta(result) if result.type == "video" else f"共 {media_count} 张",
        "LIKED_COUNT": _value(result.liked_count),
        "COMMENT_COUNT": _value(result.comment_count),
        "COLLECTED_COUNT": _value(result.collected_count),
        "SHARE_COUNT": _value(result.share_count),
        "AUTHOR_ID": author_id,
        "AUTHOR_STATS": _author_stats_html(result),
        "GENERATED_AT": generated_at,
        "QR": _qr_html(result.share_url, render_scale),
        "CARD_HEIGHT": str(card_height),
    }
    template = _template_source()
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


@lru_cache(maxsize=1)
def _template_source() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


async def render_note_card(
    result: NoteResult,
    cover_path: Path,
    client: httpx.AsyncClient | None = None,
    *,
    render_scale: float = 1.0,
) -> bytes | None:
    """渲染卡片；输入图片损坏或渲染器不可用时返回 ``None``。"""

    if not cover_path.is_file():
        return None
    try:
        scale = max(1.0, min(5.0, float(render_scale)))
        avatar_task = _download_avatar(client, result.author_avatar)
        size_task = asyncio.to_thread(_image_size, cover_path)
        avatar_uri, source_size = await asyncio.gather(avatar_task, size_task)
        card_height, _, _ = _card_geometry(source_size)
        render_scale = scale
        background_task = asyncio.to_thread(
            _build_background,
            cover_path,
            render_scale,
            card_height,
        )
        template_task = asyncio.to_thread(
            _template,
            result,
            sum(1 for item in result.media if not item.is_video),
            avatar_uri,
            render_scale,
            card_height,
        )
        background, template = await asyncio.gather(background_task, template_task)
        template = template.replace("{{BACKGROUND}}", _data_uri(background))
        render_width = round(_WIDTH * render_scale)
        render_height = round(card_height * render_scale)
        rendered = await render_html_to_bytes(
            template,
            max_width=render_width,
            dpi=96 * render_scale,
            device_height=render_height,
            allow_refit=True,
            default_font_size=25,
            font_name="MiSans",
            root_max_width=_WIDTH,
        )
        return await asyncio.to_thread(_apply_card_corners, rendered, scale)
    except (OSError, ValueError, RuntimeError, DataOverflowError) as error:
        logger.warning(f"[XHSAnalyse] 卡片渲染失败，回退文案消息：{error}")
        return None


__all__ = ["render_note_card"]

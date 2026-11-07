"""小红书卡片渲染。

Pillow 负责封面适配和氛围渐隐，pytakumi 只负责文字布局。这样可以
避免 HTML 渲染器对 ``object-fit``、CSS mask 的实现差异导致封面变形。
"""

import re
import html
import math
import base64
import asyncio
import colorsys
from io import BytesIO
from typing import NamedTuple
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import lru_cache

import httpx
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageStat, ImageFilter, ImageEnhance
from qrcode.constants import ERROR_CORRECT_Q
from qrcode.exceptions import DataOverflowError

from gsuid_core.logger import logger
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.html_render import render_html_to_bytes

from .media.image import is_jpeg, ensure_jpeg
from .parse.models import NoteResult

_WIDTH = 720
_HEIGHT = 1549
_MIN_CARD_HEIGHT = 1080
_COVER_TOP = 260
_FOOTER_SPACE = 430
_BODY_TOP = 120
_TITLE_MAX_WIDTH = 620
_TITLE_FONT_SIZE = 36
_TITLE_LINE_HEIGHT = 1.22
_TAGS_GAP = 17
_TAG_LINE_HEIGHT = 32
_COVER_OVERLAY = 12
# tag 下沿与封面渐隐起点之间的间距，与底部简介行到白纱起点的间距对齐。
_TAG_SAFE_GAP = 20
# 类型药丸压在封面实图上，必须落在封面顶部渐隐结束之后，否则会被半透明
# 渐变切成两截。这里在渐隐终点下方再留一点余量。
_PILL_GAP = 14
_MIN_COVER_TOP = 180
# 页脚是 ``bottom: 45px`` 的底部锚定块，它到封面渐隐中点的距离由这两个值决定。
# 取 377/323 时上下两侧字形到渐隐中点的距离都是 78.7px（实测 1.5 倍渲染），
# 再小就会让页脚首行贴到封面渐隐尾迹上，视觉上明显上紧下松。
_FOOTER_SPACE_WITH_STATS = 377
_FOOTER_SPACE_PLAIN = 323
# 封面上下渐隐的长度按封面高度自适应，只占封面很小一段：固定比例在
# 高封面上会吃掉过多画面，在矮封面上又不足以隐藏硬边。上下限保证
# 极端尺寸下仍然既有过渡、又不浪费空间。
_COVER_FADE_RATIO = 0.075
_COVER_FADE_MIN = 40
_COVER_FADE_MAX = 90
# 边缘模糊带：渐隐区内把封面逐步换成自身模糊版本，让短渐变也能自然
# 融进氛围层，不依赖“拉长渐变”来掩盖高频硬边。
# 白纱：文字区保持高不透明度，离开文字区后先用短渐变快速降到低强度
# 尾迹，再用长尾把过渡摊平。这样既保证对比度，又不会用一大段高白把
# 封面洗掉——视觉上“占用”的纵向空间远小于同等峰值的长渐变。
_VEIL_TOP_PEAK_MIN = 90.0
_VEIL_TOP_PEAK_MAX = 150.0
_VEIL_TOP_LUMA_WEIGHT = 0.40
_VEIL_TOP_TAIL = 24.0
_VEIL_TOP_FADE = 110
_VEIL_TOP_TAIL_FADE = 260
_VEIL_BOTTOM_PEAK_MIN = 130.0
_VEIL_BOTTOM_PEAK_MAX = 180.0
_VEIL_BOTTOM_LUMA_WEIGHT = 0.33
_VEIL_BOTTOM_TAIL = 30.0
_VEIL_BOTTOM_RAMP = 110
_VEIL_BOTTOM_TAIL_FADE = 240
# 延伸色：把封面上下两端的主色调去饱和提亮后作为白纱底色，
# 渐变末端不再是中性白，而是带着封面色相的雾，接缝感更弱。
_VEIL_TINT_SATURATION = 0.32
_VEIL_TINT_MIX = 0.30
# 混色上限受亮度地板约束：色相再深，白纱亮度也不会低于这个值，
# 保证底栏灰字（#6f6f6f）的对比度不被延伸色吃掉。
_VEIL_TINT_LUMA_FLOOR = 240.0
_AMBIENT_BLUR = 76
_CORNER_RADIUS = 24
# 二维码：透明底、圆点变形、中心头像；尺寸与右下角偏移由模板同步。
_QR_SIZE = 160
_QR_QUIET_ZONE = 4
# 中心头像会挖掉约 6% 面积，M 级（≈15% 纠错）足够；
# 用 Q 级会把模块数抬到 57+，140px 下每模块只剩 2.1px，反而更难扫。
_QR_ERROR_CORRECTION = ERROR_CORRECT_Q
_QR_SUPERSAMPLE = 4
_QR_INK = (17, 17, 17, 255)
_QR_DOT_RADIUS = 0.34
_QR_AVATAR_RATIO = 0.20
_QR_AVATAR_MARGIN_RATIO = 0.024
_QR_AVATAR_CACHE_SIZE = 32
_AVATAR_SIZE = 256
_CHINA_TZ = timezone(timedelta(hours=8))
_TEMPLATE_PATH = Path(__file__).with_name("card_template.html")
_ICON_PATH = Path(__file__).parents[2] / "ICON.png"


def _smootherstep(value: float) -> float:
    """五次平滑曲线：两端一阶、二阶导数都为 0，短渐变也不容易出现折角。"""

    value = max(0.0, min(1.0, value))
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


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


class _CardLayout(NamedTuple):
    """一张卡片的逻辑像素几何，随标题行数和底部信息块变化。"""

    card_height: int
    cover_height: int
    cover_top: int
    top_hold: int
    footer_space: int


@lru_cache(maxsize=8)
def _title_font() -> ImageFont.FreeTypeFont:
    return core_font(_TITLE_FONT_SIZE, 700.0)


def _wrap_text(text: str, max_width: int) -> list[str]:
    """按卡片标题宽度折行，中文按字、西文尽量在空格处断开。"""

    font = _title_font()
    lines: list[str] = []
    current = ""
    for char in text:
        trial = current + char
        if font.getlength(trial) <= max_width:
            current = trial
            continue
        if current:
            break_at = current.rfind(" ")
            if break_at > 0:
                lines.append(current[:break_at])
                current = current[break_at + 1 :] + char
            else:
                lines.append(current)
                current = char
            continue
        lines.append(char)
        current = ""
    if current:
        lines.append(current)
    return lines or [""]


def _title_line_count(title: str) -> int:
    return max(1, len(_wrap_text(title or "未知标题", _TITLE_MAX_WIDTH)))


def _pill_top(layout: "_CardLayout") -> int:
    """类型药丸的纵向位置：封面顶部渐隐结束之后再往下留 ``_PILL_GAP``。"""

    return layout.cover_top + _cover_fade_length(layout.cover_height) + _PILL_GAP


def _top_text_end(title: str) -> int:
    """标题和 tag 的下沿；类型 pill 不参与封面顶部安全区计算。"""

    title_height = _title_line_count(title) * round(_TITLE_FONT_SIZE * _TITLE_LINE_HEIGHT)
    return _BODY_TOP + title_height + _TAGS_GAP + _TAG_LINE_HEIGHT


def _footer_space_for(result: NoteResult) -> int:
    if result.author_follows or result.author_fans or result.author_like_and_collect:
        return _FOOTER_SPACE_WITH_STATS
    return _FOOTER_SPACE_PLAIN


def _cover_height_for(source_size: tuple[int, int]) -> int:
    source_width, source_height = source_size
    if source_width <= 0 or source_height <= 0:
        raise ValueError("封面尺寸无效")
    return max(1, round(_WIDTH * source_height / source_width))


def _card_layout(source_size: tuple[int, int], result: NoteResult | None = None) -> _CardLayout:
    cover_height = _cover_height_for(source_size)
    if result is None:
        cover_top = _COVER_TOP
        footer_space = _FOOTER_SPACE
        top_hold = _COVER_TOP + 48
        card_height = max(_MIN_CARD_HEIGHT, cover_top + cover_height + footer_space)
        return _CardLayout(card_height, cover_height, cover_top, top_hold, footer_space)
    top_hold = _top_text_end(result.title)
    # tag 下沿到封面渐隐起点留出正间距：底部简介行到白纱起点也是正间距，
    # 两侧对称后 tag 不会压在封面刚开始显影的位置上。
    cover_top = max(_MIN_COVER_TOP, top_hold + _TAG_SAFE_GAP)
    footer_space = _footer_space_for(result)
    card_height = cover_top + cover_height + footer_space
    if card_height < _MIN_CARD_HEIGHT:
        # 高度被下限托起时把多出来的空白对半分给封面上下两侧；只补在页脚上方
        # 会让下间距凭空变大，宽封面（16:9 之类）立刻又变成上紧下松。
        extra = _MIN_CARD_HEIGHT - card_height
        cover_top += extra // 2
        footer_space += extra - extra // 2
        card_height = _MIN_CARD_HEIGHT
    return _CardLayout(card_height, cover_height, cover_top, top_hold, footer_space)


def _card_geometry(source_size: tuple[int, int]) -> tuple[int, int, int]:
    """返回逻辑像素下的卡片高度、封面高度和封面顶部位置。"""

    layout = _card_layout(source_size)
    return layout.card_height, layout.cover_height, layout.cover_top


def _cover_fade_length(cover_height: int) -> int:
    """按封面高度取渐隐长度，并夹在上下限之间。"""

    return max(
        _COVER_FADE_MIN,
        min(_COVER_FADE_MAX, round(cover_height * _COVER_FADE_RATIO)),
    )


def _cover_mask(
    width: int,
    height: int,
    top_fade: int | None = None,
    bottom_fade: int | None = None,
) -> Image.Image:
    """封面上下渐隐：顶部保持紧凑，底部可延长到页脚，避免短封面留下硬边。

    渐隐长度按设备像素传入（调用方已经乘过 ``render_scale``），
    这样同一张封面在不同分辨率下的渐变比例保持一致。
    """

    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    fallback = _cover_fade_length(height)
    top = max(1, min(top_fade if top_fade is not None else fallback, max(1, height // 2)))
    bottom = max(1, bottom_fade if bottom_fade is not None else fallback)
    bottom = min(bottom, max(1, height - top))
    for y in range(height):
        if y < top:
            alpha = _smootherstep((y + 0.5) / top)
        elif y >= height - bottom:
            alpha = _smootherstep((height - 1 - y + 0.5) / bottom)
        else:
            alpha = 1.0
        draw.line((0, y, width, y), fill=round(alpha * 255))
    return mask


def _veil_strength(distance: float, peak: float, fade: int, tail: float, tail_fade: int) -> float:
    """文字区外的高强度渐变 + 低强度长尾。"""

    if distance <= 0:
        return peak + tail
    strong = peak * (1.0 - _smootherstep(distance / max(1, fade))) if distance < fade else 0.0
    soft = tail * (1.0 - _smootherstep(distance / max(1, tail_fade))) if distance < tail_fade else 0.0
    return strong + soft


def _adaptive_veil_peak(luma: float, maximum: float, minimum: float, weight: float) -> float:
    """按底图亮度调整白纱强度：亮图少洗一点，暗图多留一点对比度。"""

    return max(minimum, min(maximum, maximum - luma * weight))


def _rgb_luma(red: float, green: float, blue: float) -> float:
    return 0.299 * red + 0.587 * green + 0.114 * blue


def _edge_tint(image: Image.Image, top: int, bottom: int) -> tuple[int, int, int]:
    """取区域平均色相，按固定比例混进白色，作为该侧渐变的延伸色。

    混色比例受亮度地板约束：深色封面只能混进较少比例，
    所以白纱始终够亮，灰字对比度不被色相吃掉；
    纯灰封面的饱和度为 0，算出来仍是纯白。
    """

    height = image.height
    top = max(0, min(height - 1, top))
    bottom = max(top + 1, min(height, bottom))
    mean = ImageStat.Stat(image.crop((0, top, image.width, bottom)).convert("RGB")).mean
    hue, saturation, _ = colorsys.rgb_to_hsv(mean[0] / 255.0, mean[1] / 255.0, mean[2] / 255.0)
    pure = [channel * 255.0 for channel in colorsys.hsv_to_rgb(hue, min(saturation, _VEIL_TINT_SATURATION), 1.0)]
    pure_luma = _rgb_luma(pure[0], pure[1], pure[2])
    headroom = (255.0 - _VEIL_TINT_LUMA_FLOOR) / max(1.0, 255.0 - pure_luma)
    mix = min(_VEIL_TINT_MIX, headroom)
    mixed = [round(255.0 * (1.0 - mix) + channel * mix) for channel in pure]
    return mixed[0], mixed[1], mixed[2]


def _color_veil(
    width: int,
    height: int,
    top_hold: int,
    footer_top: int,
    top_peak: float,
    bottom_peak: float,
    top_tint: tuple[int, int, int],
    bottom_tint: tuple[int, int, int],
) -> Image.Image:
    """灰字所在区域保持近白，中间把封面颜色露出来。"""

    veil = Image.new("RGBA", (width, height), (*top_tint, 0))
    draw = ImageDraw.Draw(veil)
    for y in range(height):
        top_alpha = _veil_strength(
            y - top_hold,
            top_peak,
            _VEIL_TOP_FADE,
            _VEIL_TOP_TAIL,
            _VEIL_TOP_TAIL_FADE,
        )
        bottom_alpha = _veil_strength(
            footer_top - y,
            bottom_peak,
            _VEIL_BOTTOM_RAMP,
            _VEIL_BOTTOM_TAIL,
            _VEIL_BOTTOM_TAIL_FADE,
        )
        alpha = min(255.0, max(top_alpha, bottom_alpha))
        color = top_tint if top_alpha >= bottom_alpha else bottom_tint
        draw.line((0, y, width, y), fill=(*color, round(alpha)))
    return veil


def _region_luma(image: Image.Image, top: int, bottom: int) -> float:
    """取一条横带亮度的平均值，用来决定白纱强度。"""

    height = image.height
    top = max(0, min(height - 1, top))
    bottom = max(top + 1, min(height, bottom))
    return ImageStat.Stat(image.crop((0, top, image.width, bottom)).convert("L")).mean[0]


def _build_background(
    path: Path,
    render_scale: float,
    card_height: int | None = None,
    layout: _CardLayout | None = None,
) -> bytes:
    width = max(1, round(_WIDTH * render_scale))
    source = Image.open(path).convert("RGB")
    resolved = layout or _card_layout(source.size)
    logical_height = card_height or resolved.card_height
    height = max(1, round(logical_height * render_scale))
    cover_height = max(1, round(resolved.cover_height * render_scale))
    cover_top = round(resolved.cover_top * render_scale)
    top_hold = round(resolved.top_hold * render_scale)
    footer_top = height - round(resolved.footer_space * render_scale)
    ambient = _crop_to_card(source, width, height).filter(ImageFilter.GaussianBlur(_AMBIENT_BLUR * render_scale))
    ambient = ImageEnhance.Color(ambient).enhance(1.10)
    ambient = ImageEnhance.Contrast(ambient).enhance(0.96)
    ambient = ambient.convert("RGBA")
    cover = source.resize((width, cover_height), Image.Resampling.LANCZOS).convert("RGBA")
    # 渐隐长度按逻辑像素算好再乘分辨率：同一张封面在 1.0/1.5/2.0 下
    # 的渐变比例必须一致，否则高分辨率反而显得更硬。
    top_fade = _cover_fade_length(resolved.cover_height)
    footer_top_logical = logical_height - resolved.footer_space
    cover_bottom = resolved.cover_top + resolved.cover_height
    bottom_fade = max(top_fade, footer_top_logical - cover_bottom + 24)
    bottom_fade = min(bottom_fade, max(top_fade, resolved.cover_height - top_fade))
    cover_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    cover.putalpha(
        _cover_mask(
            width,
            cover_height,
            round(top_fade * render_scale),
            round(bottom_fade * render_scale),
        )
    )
    cover_layer.alpha_composite(cover, (0, cover_top))
    composed = Image.alpha_composite(ambient, cover_layer)
    top_peak = _adaptive_veil_peak(
        _region_luma(composed, 0, top_hold),
        _VEIL_TOP_PEAK_MAX,
        _VEIL_TOP_PEAK_MIN,
        _VEIL_TOP_LUMA_WEIGHT,
    )
    bottom_peak = _adaptive_veil_peak(
        _region_luma(composed, footer_top, height),
        _VEIL_BOTTOM_PEAK_MAX,
        _VEIL_BOTTOM_PEAK_MIN,
        _VEIL_BOTTOM_LUMA_WEIGHT,
    )
    result = Image.alpha_composite(
        composed,
        _color_veil(
            width,
            height,
            top_hold,
            footer_top,
            top_peak,
            bottom_peak,
            _edge_tint(composed, 0, top_hold),
            _edge_tint(composed, footer_top, height),
        ),
    ).convert("RGB")
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


class _Avatar(NamedTuple):
    """已归一化的作者头像，供卡片与二维码复用。"""

    data: bytes
    mime: str


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=max(0, min(radius, size // 2)),
        fill=255,
    )
    return mask


def _qr_avatar_geometry(canvas: int) -> tuple[int, int, int]:
    size = max(1, int(canvas * _QR_AVATAR_RATIO))
    margin = max(1, int(canvas * _QR_AVATAR_MARGIN_RATIO))
    return size, margin, size + margin * 2


def _qr_module_mask(matrix: list[list[bool]], canvas: int) -> Image.Image:
    """把 QR 矩阵画成圆角方块的变形点阵，三个定位角画成圆环。

    单模块用 ``_QR_DOT_RADIUS`` 的圆角方块而不是正圆：正圆只覆盖模块
    面积的 78%，在 140px、57 模块这种高密度下会糊成灰团；圆角方块既
    保留参考实现的团块感，又能让解码器稳定采到模块中心。
    相邻黑点用同高矩形连成一片，定位角单独清空后重绘成圆环 + 圆点。
    """

    count = len(matrix)
    module = canvas / (count + _QR_QUIET_ZONE * 2)
    radius = max(0, int(module * _QR_DOT_RADIUS))
    mask = Image.new("L", (canvas, canvas), 0)
    draw = ImageDraw.Draw(mask)
    finders = ((0, 0), (0, count - 7), (count - 7, 0))

    def in_finder(x: int, y: int) -> bool:
        return any(fx <= x < fx + 7 and fy <= y < fy + 7 for fx, fy in finders)

    for y in range(count):
        row = matrix[y]
        for x in range(count):
            if not row[x] or in_finder(x, y):
                continue
            left = (x + _QR_QUIET_ZONE) * module
            top = (y + _QR_QUIET_ZONE) * module
            draw.rounded_rectangle(
                (left, top, left + module, top + module),
                radius=radius,
                fill=255,
            )

    for finder_x, finder_y in finders:
        origin_x = (finder_x + _QR_QUIET_ZONE) * module
        origin_y = (finder_y + _QR_QUIET_ZONE) * module
        draw.ellipse(
            (origin_x, origin_y, origin_x + 7 * module, origin_y + 7 * module),
            outline=255,
            width=max(1, round(module)),
        )
        draw.ellipse(
            (origin_x + 2 * module, origin_y + 2 * module, origin_x + 5 * module, origin_y + 5 * module),
            fill=255,
        )
    return mask


def _qr_avatar_layer(avatar: _Avatar, canvas: int) -> tuple[Image.Image, Image.Image]:
    """返回头像图层与需要挖空的圆角遮罩。"""

    size, margin, cut = _qr_avatar_geometry(canvas)
    cutout = Image.new("L", (canvas, canvas), 0)
    cutout.paste(
        255,
        ((canvas - cut) // 2, (canvas - cut) // 2),
        _rounded_mask(cut, int(cut * 0.2)),
    )
    source = Image.open(BytesIO(avatar.data)).convert("RGBA")
    source = source.resize((size, size), Image.Resampling.LANCZOS)
    source.putalpha(_rounded_mask(size, int(size * 0.2)))
    layer = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    layer.alpha_composite(source, ((canvas - size) // 2, (canvas - size) // 2))
    return layer, cutout


@lru_cache(maxsize=_QR_AVATAR_CACHE_SIZE)
def _qr_png(value: str, render_scale: float, avatar: _Avatar | None) -> bytes:
    code = qrcode.QRCode(
        version=None,
        error_correction=_QR_ERROR_CORRECTION,
        box_size=1,
        border=0,
    )
    code.add_data(value)
    code.make(fit=True)
    target = max(1, round(_QR_SIZE * render_scale))
    canvas = target * _QR_SUPERSAMPLE
    mask = _qr_module_mask(code.get_matrix(), canvas)
    avatar_layer: Image.Image | None = None
    if avatar is not None:
        avatar_layer, cutout = _qr_avatar_layer(avatar, canvas)
        mask.paste(0, (0, 0), cutout)
    # 先把灰度遮罩缩到目标尺寸再上色：直接对 RGBA 做 LANCZOS 会让
    # 透明的黑底和墨色一起参与插值，alpha 振铃把相邻模块糊成一片，
    # 57 模块以上的长链接就会在 2~3 px/module 时扫不出来。
    image = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    image.paste(_QR_INK, mask=mask.resize((target, target), Image.Resampling.LANCZOS))
    if avatar_layer is not None:
        image.alpha_composite(avatar_layer.resize((target, target), Image.Resampling.LANCZOS))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _qr_html(value: str, render_scale: float, avatar: _Avatar | None) -> str:
    if not value:
        return '<div class="qr-placeholder"></div>'
    return f'<img class="qr" src="{_data_uri(_qr_png(value, render_scale, avatar))}">'


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
    """规范化作者统计展示值。

    平台对部分账号只下发模糊文案（``10+``、``1千+``、``1万+``），原样展示
    模糊值比留空更符合预期；只有占位符与无法解析的形态才返回空串。
    """

    raw = (value or "").strip().replace(",", "")
    if not raw or raw in {"-", "--", "—"}:
        return ""
    if re.fullmatch(r"\d+", raw):
        return _compact_count(int(raw))
    if re.fullmatch(r"\d+(?:\.\d+)?[万亿万千kKwW]?\+?", raw):
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


def _author_stats_html(result: NoteResult, *, no_cookie_mode: bool = False) -> str:
    if no_cookie_mode:
        items = [("关注", "无"), ("粉丝", "Cookies"), ("获赞与收藏", "模式")]
    else:
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


def _avatar_html(avatar: _Avatar | None, *, mini: bool = False) -> str:
    class_name = "avatar mini-avatar" if mini else "avatar"
    if avatar is not None:
        return f'<img class="{class_name}" src="{_data_uri(avatar.data, avatar.mime)}">'
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


def _normalize_avatar(content: bytes) -> _Avatar | None:
    """把头像裁成正方形并缩到固定尺寸，避免原图 base64 撑大模板。"""

    try:
        image = Image.open(BytesIO(content)).convert("RGBA")
        width, height = image.size
        if width <= 0 or height <= 0:
            return None
        side = min(width, height)
        image = image.crop(
            ((width - side) // 2, (height - side) // 2, (width + side) // 2, (height + side) // 2)
        )
        image = image.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return _Avatar(output.getvalue(), "image/png")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        logger.debug(f"[XHSAnalyse] 作者头像解析失败：{error}")
        return None


async def _download_avatar(client: httpx.AsyncClient | None, url: str) -> _Avatar | None:
    if client is None or not url:
        return None
    try:
        response = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=httpx.Timeout(10.0, connect=3.0),
        )
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("头像超过 2 MB")
        return await asyncio.to_thread(_normalize_avatar, response.content)
    except (httpx.HTTPError, ValueError) as error:
        logger.debug(f"[XHSAnalyse] 作者头像下载失败：{error}")
        return None


def _template(
    result: NoteResult,
    media_count: int,
    avatar: _Avatar | None,
    render_scale: float,
    card_height: int = _HEIGHT,
    *,
    pill_top: int,
    no_cookie_mode: bool = False,
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
    if result.type == "video":
        media_label = "视频笔记"
    else:
        labels = []
        if result.has_live_photo:
            labels.append("含实况")
        if result.has_hdr_image:
            labels.append("HDR")
        media_label = f"图文 · {' · '.join(labels)}" if labels else "图文笔记"
    author_id = html.escape(result.author_red_id or result.author_id or result.note_id)
    generated_at = datetime.now(_CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    values = {
        "AUTHOR": author,
        "BRAND_ICON": _brand_icon_uri(render_scale),
        "AVATAR": _avatar_html(avatar),
        "MINI_AVATAR": _avatar_html(avatar, mini=True),
        "PUBLISH_TIME": publish_time,
        "LOCATION": location_html,
        "TITLE": title,
        "TAGS": html.escape(_tags(result.desc)),
        "MEDIA_LABEL": media_label,
        "DESC_PREVIEW": html.escape(_description_preview(result.desc)),
        "MEDIA_META": (
            _video_meta(result)
            if result.type == "video"
            else f"共 {media_count} 张{' · HDR' if result.has_hdr_image else ''}"
        ),
        "LIKED_COUNT": _value(result.liked_count),
        "COMMENT_COUNT": _value(result.comment_count),
        "COLLECTED_COUNT": _value(result.collected_count),
        "SHARE_COUNT": _value(result.share_count),
        "AUTHOR_ID": author_id,
        "AUTHOR_STATS": _author_stats_html(result, no_cookie_mode=no_cookie_mode),
        "GENERATED_AT": generated_at,
        "QR": _qr_html(result.share_url, render_scale, avatar),
        "CARD_HEIGHT": str(card_height),
        "PILL_TOP": str(pill_top),
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
    no_cookie_mode: bool = False,
) -> bytes | None:
    """渲染卡片；输入图片损坏或渲染器不可用时返回 ``None``。"""

    if not cover_path.is_file():
        return None
    preview_path = cover_path
    converted_preview: Path | None = None
    try:
        scale = max(1.0, min(5.0, float(render_scale)))
        if not await is_jpeg(cover_path):
            converted_preview = await ensure_jpeg(cover_path)
            if converted_preview is None:
                return None
            preview_path = converted_preview
        avatar_task = _download_avatar(client, result.author_avatar)
        size_task = asyncio.to_thread(_image_size, preview_path)
        avatar, source_size = await asyncio.gather(avatar_task, size_task)
        layout = _card_layout(source_size, result)
        card_height = layout.card_height
        render_scale = scale
        background_task = asyncio.to_thread(
            _build_background,
            preview_path,
            render_scale,
            card_height,
            layout,
        )
        template_task = asyncio.to_thread(
            _template,
            result,
            sum(1 for item in result.media if not item.is_video),
            avatar,
            render_scale,
            card_height,
            pill_top=_pill_top(layout),
            no_cookie_mode=no_cookie_mode,
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
    finally:
        if converted_preview is not None and converted_preview != cover_path:
            converted_preview.unlink(missing_ok=True)


__all__ = ["render_note_card"]

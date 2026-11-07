import base64
from io import BytesIO
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw

from XHSAnalyse.utils.card import (
    _QR_SIZE,
    _TAG_SAFE_GAP,
    _QR_QUIET_ZONE,
    _QR_ERROR_CORRECTION,
    _Avatar,
    _qr_png,
    _qr_html,
    _template,
    _edge_tint,
    _color_veil,
    _cover_mask,
    _card_layout,
    _author_count,
    _top_text_end,
    _card_geometry,
    _live_icon_uri,
    _veil_strength,
    _build_background,
    _title_line_count,
    _author_stats_html,
    _cover_fade_length,
    _adaptive_veil_peak,
    _apply_card_corners,
    _crop_rendered_card,
    _qr_avatar_geometry,
    _description_preview,
)
from XHSAnalyse.utils.parse.models import MediaItem, NoteResult


def _decode_data_uri(value: str) -> bytes:
    return base64.b64decode(value.split(",", 1)[1])


def test_background_render_scale_uses_dynamic_cover_height(tmp_path: Path) -> None:
    cover = tmp_path / "cover.png"
    Image.new("RGB", (1200, 600), "#6688aa").save(cover)

    image = Image.open(BytesIO(_build_background(cover, 1.5)))

    assert image.size == (1080, round(1080 * 1.5))


def test_card_geometry_keeps_complete_horizontal_cover() -> None:
    card_height, cover_height, cover_top = _card_geometry((1200, 600))

    assert cover_height == 360
    assert cover_top == 260
    assert card_height == 1080


def test_title_line_count_matches_card_width() -> None:
    assert _title_line_count("渐变对比测试") == 1
    assert _title_line_count("渐变对比：图片渐变开始处再快一点变白") == 2
    long_title = "这是一条特别长的笔记标题用来测试两行甚至三行标题时卡片顶部区域会不会和封面发生重叠的情况"
    assert _title_line_count(long_title) == 3


def test_card_layout_grows_with_title_lines() -> None:
    short = _note("短标题")
    long = _note("这是一条特别长的笔记标题用来测试两行甚至三行标题时卡片顶部区域会不会和封面发生重叠的情况")
    short_layout = _card_layout((1080, 1440), short)
    long_layout = _card_layout((1080, 1440), long)

    assert _top_text_end(short.title) < _top_text_end(long.title)
    assert short_layout.cover_top < long_layout.cover_top
    assert short_layout.card_height < long_layout.card_height
    assert short_layout.footer_space == long_layout.footer_space


def test_cover_top_leaves_positive_gap_below_tags() -> None:
    # tag 下沿必须完全落在封面渐隐起点之上，且留出正间距；
    # 底部简介行到白纱起点也是正间距，两侧才不会一紧一松。
    for title in (
        "短标题",
        "这是一条特别长的笔记标题用来测试两行甚至三行标题时卡片顶部区域会不会和封面发生重叠的情况",
    ):
        note = _note(title)
        layout = _card_layout((1080, 1440), note)
        assert layout.cover_top >= _top_text_end(title) + _TAG_SAFE_GAP


def test_card_layout_shrinks_footer_without_author_stats() -> None:
    with_stats = _note("短标题")
    without_stats = _note("短标题", author_follows="", author_fans="", author_like_and_collect="")
    taller = _card_layout((1080, 1440), with_stats)
    shorter = _card_layout((1080, 1440), without_stats)

    assert shorter.footer_space < taller.footer_space
    assert shorter.card_height < taller.card_height


def test_cover_mask_is_symmetric_and_compact() -> None:
    mask = _cover_mask(1, 401)
    assert all(mask.getpixel((0, row)) == mask.getpixel((0, 400 - row)) for row in range(201))

    fade = _cover_fade_length(401)
    assert fade == 40
    assert mask.getpixel((0, 0)) == 0
    assert mask.getpixel((0, fade)) == 255


def test_cover_mask_takes_explicit_device_fades() -> None:
    # 逻辑 100px 的顶部渐隐在 2 倍分辨率下应该是 200 设备像素，
    # 这样同一张封面换分辨率时渐变比例不变。
    low = _cover_mask(1, 400, 100, 100)
    high = _cover_mask(1, 800, 200, 200)

    # 采样点差半像素，允许 2 级量化误差
    assert abs(low.getpixel((0, 50)) - high.getpixel((0, 100))) <= 2
    assert abs(low.getpixel((0, 99)) - high.getpixel((0, 198))) <= 2
    assert high.getpixel((0, 199)) == 255


def test_cover_fade_length_is_bounded() -> None:
    assert _cover_fade_length(200) == 40
    assert _cover_fade_length(1000) == 75
    assert _cover_fade_length(2000) == 90


def test_cover_mask_can_extend_bottom_fade() -> None:
    default = _cover_mask(1, 200, 40, 40)
    extended = _cover_mask(1, 200, 40, 100)

    assert default.getpixel((0, 150)) == 255
    assert extended.getpixel((0, 150)) < 255


def test_veil_strength_uses_short_peak_and_long_tail() -> None:
    assert _veil_strength(0, 150, 64, 30, 190) == 180
    assert _veil_strength(64, 150, 64, 30, 190) < 30
    assert _veil_strength(300, 150, 64, 30, 190) == 0


def test_adaptive_veil_peak_washes_light_images_less() -> None:
    assert _adaptive_veil_peak(20, 168, 90, 0.40) > _adaptive_veil_peak(220, 168, 90, 0.40)


def _solid(color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (32, 32), color)


def test_edge_tint_keeps_luminance_but_carries_hue() -> None:
    tint = _edge_tint(_solid((16, 16, 72)), 0, 32)
    # 亮度仍然接近白，只有色相被带出来：蓝通道最高、整体 >= 230
    assert min(tint) >= 230
    assert tint[2] > tint[0] and tint[2] > tint[1]


def test_edge_tint_of_gray_cover_stays_neutral() -> None:
    red, green, blue = _edge_tint(_solid((128, 128, 128)), 0, 32)
    assert abs(red - green) <= 1 and abs(green - blue) <= 1
    assert min(red, green, blue) >= 250


def test_color_veil_paints_with_the_extension_tint() -> None:
    veil = _color_veil(8, 1200, 100, 1000, 150.0, 170.0, (255, 240, 235), (240, 245, 255))
    assert veil.getpixel((0, 40))[:3] == (255, 240, 235)
    assert veil.getpixel((0, 1160))[:3] == (240, 245, 255)
    # 两段渐变的长尾都会在封面中段回落到 0，中间露出完整封面
    assert veil.getpixel((0, 550))[3] == 0


def test_qr_png_is_transparent_and_scaled() -> None:
    image = Image.open(BytesIO(_qr_png("https://example.com/note", 1.5, None))).convert("RGBA")

    assert image.size == (round(_QR_SIZE * 1.5), round(_QR_SIZE * 1.5))
    assert image.getpixel((0, 0))[3] == 0
    # 墨点覆盖率落在合理区间：太少说明模块没画出来，太多说明连成一片
    alpha = image.getchannel("A")
    histogram = alpha.histogram()
    inked = sum(histogram[129:])
    ratio = inked / (image.width * image.height)
    assert 0.12 < ratio < 0.55


def test_qr_png_keeps_quiet_zone_and_finder_rings() -> None:
    url = "https://example.com/note"
    image = Image.open(BytesIO(_qr_png(url, 1.0, None))).convert("RGBA")
    alpha = image.getchannel("A")

    code = qrcode.QRCode(version=None, error_correction=_QR_ERROR_CORRECTION, box_size=1, border=0)
    code.add_data(url)
    code.make(fit=True)
    count = len(code.get_matrix())
    module = image.width / (count + _QR_QUIET_ZONE * 2)
    margin = int(module * _QR_QUIET_ZONE)

    # 静区四边都必须留空，否则弱解码器找不到边界；上限 24 是缩放振铃的溢出
    assert alpha.crop((0, 0, image.width, margin)).getextrema()[1] <= 24
    assert alpha.crop((0, image.height - margin, image.width, image.height)).getextrema()[1] <= 24
    # 定位角是圆环 + 中心圆点：环心必实，环与圆点之间的空隙必空
    center = round((_QR_QUIET_ZONE + 3.5) * module)
    assert alpha.getpixel((center, center)) > 200
    # 沿水平方向偏离环心 2 个模块：落在中心圆点（1.5m）与环内沿（2.5m）之间的空隙
    gap = round((_QR_QUIET_ZONE + 5.5) * module)
    assert alpha.getpixel((gap, center)) < 60


def test_qr_png_embeds_avatar_at_center() -> None:
    avatar = _avatar_asset()
    image = Image.open(BytesIO(_qr_png("https://example.com/note", 1.0, avatar))).convert("RGBA")
    center = image.getpixel((_QR_SIZE // 2, _QR_SIZE // 2))

    assert center[3] > 200
    assert center[0] > 180
    assert center[1] < 120


def test_qr_avatar_is_circular_like_the_card_avatars() -> None:
    avatar = _avatar_asset()
    image = Image.open(BytesIO(_qr_png("https://example.com/note", 1.0, avatar))).convert("RGBA")
    size, _margin, _cut = _qr_avatar_geometry(_QR_SIZE)
    left = (_QR_SIZE - size) // 2
    alpha = image.getchannel("A")

    # 方形四角必须被切掉，上边缘正中与中心保持实心——这就是圆
    assert alpha.getpixel((left + 1, left + 1)) < 40
    assert alpha.getpixel((left + size - 2, left + 1)) < 40
    assert alpha.getpixel((left + size // 2, left + 2)) > 200
    assert alpha.getpixel((left + size // 2, left + size // 2)) > 200


def test_qr_html_marks_missing_link_with_transparent_placeholder() -> None:
    assert _qr_html("", 1.0, None) == '<div class="qr-placeholder"></div>'
    assert 'class="qr"' in _qr_html("https://example.com/note", 1.0, None)


def _avatar_asset() -> _Avatar:
    image = Image.new("RGBA", (64, 64), "#ff3366")
    output = BytesIO()
    image.save(output, format="PNG")
    return _Avatar(output.getvalue(), "image/png")


def _note(title: str, **kwargs: str) -> NoteResult:
    values = dict(
        note_id="note-layout",
        title=title,
        author="作者",
        desc="正文 #标签",
        publish_time="2026-09-16 12:00",
        type="image",
        video_quality=None,
        media=(MediaItem("https://img.example/cover.jpg"),),
        author_follows="123",
        author_fans="4567",
        author_like_and_collect="89万",
    )
    values.update(kwargs)
    return NoteResult(**values)


def test_live_icon_render_scale_produces_high_resolution_asset() -> None:
    image = Image.open(BytesIO(_decode_data_uri(_live_icon_uri(2.0))))

    assert image.size == (46, 46)


def test_card_corners_are_transparent() -> None:
    source = BytesIO()
    Image.new("RGB", (720, 1549), "#abcdef").save(source, format="PNG")

    image = Image.open(BytesIO(_apply_card_corners(source.getvalue(), 1.0)))

    assert image.mode == "RGBA"
    corner = image.getpixel((0, 0))
    center = image.getpixel((360, 774))
    assert isinstance(corner, tuple)
    assert isinstance(center, tuple)
    assert corner[3] == 0
    assert center[3] == 255


def test_low_scale_renderer_resize_keeps_full_card() -> None:
    source = Image.new("RGBA", (720, 100), "#000000")
    ImageDraw.Draw(source).rectangle((0, 0, 359, 99), fill="#abcdef")
    encoded = BytesIO()
    source.save(encoded, format="PNG")

    cropped = Image.open(BytesIO(_crop_rendered_card(encoded.getvalue(), 360, 100))).convert("RGBA")

    assert cropped.size == (360, 100)
    assert cropped.getpixel((90, 50))[:3] == (171, 205, 239)
    assert cropped.getpixel((359, 50))[:3] == (0, 0, 0)


def test_author_stats_keep_icon_and_number_in_one_column() -> None:
    result = NoteResult(
        note_id="note-1",
        title="标题",
        author="作者",
        desc="",
        publish_time="",
        type="image",
        video_quality=None,
        media=(MediaItem("https://img.example/cover.jpg"),),
        author_follows="10+",
        author_fans="1千+",
        author_like_and_collect="1万+",
    )

    rendered = _author_stats_html(result)

    assert rendered.count('class="author-stat"') == 3
    assert rendered.count('class="author-stat-icon"') == 3
    assert rendered.count('class="author-stat-head"') == 3
    assert rendered.count("<strong>") == 3


def test_author_stats_format_exact_counts_without_plus_suffix() -> None:
    assert _author_count("194") == "194"
    assert _author_count("17230") == "1.7万"
    assert _author_count("354715") == "35.5万"


def test_author_count_accepts_fuzzy_platform_values() -> None:
    """平台模糊文案（10+ / 1千+ / 1万+）原样展示，不再显示为 —。"""

    assert _author_count("10+") == "10+"
    assert _author_count("1千+") == "1千+"
    assert _author_count("1万+") == "1万+"
    assert _author_count("10.5万") == "10.5万"
    assert _author_count("1.2万+") == "1.2万+"
    # 占位符与垃圾值仍返回空串，卡片层继续显示 —。
    assert _author_count("-") == ""
    assert _author_count("--") == ""
    assert _author_count("—") == ""
    assert _author_count("") == ""
    assert _author_count("abc") == ""


def test_author_stats_render_fuzzy_values() -> None:
    """带模糊值的作者三项应渲染出数字而非 —。"""

    result = NoteResult(
        note_id="note-fuzzy",
        title="标题",
        author="作者",
        desc="",
        publish_time="",
        type="image",
        video_quality=None,
        media=(MediaItem("https://img.example/cover.jpg"),),
        author_follows="10+",
        author_fans="1千+",
        author_like_and_collect="1万+",
    )

    rendered = _author_stats_html(result)

    assert "10+" in rendered
    assert "1千+" in rendered
    assert "1万+" in rendered
    assert "—" not in rendered


def test_author_stats_show_no_cookie_mode() -> None:
    result = NoteResult(
        note_id="note-no-cookie",
        title="标题",
        author="作者",
        desc="",
        publish_time="",
        type="image",
        video_quality=None,
        media=(MediaItem("https://img.example/cover.jpg"),),
    )

    rendered = _author_stats_html(result, no_cookie_mode=True)

    assert "无" in rendered
    assert "Cookies" in rendered
    assert "模式" in rendered
    assert "—" not in rendered
    assert rendered.count('class="author-stat"') == 3


def test_video_card_footer_is_not_labeled_as_image_note() -> None:
    result = NoteResult(
        note_id="note-video",
        title="视频标题",
        author="作者",
        desc="这是一段视频正文摘要",
        publish_time="",
        ip_location="中国台湾",
        type="video",
        video_quality="720p",
        media=(
            MediaItem("https://img.example/cover.jpg"),
            MediaItem("https://video.example/main.mp4", is_video=True),
        ),
    )

    rendered = _template(result, 1, None, 1.0, pill_top=400)

    assert '<div class="desc-preview">这是一段视频正文摘要</div>' in rendered
    assert "720P" in rendered


def test_video_meta_uses_four_k_and_hdr_label() -> None:
    result = NoteResult(
        note_id="note-video-4k",
        title="视频标题",
        author="作者",
        desc="",
        publish_time="",
        type="video",
        video_quality="2160p HDR",
        media=(MediaItem("https://video.example/main.mp4", is_video=True, is_hdr=True),),
    )

    rendered = _template(result, 0, None, 1.0, pill_top=400)

    assert "4K HDR" in rendered


def test_image_card_shows_live_and_hdr_labels() -> None:
    result = NoteResult(
        note_id="note-live-hdr",
        title="图文",
        author="作者",
        desc="正文",
        publish_time="",
        type="image",
        video_quality=None,
        media=(
            MediaItem("https://img.example/live.jpg", is_live=True, live_url="https://video.example/live.mp4"),
            MediaItem("https://ci.xiaohongshu.com/notes_uhdr/hdr", is_hdr=True),
        ),
    )

    rendered = _template(result, 2, None, 1.0, pill_top=400)

    assert "图文 · 含实况 · HDR" in rendered
    assert "共 2 张 · HDR" in rendered


def test_card_shows_ip_location_next_to_publish_time() -> None:
    result = NoteResult(
        note_id="note-location",
        title="标题",
        author="作者",
        desc="",
        publish_time="2026-09-14 12:00",
        type="image",
        video_quality=None,
        media=(MediaItem("https://img.example/cover.jpg"),),
        ip_location="中国台湾",
    )

    rendered = _template(result, 1, None, 1.0, pill_top=400)

    assert 'class="location-icon"' in rendered
    assert 'class="location-name">中国台湾</span>' in rendered
    assert '<span class="publish-time">2026-09-14 12:00</span>' in rendered


def test_card_uses_plugin_icon_for_brand_mark() -> None:
    result = NoteResult(
        note_id="note-brand",
        title="标题",
        author="作者",
        desc="",
        publish_time="",
        type="image",
        video_quality=None,
        media=(MediaItem("https://img.example/cover.jpg"),),
    )

    rendered = _template(result, 1, None, 1.0, pill_top=400)

    assert '<img class="brand-icon"' in rendered
    assert 'alt="XHSAnalyse"' in rendered
    assert '<div class="brand">小红书</div>' not in rendered


def test_card_places_caption_above_qr_code() -> None:
    result = NoteResult(
        note_id="note-qr-caption",
        title="标题",
        author="作者",
        desc="",
        publish_time="",
        type="image",
        video_quality=None,
        media=(MediaItem("https://img.example/cover.jpg"),),
        share_url="https://www.xiaohongshu.com/explore/note-qr-caption",
    )

    rendered = _template(result, 1, None, 1.0, pill_top=400)

    assert '<div class="qr-wrap"><div class="qr-caption">扫码直达笔记</div>' in rendered


def test_description_preview_is_limited_and_omits_tags() -> None:
    preview = _description_preview("正文内容 #话题 " + "很长" * 30)

    assert "#话题" not in preview
    assert len(preview) <= 32
    assert preview.endswith("…")

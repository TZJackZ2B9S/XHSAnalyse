import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from XHSAnalyse.utils.card import (
    _VEIL_TOP_FADE,
    _VEIL_TOP_PEAK,
    _VEIL_BOTTOM_PEAK,
    _VEIL_BOTTOM_RAMP,
    _template,
    _cover_mask,
    _card_layout,
    _author_count,
    _top_text_end,
    _card_geometry,
    _live_icon_uri,
    _foreground_mask,
    _build_background,
    _title_line_count,
    _author_stats_html,
    _apply_card_corners,
    _crop_rendered_card,
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
    long_title = (
        "这是一条特别长的笔记标题用来测试两行甚至三行标题时"
        "卡片顶部区域会不会和封面发生重叠的情况"
    )
    assert _title_line_count(long_title) == 3


def test_card_layout_grows_with_title_lines() -> None:
    short = _note("短标题")
    long = _note(
        "这是一条特别长的笔记标题用来测试两行甚至三行标题时"
        "卡片顶部区域会不会和封面发生重叠的情况"
    )
    short_layout = _card_layout((1080, 1440), short)
    long_layout = _card_layout((1080, 1440), long)

    assert _top_text_end(short.title) < _top_text_end(long.title)
    assert short_layout.cover_top < long_layout.cover_top
    assert short_layout.card_height < long_layout.card_height
    assert short_layout.footer_space == long_layout.footer_space


def test_card_layout_shrinks_footer_without_author_stats() -> None:
    with_stats = _note("短标题")
    without_stats = _note("短标题", author_follows="", author_fans="", author_like_and_collect="")
    taller = _card_layout((1080, 1440), with_stats)
    shorter = _card_layout((1080, 1440), without_stats)

    assert shorter.footer_space < taller.footer_space
    assert shorter.card_height < taller.card_height


def test_card_cover_and_veil_transitions_are_symmetric() -> None:
    cover_mask = _cover_mask(1, 101)
    assert all(
        cover_mask.getpixel((0, row)) == cover_mask.getpixel((0, 100 - row))
        for row in range(51)
    )

    foreground = _foreground_mask(1, 400, 100, 200)
    assert all(
        foreground.getpixel((0, 64 + offset)) == foreground.getpixel((0, 336 - offset))
        for offset in range(127)
    )
    assert _VEIL_TOP_PEAK == _VEIL_BOTTOM_PEAK
    assert _VEIL_TOP_FADE == _VEIL_BOTTOM_RAMP


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
    assert _author_count("10+") == ""


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

    rendered = _template(result, 1, "", 1.0)

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

    rendered = _template(result, 0, "", 1.0)

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

    rendered = _template(result, 2, "", 1.0)

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

    rendered = _template(result, 1, "", 1.0)

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

    rendered = _template(result, 1, "", 1.0)

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

    rendered = _template(result, 1, "", 1.0)

    assert '<div class="qr-wrap"><div class="qr-caption">扫码直达笔记</div>' in rendered


def test_description_preview_is_limited_and_omits_tags() -> None:
    preview = _description_preview("正文内容 #话题 " + "很长" * 30)

    assert "#话题" not in preview
    assert len(preview) <= 32
    assert preview.endswith("…")

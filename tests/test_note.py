import json

from xhs_core.parse.note import (
    collect_media,
    format_timestamp,
    build_ci_image_url,
    extract_note_from_html,
)


def test_build_ci_image_url() -> None:
    url = "https://sns-webpic-qc.xhscdn.com/202608170504/88fc0d553d26b84b69f9b1cdc2b18cf5/1040g0083239pc13o6u005q30lhg6r903fvom6t8!nd_dft_wgth_webp_3"
    assert build_ci_image_url(url) == (
        "https://ci.xiaohongshu.com/1040g0083239pc13o6u005q30lhg6r903fvom6t8",
        "1040g0083239pc13o6u005q30lhg6r903fvom6t8",
    )


def test_extract_note_from_initial_state() -> None:
    note = {"noteId": "0123456789abcdef01234567", "title": "测试", "imageList": []}
    document = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps({"note": {"noteDetailMap": {"x": {"note": note}}}})
        + "</script>"
    )
    assert extract_note_from_html(document) == note


def test_collect_media_builds_live_photo() -> None:
    result = collect_media(
        "0123456789abcdef01234567",
        {
            "type": "normal",
            "title": "测试",
            "user": {"nickname": "作者"},
            "imageList": [
                {
                    "urlDefault": "https://sns-webpic.xhscdn.com/1/abc/cover!nd_dft_wgth_webp_3",
                    "livePhoto": True,
                    "stream": {
                        "h265": [
                            {
                                "masterUrl": "https://video/live.mp4",
                                "width": 1080,
                                "height": 1920,
                                "hdrType": 2,
                            }
                        ]
                    },
                }
            ],
        },
    )
    assert result.type == "image"
    assert result.has_live_photo
    assert result.media[0].url == "https://ci.xiaohongshu.com/cover"
    assert result.media[0].live_url == "https://video/live.mp4"


def test_collect_media_skips_slideshow_video_for_image_set() -> None:
    result = collect_media(
        "0123456789abcdef01234567",
        {
            "type": "video",
            "title": "图集",
            "user": {"nickname": "作者"},
            "imageList": [
                {"urlDefault": "https://img/a.jpg"},
                {"urlDefault": "https://img/b.jpg"},
            ],
            "video": {"media": {"stream": {"h264": [{"masterUrl": "https://video/slideshow.mp4"}]}}},
        },
    )
    assert result.type == "image"
    assert len(result.media) == 2
    assert not any(item.is_video for item in result.media)


def test_collect_media_can_disable_original_image() -> None:
    result = collect_media(
        "0123456789abcdef01234567",
        {
            "type": "normal",
            "title": "测试",
            "user": {"nickname": "作者"},
            "imageList": [{"urlDefault": "https://sns-webpic.xhscdn.com/1/abc/cover!nd_dft_wgth_webp_3"}],
        },
        prefer_original_image=False,
    )
    assert result.media[0].url == "https://sns-webpic.xhscdn.com/1/abc/cover!nd_dft_wgth_webp_3"


def test_format_timestamp_supports_seconds_and_iso() -> None:
    assert format_timestamp(1_700_000_000) == "2023-11-15 06:13"
    assert format_timestamp("2026-08-13T00:00:00Z") == "2026-08-13 08:00"

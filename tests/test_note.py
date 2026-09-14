import json

from XHSAnalyse.utils.parse.note import (
    UA_NOTE,
    collect_media,
    format_timestamp,
    build_ci_image_url,
    extract_note_from_html,
    extract_author_profile_from_html,
)


def test_note_request_uses_desktop_ua_for_full_video_manifest() -> None:
    assert "Windows NT" in UA_NOTE
    assert "Chrome/" in UA_NOTE
    assert "Mobile" not in UA_NOTE


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


def test_collect_media_does_not_treat_non_live_image_stream_as_live() -> None:
    result = collect_media(
        "0123456789abcdef01234567",
        {
            "type": "normal",
            "title": "HDR 图片",
            "user": {"nickname": "作者"},
            "imageList": [
                {
                    "urlDefault": "https://sns-webpic.xhscdn.com/notes_uhdr/cover!nd_dft_wgth_webp_3",
                    "livePhoto": False,
                    "stream": {"h265": [{"masterUrl": "https://video/not-live.mp4", "hdrType": 1}]},
                }
            ],
        },
    )

    assert not result.has_live_photo
    assert result.has_hdr_image
    assert result.media[0].is_hdr


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


def test_collect_media_extracts_card_metadata() -> None:
    result = collect_media(
        "0123456789abcdef01234567",
        {
            "type": "normal",
            "title": "测试卡片",
            "desc": "正文 #标签",
            "ipLocation": "中国台湾",
            "user": {
                "nickname": "作者",
                "userId": "user-1",
                "avatar": {"urlDefault": "https://avatar.example/a.jpg"},
            },
            "interactInfo": {
                "likedCount": "15",
                "commentCount": 16,
                "collectedCount": "2",
                "shareCount": 6,
            },
            "imageList": [{"urlDefault": "https://img.example/a.jpg"}],
        },
        share_url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
    )
    assert result.liked_count == "15"
    assert result.comment_count == "16"
    assert result.collected_count == "2"
    assert result.share_count == "6"
    assert result.author_id == "user-1"
    assert result.author_avatar == "https://avatar.example/a.jpg"
    assert result.share_url.endswith("01234567")
    assert result.ip_location == "中国台湾"


def test_collect_media_extracts_optional_author_profile_stats() -> None:
    result = collect_media(
        "0123456789abcdef01234567",
        {
            "title": "带作者资料",
            "user": {"nickname": "作者", "userId": "user-1"},
            "profile": {
                "userInfo": {
                    "follows": "12",
                    "fans": "3.4万",
                    "likeAndCollect": "8.9万",
                }
            },
            "imageList": [],
        },
    )

    assert result.author_follows == "12"
    assert result.author_fans == "3.4万"
    assert result.author_like_and_collect == "8.9万"


def test_collect_media_ignores_unavailable_author_profile_stats() -> None:
    result = collect_media(
        "0123456789abcdef01234567",
        {
            "title": "无作者资料",
            "user": {"nickname": "作者"},
            "profile": {"userInfo": {"follows": "-", "fans": "-", "likeAndCollect": "-"}},
            "imageList": [],
        },
    )

    assert result.author_follows == ""
    assert result.author_fans == ""
    assert result.author_like_and_collect == ""


def test_extract_author_profile_stats() -> None:
    document = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "user": {
                    "userPageData": {
                        "basicInfo": {"redId": "4519166676"},
                        "interactions": [
                            {"type": "follows", "name": "关注", "count": "10+"},
                            {"type": "fans", "name": "粉丝", "count": "1千+"},
                            {"type": "interaction", "name": "获赞与收藏", "count": "1万+"},
                        ],
                    }
                }
            }
        )
        + "</script>"
    )

    assert extract_author_profile_from_html(document) == {
        "red_id": "4519166676",
        "follows": "10+",
        "fans": "1千+",
        "like_and_collect": "1万+",
    }


def test_format_timestamp_supports_seconds_and_iso() -> None:
    assert format_timestamp(1_700_000_000) == "2023-11-15 06:13"
    assert format_timestamp("2026-08-13T00:00:00Z") == "2026-08-13 08:00"

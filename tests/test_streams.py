from XHSAnalyse.utils.parse.video import get_best_video_url
from XHSAnalyse.utils.parse.streams import select_stream, select_live_stream


def _stream(url: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "masterUrl": url,
        "width": 1920,
        "height": 1080,
        "videoCodec": "h264",
        "videoBitrate": 1000,
    }
    value.update(overrides)
    return value


def test_snake_case_stream_prefers_hdr_then_resolution() -> None:
    result = select_stream(
        {
            "h265": [
                {"master_url": "https://video/sdr-4k.mp4", "width": 3840, "height": 2160, "hdr_type": 0},
                {"master_url": "https://video/hdr-1080.mp4", "width": 1920, "height": 1080, "hdr_type": 3},
            ]
        },
        video_only=True,
    )
    assert result is not None
    assert result.url == "https://video/hdr-1080.mp4"
    assert result.quality == "1920p HDR"
    assert result.is_hdr


def test_stream_prefers_playable_mp4_over_dash_track() -> None:
    result = select_stream(
        {
            "h265": [
                {"master_url": "https://v/high.m4s", "width": 3840, "height": 2160, "video_bitrate": 9000},
                {"master_url": "https://v/usable.mp4", "width": 1920, "height": 1080, "video_bitrate": 5000},
            ]
        }
    )
    assert result is not None
    assert result.url == "https://v/usable.mp4"


def test_live_stream_falls_back_to_ef_family() -> None:
    result = select_live_stream(
        {
            "EF4": [{"masterUrl": "https://v/ef4.mp4", "videoBitrate": 2000}],
            "EF5": [{"masterUrl": "https://v/ef5.mp4", "videoBitrate": 5000}],
        }
    )
    assert result is not None
    assert result.url == "https://v/ef5.mp4"


def test_video_page_hdr_wins_over_origin() -> None:
    note = {
        "video": {
            "consumer": {"originVideoKey": "origin.mp4"},
            "media": {"stream": {"h265": [_stream("https://video/hdr.mp4", hdrType=2)]}},
        }
    }
    result = get_best_video_url(note)
    assert result is not None
    assert result.url == "https://video/hdr.mp4"
    assert result.quality == "1080p HDR"


def test_video_page_uses_origin_without_hdr() -> None:
    note = {
        "video": {
            "consumer": {"originVideoKey": "origin.mp4"},
            "media": {"stream": {"h264": [_stream("https://video/sdr.mp4")]}},
        }
    }
    result = get_best_video_url(note)
    assert result is not None
    assert result.url == "https://sns-video-bd.xhscdn.com/origin.mp4"
    assert result.quality == "origin"


def test_video_page_avoids_ef4_when_other_sdr_exists() -> None:
    note = {
        "video": {
            "media": {
                "stream": {
                    "EF4": [_stream("https://video/ef4.mp4", videoBitrate=9000)],
                    "EF5": [_stream("https://video/ef5.mp4", videoBitrate=1000)],
                }
            }
        }
    }
    result = get_best_video_url(note)
    assert result is not None
    assert result.url == "https://video/ef5.mp4"


def test_video_quality_limit_selects_closest_below_target() -> None:
    note = {
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        _stream("https://video/2160.mp4", width=3840, height=2160),
                        _stream("https://video/1080.mp4", width=1920, height=1080),
                        _stream("https://video/720.mp4", width=1280, height=720),
                    ]
                }
            }
        }
    }
    result = get_best_video_url(note, max_height=1080)
    assert result is not None
    assert result.url == "https://video/1080.mp4"
    assert result.quality == "1080p"


def test_video_quality_limit_falls_back_to_source_maximum() -> None:
    note = {
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        _stream("https://video/720.mp4", width=1280, height=720),
                        _stream("https://video/480.mp4", width=854, height=480),
                    ]
                }
            }
        }
    }
    result = get_best_video_url(note, max_height=2160)
    assert result is not None
    assert result.url == "https://video/720.mp4"
    assert result.quality == "720p"


def test_video_quality_limit_720_caps_4k_source() -> None:
    note = {
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        _stream("https://video/2160.mp4", width=3840, height=2160),
                        _stream("https://video/720.mp4", width=1280, height=720),
                    ]
                }
            }
        }
    }
    result = get_best_video_url(note, max_height=720)
    assert result is not None
    assert result.url == "https://video/720.mp4"


def test_hdr_preference_can_be_disabled() -> None:
    note = {
        "video": {
            "media": {
                "stream": {
                    "h265": [
                        _stream("https://video/hdr.mp4", hdrType=2),
                        _stream("https://video/sdr.mp4", hdrType=0, videoBitrate=5000),
                    ]
                }
            }
        }
    }
    result = get_best_video_url(note, prefer_hdr=False)
    assert result is not None
    assert result.url == "https://video/sdr.mp4"


def test_real_hdr_video_prefers_4k_hdr_over_plain_4k() -> None:
    """真实档位：4K HDR 码率低于普通 4K，但仍应优先 HDR。"""

    note = {
        "video": {
            "media": {
                "stream": {
                    "EF5": [
                        _stream(
                            "https://video/4k-sdr.mp4",
                            width=3840,
                            height=2160,
                            videoCodec="EF5",
                            videoBitrate=2943775,
                            hdrType=0,
                        ),
                        _stream(
                            "https://video/4k-hdr.mp4",
                            width=3840,
                            height=2160,
                            videoCodec="EF5",
                            videoBitrate=2357731,
                            hdrType=1,
                        ),
                    ],
                    "EF4": [
                        _stream(
                            "https://video/720.mp4",
                            width=1280,
                            height=720,
                            videoCodec="EF4",
                            videoBitrate=1235213,
                        )
                    ],
                }
            }
        }
    }
    result = get_best_video_url(note)
    assert result is not None
    assert result.url == "https://video/4k-hdr.mp4"
    assert result.quality == "2160p HDR"
    assert result.is_hdr


def test_real_hdr_video_quality_limit_still_prefers_hdr() -> None:
    """选择 1080p 时只能在 1080p 及以下取流，且 HDR 优先。"""

    note = {
        "video": {
            "media": {
                "stream": {
                    "EF5": [
                        _stream("https://video/4k-sdr.mp4", width=3840, height=2160, videoCodec="EF5"),
                        _stream("https://video/1080-sdr.mp4", width=1920, height=1080, videoCodec="EF5"),
                    ],
                    "EF4": [
                        _stream(
                            "https://video/1080-hdr.mp4",
                            width=1920,
                            height=1080,
                            videoCodec="EF5",
                            hdrType=1,
                            videoBitrate=500000,
                        )
                    ],
                }
            }
        }
    }
    result = get_best_video_url(note, max_height=1080)
    assert result is not None
    assert result.url == "https://video/1080-hdr.mp4"
    assert result.quality == "1080p HDR"

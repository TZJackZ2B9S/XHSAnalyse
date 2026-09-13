import json

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


def test_video_page_uses_quality_candidate_even_with_origin_key() -> None:
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
    assert result.is_hdr


def test_video_page_uses_origin_without_hdr() -> None:
    note = {
        "video": {
            "consumer": {"originVideoKey": "origin.mp4"},
            "media": {"stream": {"h264": [_stream("https://video/sdr.mp4")]}},
        }
    }
    result = get_best_video_url(note)
    assert result is not None
    assert result.url == "https://video/sdr.mp4"
    assert result.quality == "1080p"


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


def test_origin_stream_is_used_for_4k_when_signed_candidates_are_720p() -> None:
    """原始视频只有 4K、签名候选只有 720p 时，4K 档位不能降级。"""

    note = {
        "video": {
            "consumer": {"originVideoKey": "original.mp4"},
            "media": {
                "stream": {
                    "h264": [_stream("https://video/signed-720.mp4", width=1280, height=720)]
                }
            },
            "mediaV2": json.dumps(
                {
                    "video": {"width": 3840, "height": 2160},
                    "stream": {
                        "h264": [_stream("https://video/signed-720.mp4", width=1280, height=720)]
                    },
                }
            ),
        }
    }
    result_4k = get_best_video_url(note, max_height=2160)
    assert result_4k is not None
    assert result_4k.url == "https://sns-video-bd.xhscdn.com/original.mp4"
    assert result_4k.quality == "2160p"

    result_1080 = get_best_video_url(note, max_height=1080)
    assert result_1080 is not None
    assert result_1080.url == "https://video/signed-720.mp4"
    assert result_1080.quality == "720p"


def test_source_shortage_falls_back_to_720p_candidate() -> None:
    note = {
        "video": {
            "consumer": {"originVideoKey": "original.mp4"},
            "media": {
                "stream": {
                    "h264": [_stream("https://video/720.mp4", width=720, height=1280)],
                    "h265": [_stream("https://video/720-h265.mp4", width=720, height=1280, hdrType=1)],
                }
            },
        }
    }
    for max_height in (720, 1080, 1440, 2160):
        result = get_best_video_url(note, max_height=max_height, prefer_hdr=True)
        assert result is not None
        assert result.url.endswith("/720-h265.mp4")
        assert result.quality == "720p HDR"


def test_media_v2_raw_stream_matches_configured_quality() -> None:
    note = {
        "video": {
            "consumer": {"originVideoKey": "original.mp4"},
            "media": {"stream": {"h264": [_stream("https://video/page-720.mp4", width=1280, height=720)]}},
            "mediaV2": json.dumps(
                {
                    "video": {"width": 3840, "height": 2160},
                    "stream": {
                        "EF5": [
                            _stream("https://video/raw-1080.mp4", width=1920, height=1080, videoCodec="EF5"),
                            _stream("https://video/raw-1440.mp4", width=2560, height=1440, videoCodec="EF5"),
                            _stream("https://video/raw-2160.mp4", width=3840, height=2160, videoCodec="EF5"),
                        ]
                    },
                }
            ),
        }
    }
    expected = {
        720: ("https://video/page-720.mp4", "720p"),
        1080: ("https://video/raw-1080.mp4", "1080p"),
        1440: ("https://video/raw-1440.mp4", "1440p"),
        2160: ("https://video/raw-2160.mp4", "2160p"),
    }
    for target, (url, quality) in expected.items():
        result = get_best_video_url(note, max_height=target)
        assert result is not None
        assert result.url == url
        assert result.quality == quality


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


def test_quality_target_beats_lower_resolution_hdr_stream() -> None:
    """HDR 仅用于同档位择优，不能把 1080p/2K 降成 720p。"""

    note = {
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        _stream("https://video/1080-sdr.mp4", width=1920, height=1080, hdrType=0),
                        _stream("https://video/720-hdr.mp4", width=1280, height=720, hdrType=1),
                    ]
                }
            }
        }
    }
    result = get_best_video_url(note, max_height=1080, prefer_hdr=True)
    assert result is not None
    assert result.url == "https://video/1080-sdr.mp4"
    assert result.quality == "1080p"
    assert not result.is_hdr


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

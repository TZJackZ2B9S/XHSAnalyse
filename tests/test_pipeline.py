from pathlib import Path

from xhs_core.parse.models import MediaItem, NoteResult
from xhs_core.media.pipeline import PreparedMedia, build_info_text, build_forward_message


def _result() -> NoteResult:
    return NoteResult(
        note_id="0123456789abcdef01234567",
        title="标题",
        author="作者",
        desc="描述",
        publish_time="2026-08-13 08:00",
        type="video",
        video_quality="2160p HDR",
        media=(
            MediaItem("https://img/cover.jpg", is_cover=True),
            MediaItem("https://video/main.mp4", is_video=True, quality="2160p HDR"),
        ),
    )


def test_forward_message_packs_info_and_cover_in_first_node(tmp_path: Path) -> None:
    cover = tmp_path / "cover.jpg"
    video = tmp_path / "main.mp4"
    cover.write_bytes(b"cover")
    video.write_bytes(b"video")
    result = _result()
    media = (
        PreparedMedia(cover, result.media[0], 0, False),
        PreparedMedia(video, result.media[1], 1, True),
    )
    forward = build_forward_message(result, media, build_info_text(result, media))
    assert forward.type == "node"
    assert isinstance(forward.data, list)
    assert forward.data[0].type == "node"
    assert forward.data[0].data[0].type == "text"
    assert "标题: 标题" in forward.data[0].data[0].data
    assert forward.data[0].data[1].type == "image"
    assert forward.data[1].type == "video"


def test_info_text_mentions_live_hdr_and_cookie(tmp_path: Path) -> None:
    result = NoteResult(
        note_id="0123456789abcdef01234567",
        title="Live",
        author="作者",
        desc="",
        publish_time="",
        type="video",
        video_quality="1080p",
        media=(MediaItem("https://img/live.jpg", is_live=True, live_url="https://v/live.mp4", is_hdr=True),),
        cookie_expired=True,
    )
    text = build_info_text(result, ())
    assert "视频画质: 1080p" in text
    assert "Live 图" in text
    assert "HDR" in text
    assert "Cookie" in text

import asyncio
from pathlib import Path

from XHSAnalyse.utils.parse.models import MediaItem, NoteResult
from XHSAnalyse.utils.media.download import _cleanup_tasks
from XHSAnalyse.utils.media.pipeline import (
    Message,
    PreparedMedia,
    cleanup_media,
    build_info_text,
    build_note_message,
    build_media_message,
    select_media_for_delivery,
)


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


def test_note_message_uses_card_as_standalone_image() -> None:
    message = build_note_message("unused", b"card")

    assert message.type == "image"
    assert message.data == "base64://Y2FyZA=="


def test_note_message_falls_back_to_standalone_text() -> None:
    message = build_note_message("标题: 标题")

    assert message.type == "text"
    assert message.data == "标题: 标题"


def test_single_media_message_is_not_wrapped_in_forward(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"cover")
    media = (PreparedMedia(image, MediaItem("https://img/cover.jpg"), 0, False),)

    message = build_media_message(media)

    assert message.type == "image"
    assert message.data == "base64://Y292ZXI="


def test_multiple_media_message_contains_only_all_media(tmp_path: Path) -> None:
    cover = tmp_path / "cover.jpg"
    video = tmp_path / "main.mp4"
    cover.write_bytes(b"cover")
    video.write_bytes(b"video")
    result = _result()
    media = (
        PreparedMedia(cover, result.media[0], 0, False),
        PreparedMedia(video, result.media[1], 1, True),
    )
    forward = build_media_message(media)

    assert forward.type == "node"
    assert isinstance(forward.data, list)
    assert [item.type for item in forward.data] == ["image", "video"]


def test_video_delivery_sends_video_without_cover(tmp_path: Path) -> None:
    cover = tmp_path / "cover.jpg"
    video = tmp_path / "main.mp4"
    cover.write_bytes(b"cover")
    video.write_bytes(b"video")
    result = _result()
    media = (
        PreparedMedia(cover, result.media[0], 0, False),
        PreparedMedia(video, result.media[1], 1, True),
    )

    delivery = select_media_for_delivery(result, media)
    message = build_media_message(delivery)

    assert delivery == (media[1],)
    assert message.type == "video"


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


def test_info_text_mentions_actual_lower_than_target() -> None:
    result = NoteResult(
        note_id="0123456789abcdef01234567",
        title="视频",
        author="作者",
        desc="",
        publish_time="",
        type="video",
        video_quality="720p",
        media=(MediaItem("https://video/main.mp4", is_video=True, quality="720p", width=1280, height=720),),
        target_video_height=1080,
    )
    text = build_info_text(result, ())
    assert "目标画质 1080p" in text
    assert "源视频最高仅 720p" in text


def test_forward_message_respects_file_video_send_type(tmp_path: Path) -> None:
    video = tmp_path / "main.mp4"
    video.write_bytes(b"video")
    result = NoteResult(
        note_id="0123456789abcdef01234567",
        title="视频",
        author="作者",
        desc="",
        publish_time="",
        type="video",
        video_quality="1080p",
        media=(MediaItem("https://video/main.mp4", is_video=True, quality="1080p"),),
    )
    media = (PreparedMedia(video, result.media[0], 0, True),)
    message = asyncio.run(_build_file_media(media))
    assert message.type == "video"
    assert str(message.data).startswith("file://localhost/")


async def _build_file_media(
    media: tuple[PreparedMedia, ...],
) -> Message:
    """在事件循环内构建 file 媒体消息，并清理测试产生的延迟删除任务。"""

    message = build_media_message(media, video_send_type="file")
    await asyncio.sleep(0)
    for task in list(_cleanup_tasks):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return message


def test_cleanup_media_removes_base64_video(tmp_path: Path) -> None:
    video = tmp_path / "main.mp4"
    image = tmp_path / "cover.jpg"
    video.write_bytes(b"video")
    image.write_bytes(b"image")
    result = _result()
    media = (
        PreparedMedia(image, result.media[0], 0, False),
        PreparedMedia(video, result.media[1], 1, True),
    )
    cleanup_media(media)
    assert not image.exists()
    assert not video.exists()


def test_cleanup_media_keeps_file_sent_video(tmp_path: Path) -> None:
    video = tmp_path / "main.mp4"
    video.write_bytes(b"video")
    media = (PreparedMedia(video, _result().media[1], 1, True),)

    asyncio.run(_cleanup_file_media(media))

    assert video.exists()


async def _cleanup_file_media(media: tuple[PreparedMedia, ...]) -> None:
    cleanup_media(media, video_send_type="file")
    await asyncio.sleep(0)
    for task in list(_cleanup_tasks):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

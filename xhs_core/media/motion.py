"""小红书 Live Photo 合成。"""

from pathlib import Path

import aiofiles

from .image import ensure_jpeg

_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


def build_motion_photo_xmp(video_length: int) -> str:
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.1.0-jc003">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/" '
        'xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" '
        'xmlns:OpCamera="http://ns.oplus.com/photos/1.0/camera/" '
        'xmlns:Container="http://ns.google.com/photos/1.0/container/" '
        'xmlns:Item="http://ns.google.com/photos/1.0/container/item/" hdrgm:Version="1.0" '
        'GCamera:MotionPhoto="1" GCamera:MotionPhotoVersion="1" '
        'GCamera:MotionPhotoPresentationTimestampUs="0" '
        'OpCamera:MotionPhotoPrimaryPresentationTimestampUs="0" OpCamera:MotionPhotoOwner="oplus" '
        f'OpCamera:OLivePhotoVersion="2" OpCamera:VideoLength="{video_length}">'
        "<Container:Directory><rdf:Seq>"
        '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="image/jpeg" '
        'Item:Semantic="Primary" Item:Length="0" Item:Padding="0" /></rdf:li>'
        '<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="video/mp4" '
        f'Item:Semantic="MotionPhoto" Item:Length="{video_length}" /></rdf:li>'
        "</rdf:Seq></Container:Directory></rdf:Description></rdf:RDF></x:xmpmeta>"
    )


def inject_xmp(jpeg: bytes, packet: str) -> bytes:
    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("输入不是 JPEG")
    payload = _XMP_HEADER + packet.encode()
    if len(payload) + 2 > 65535:
        raise ValueError("XMP 数据过大")
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big")
    return jpeg[:2] + app1 + payload + jpeg[2:]


async def build_motion_photo(image_path: Path, video_path: Path, output_path: Path) -> bool:
    """把静态图和 MP4 合成为 Motion Photo JPEG。"""

    jpeg_path = await ensure_jpeg(image_path)
    if jpeg_path is None:
        return False
    try:
        async with aiofiles.open(jpeg_path, "rb") as file:
            jpeg = await file.read()
        async with aiofiles.open(video_path, "rb") as file:
            video = await file.read()
        if not video:
            return False
        combined = inject_xmp(jpeg, build_motion_photo_xmp(len(video))) + video
        async with aiofiles.open(output_path, "wb") as file:
            await file.write(combined)
        return True
    finally:
        if jpeg_path != image_path:
            jpeg_path.unlink(missing_ok=True)

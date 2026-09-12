from xhs_core.media.motion import inject_xmp, build_motion_photo_xmp


def test_inject_xmp_builds_valid_app1_segment() -> None:
    jpeg = b"\xff\xd8\xff\xd9"
    packet = build_motion_photo_xmp(1234)
    result = inject_xmp(jpeg, packet)
    assert result.startswith(b"\xff\xd8\xff\xe1")
    length = int.from_bytes(result[4:6], "big")
    assert result[4 + length :] == jpeg[2:]
    assert b"GCamera:MotionPhoto=\"1\"" in result
    assert b"OpCamera:VideoLength=\"1234\"" in result


def test_inject_xmp_rejects_non_jpeg() -> None:
    try:
        inject_xmp(b"not-jpeg", build_motion_photo_xmp(1))
    except ValueError as error:
        assert str(error) == "输入不是 JPEG"
    else:
        raise AssertionError("应拒绝非 JPEG 输入")

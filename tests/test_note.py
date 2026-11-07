import json
import asyncio

import httpx

from XHSAnalyse.utils.parse.note import (
    UA_NOTE,
    CODE_LOGIN,
    CODE_COOKIE,
    CODE_CAPTCHA,
    CODE_NOTE_ID,
    CODE_UNKNOWN,
    SKIPPABLE_CODES,
    CODE_UNAVAILABLE,
    NoteParseError,
    user_hint,
    parse_note,
    _page_error,
    classify_page,
    collect_media,
    effective_page,
    format_timestamp,
    build_ci_image_url,
    note_error_message,
    is_forged_unavailable,
    should_report_failure,
    extract_note_from_html,
    _sanitize_platform_message,
    extract_author_profile_from_html,
)
from XHSAnalyse.utils.parse.models import NoteResult


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


def test_precise_profile_counts_accepts_fuzzy_platform_values() -> None:
    """otherinfo 接口下发模糊文案（100+ / 1千+ / 1万+）时原样保留，不再丢弃。"""

    from XHSAnalyse.utils.parse.note import _precise_profile_counts

    payload = {
        "data": {
            "user": {
                "follows": "100+",
                "fans": "1千+",
                "interactions": "1万+",
            }
        }
    }
    assert _precise_profile_counts(payload) == {
        "follows": "100+",
        "fans": "1千+",
        "like_and_collect": "1万+",
    }
    # 带小数与单位后缀的形态同样接受。
    decimal_payload = {"data": {"user": {"fans": "10.5万"}}}
    assert _precise_profile_counts(decimal_payload) == {"fans": "10.5万"}
    # 精确整数路径保持不变（含千分位）。
    exact_payload = {"data": {"user": {"follows": "1,234", "fans": 5678}}}
    assert _precise_profile_counts(exact_payload) == {"follows": "1234", "fans": "5678"}
    # 占位符与垃圾值依旧丢弃。
    junk_payload = {"data": {"user": {"follows": "-", "fans": "--", "interactions": "abc"}}}
    assert _precise_profile_counts(junk_payload) == {}


def test_format_timestamp_supports_seconds_and_iso() -> None:
    assert format_timestamp(1_700_000_000) == "2023-11-15 06:13"
    assert format_timestamp("2026-08-13T00:00:00Z") == "2026-08-13 08:00"


# ── 失败页面分类：登录墙 / 验证码 标记只出现在重定向 URL 上，不在正文里 ──


def _response(url: str, *, text: str = "", status: int = 200) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url), text=text)


def test_classify_page_detects_login_wall_from_redirect_url() -> None:
    response = _response(
        "https://www.xiaohongshu.com/login?redirectPath=https%3A%2F%2Fwww.xiaohongshu.com%2Fexplore%2Fabc",
        text="<html><script>window.__INITIAL_STATE__={}</script></html>",
    )
    assert classify_page(response) == "LOGIN"


def test_classify_page_detects_captcha_from_redirect_url() -> None:
    response = _response("https://www.xiaohongshu.com/website-login/captcha?redirectPath=x")
    assert classify_page(response) == "CAPTCHA"


def test_classify_page_detects_unavailable_note() -> None:
    response = _response("https://www.xiaohongshu.com/404?source=/404/sec_abc&error_code=300031&error_msg=x")
    assert classify_page(response) == "UNAVAILABLE"


def test_classify_page_keeps_normal_note_as_ok() -> None:
    response = _response(
        "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc",
        text='<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{}}}</script>',
    )
    assert classify_page(response) == "OK"


def test_classify_page_does_not_mistake_note_body_mentioning_login() -> None:
    """正文里出现 /login 字样的正常笔记页不能被误判成登录墙。"""

    response = _response(
        "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc",
        text='<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{}}}</script><a href="/login">登录</a>',
    )
    assert classify_page(response) == "OK"


# ── 错误码与可跳过判定 ──


def test_login_wall_without_cookie_reports_need_login() -> None:
    response = _response("https://www.xiaohongshu.com/login?redirectPath=x")
    error = _page_error(response, has_token=True, cookie="")
    assert error.code == CODE_LOGIN
    assert "Cookies" in str(error)


def test_login_wall_with_cookie_asks_to_renew_cookie() -> None:
    response = _response("https://www.xiaohongshu.com/login?redirectPath=x")
    hint = user_hint(_page_error(response, has_token=True, cookie="a1=stale"))
    assert "重新获取" in hint


def test_login_wall_hint_tells_user_to_configure_cookie_when_absent() -> None:
    response = _response("https://www.xiaohongshu.com/login?redirectPath=x")
    hint = user_hint(_page_error(response, has_token=True, cookie=""))
    assert "Cookies" in hint


def test_normal_note_with_error_code_zero_is_not_treated_as_unavailable() -> None:
    """小红书分享链接常带 error_code=0，不能据此判为下架或风控。"""

    response = _response("https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc?xsec_token=ABC=&error_code=0")
    assert classify_page(response) == "OK"
    assert _page_error(response, has_token=True, cookie="").code != CODE_UNAVAILABLE


def test_skippable_codes_exclude_only_captcha() -> None:
    assert CODE_CAPTCHA not in SKIPPABLE_CODES
    assert SKIPPABLE_CODES == frozenset({CODE_NOTE_ID, CODE_LOGIN, CODE_COOKIE, CODE_UNAVAILABLE, CODE_UNKNOWN})


def test_user_hint_explains_login_requirement_instead_of_invalid_link() -> None:
    response = _response("https://www.xiaohongshu.com/login?redirectPath=x")
    hint = user_hint(_page_error(response, has_token=True, cookie=""))
    assert "需要登录" in hint
    assert "链接是否有效" not in hint


def test_user_hint_falls_back_to_original_message_for_unmapped_code() -> None:
    assert user_hint(NoteParseError("自定义失败原因", CODE_CAPTCHA)) == "❌ 自定义失败原因"


def test_login_error_is_skippable_so_other_candidates_still_get_tried() -> None:
    """同一条消息可能混有仅该条要求登录的笔记，登录失败不应终止其余候选。"""

    assert CODE_LOGIN in SKIPPABLE_CODES


def test_captcha_is_not_skippable_to_avoid_hammering_risk_control() -> None:
    """风控是出口 IP 级别的限制，继续试其它候选只会加重风控。"""

    assert CODE_CAPTCHA not in SKIPPABLE_CODES


# ── parse_note 控制流：无登录回退与多候选跳过 ──


def _note_page(note_id: str, title: str) -> str:
    note = {
        "noteId": note_id,
        "title": title,
        "user": {"nickname": "作者"},
        "imageList": [{"urlDefault": "https://img/a.jpg"}],
    }
    state = {"note": {"noteDetailMap": {"x": {"note": note}}}}
    return "<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>"


NOTE_PUBLIC = "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc?xsec_token=BBBBBBBB="


def test_parse_note_falls_back_to_anonymous_when_cookie_hits_login_wall() -> None:
    """Cookies 失效表现为登录墙时必须删掉 Cookies 重试，否则配置项形同虚设。"""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # 只记录目标笔记页请求：httpx 跟随 302 时会丢掉显式 Cookie 头，
        # 若把重定向后的请求也算进来，空串会来自登录页而非匿名重试。
        if request.url.path.startswith("/explore/"):
            seen.append("anon" if not request.headers.get("cookie") else "cookie")
        if request.headers.get("cookie"):
            return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login?redirectPath=x"})
        return httpx.Response(200, text=_note_page("6abcb8ce000000001c01cbdc", "公开笔记"))

    async def run() -> NoteResult:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            return await parse_note(client, NOTE_PUBLIC, "a1=stale")

    result = asyncio.run(run())
    assert result.title == "公开笔记"
    assert result.cookie_expired is True
    assert seen == ["cookie", "anon"], f"应当先带 Cookie 再匿名重试，实际 {seen}"


def test_parse_note_does_not_claim_cookie_failure_when_anonymous_also_hits_login_wall() -> None:
    """匿名重试仍撞登录墙说明该笔记要求登录，此时不能引导用户去换 Cookies。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/login"):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login?redirectPath=x"})

    async def run() -> NoteParseError:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            try:
                await parse_note(client, NOTE_PUBLIC, "a1=some-cookie")
            except NoteParseError as error:
                return error
            raise AssertionError("应当抛出 NoteParseError")

    error = asyncio.run(run())
    assert error.code == CODE_LOGIN
    assert "重新获取 Cookies" not in str(error), "匿名同样受限时不该建议换 Cookies"


def test_parse_note_without_cookie_reports_need_login() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/login"):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login?redirectPath=x"})

    async def run() -> NoteParseError:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            try:
                await parse_note(client, NOTE_PUBLIC, "")
            except NoteParseError as error:
                return error
            raise AssertionError("应当抛出 NoteParseError")

    error = asyncio.run(run())
    assert error.code == CODE_LOGIN
    assert "链接是否有效" not in user_hint(error)


def test_parse_note_rejects_url_without_note_id() -> None:
    async def run() -> NoteParseError:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text=_note_page("6abcb8ce000000001c01cbdc", "公开笔记"))
        )
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            try:
                await parse_note(client, "https://www.xiaohongshu.com/explore/not-a-note-id", "")
            except NoteParseError as error:
                return error
            raise AssertionError("应当抛出 NoteParseError")

    assert asyncio.run(run()).code == CODE_NOTE_ID


def test_error_code_variants_are_classified_by_numeric_value() -> None:
    """分享链接常带 error_code=0，只有非零值才代表笔记不可浏览。"""

    base = "https://www.xiaohongshu.com/explore/abc"
    for query in ("error_code=0", "error_code=00", "error_code=0.0", "error_code=0&x=1", "error_code=0;x=1"):
        assert classify_page(_response(f"{base}?{query}")) == "OK", query
    for query in ("error_code=1", "error_code=300031", "errorCode=300031"):
        assert classify_page(_response(f"{base}?{query}")) == "UNAVAILABLE", query


def test_slash_404_sec_in_query_value_is_not_treated_as_blocked_page() -> None:
    """只有路径命中 /404/sec 才算风控页，参数值里出现该串不算。"""

    assert classify_page(_response("https://www.xiaohongshu.com/404/sec_abc")) == "UNAVAILABLE"
    assert classify_page(_response("https://www.xiaohongshu.com/explore/abc?redirect=/404/sec")) == "OK"


def test_classify_page_and_unfollowed_redirect_agree_on_same_url() -> None:
    """跟随与未跟随重定向必须用同一套判据，否则会出现判定分叉。"""

    targets = (
        "https://www.xiaohongshu.com/login?redirectPath=x",
        "https://www.xiaohongshu.com/website-login/captcha?redirectPath=x",
        "https://www.xiaohongshu.com/404/sec_x?error_code=300031",
        "https://www.xiaohongshu.com/explore/abc?error_code=00",
        "https://www.xiaohongshu.com/explore/abc?errorCode=300031",
        "https://www.xiaohongshu.com/explore/abc",
    )
    for target in targets:
        followed = _response(target)
        raw = httpx.Response(
            302,
            request=httpx.Request("GET", "https://www.xiaohongshu.com/explore/abc"),
            headers={"location": target},
        )
        assert classify_page(followed) == classify_page(raw), target


def test_parse_note_without_token_falls_back_to_anonymous_when_cookie_expired() -> None:
    """无 token 直链同样要有匿名回落，否则 Cookies 失效会被误报成 Cookies 无效。"""

    direct = "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/explore/"):
            seen.append("anon" if not request.headers.get("cookie") else "cookie")
        if request.headers.get("cookie"):
            return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login?redirectPath=x"})
        return httpx.Response(200, text=_note_page("6abcb8ce000000001c01cbdc", "公开笔记"))

    async def run() -> NoteResult:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            return await parse_note(client, direct, "a1=stale")

    result = asyncio.run(run())
    assert result.title == "公开笔记"
    assert result.cookie_expired is True
    assert seen == ["cookie", "anon"], f"无 token 直链也应匿名回落，实际 {seen}"


def test_redirect_response_is_classified_without_following_redirects() -> None:
    """调用方未跟随重定向时，登录墙不能被降级成「链接无效」。"""

    login = httpx.Response(
        302,
        request=httpx.Request("GET", "https://www.xiaohongshu.com/explore/abc"),
        headers={"location": "https://www.xiaohongshu.com/login?redirectPath=x"},
    )
    assert classify_page(login) == "LOGIN"

    captcha = httpx.Response(
        302,
        request=httpx.Request("GET", "https://www.xiaohongshu.com/explore/abc"),
        headers={"location": "https://www.xiaohongshu.com/website-login/captcha?redirectPath=x"},
    )
    assert classify_page(captcha) == "CAPTCHA"

    gone = httpx.Response(
        302,
        request=httpx.Request("GET", "https://www.xiaohongshu.com/explore/abc"),
        headers={"location": "https://www.xiaohongshu.com/404/sec_x?error_code=300031"},
    )
    assert classify_page(gone) == "UNAVAILABLE"


def test_error_code_with_empty_or_non_numeric_value_is_not_treated_as_unavailable() -> None:
    """`?error_code=` 这类后端拼参未给值的情况不代表笔记下架。"""

    base = "https://www.xiaohongshu.com/explore/abc"
    for query in ("error_code=", "error_code=nan", "error_code=inf", "error_code=abc"):
        assert classify_page(_response(f"{base}?{query}")) == "OK", query


# ── 第三种失效形态：302 → /explore?...&undertake_note_error=... ──
# 落地页是正常的 explore 首页，noteDetailMap 有条目但 .note 是空对象，
# 只有 undertake_note_error 参数能说明笔记取不到。

_UNAVAILABLE_QUERY = (
    "xsec_token=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA%3D"
    "&target_note_id=6abc7c36000000001203f294"
    "&undertake_note_error=%25E8%25AF%25A5%25E5%2586%2585%25E5%25AE%25B9%25E6%259A%2582%25E6%2597%25B6%25E6%2597%25A0%25E6%25B3%2595%25E6%259F%25A5%25E7%259C%258B"
)
_UNAVAILABLE_URL = f"https://www.xiaohongshu.com/explore?{_UNAVAILABLE_QUERY}"


def _empty_note_state() -> str:
    """复刻不可用笔记的页面：noteDetailMap 有条目，但 .note 为空对象。"""

    state = {
        "note": {
            "noteDetailMap": {
                "6abc7c36000000001203f294": {"comments": {}, "currentTime": 1, "note": {}},
            }
        }
    }
    return "<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>"


def test_classify_page_detects_undertake_note_error() -> None:
    assert classify_page(_response(_UNAVAILABLE_URL)) == "UNAVAILABLE"


def test_classify_page_detects_single_encoded_undertake_note_error() -> None:
    """httpx 只解一层编码，单层编码的形态同样要能识别。"""

    url = (
        "https://www.xiaohongshu.com/explore"
        "?undertake_note_error=%E8%AF%A5%E5%86%85%E5%AE%B9%E6%9A%82%E6%97%B6%E6%97%A0%E6%B3%95%E6%9F%A5%E7%9C%8B"
    )
    assert classify_page(_response(url)) == "UNAVAILABLE"


def test_classify_page_ignores_empty_undertake_note_error() -> None:
    """参数存在但为空不代表笔记不可用。"""

    assert classify_page(_response("https://www.xiaohongshu.com/explore?undertake_note_error=")) == "OK"
    assert classify_page(_response("https://www.xiaohongshu.com/explore")) == "OK"


def test_note_error_message_decodes_chinese_text() -> None:
    assert note_error_message(httpx.URL(_UNAVAILABLE_URL)) == "该内容暂时无法查看"
    assert note_error_message(httpx.URL("https://www.xiaohongshu.com/explore")) == ""


def test_empty_note_detail_map_is_not_treated_as_a_note() -> None:
    """`.note` 是空对象时不能把外层壳字段当成笔记返回。"""

    assert extract_note_from_html(_empty_note_state()) is None


def test_note_detail_map_still_extracts_real_note() -> None:
    state = {
        "note": {
            "noteDetailMap": {
                "6abc7c36000000001203f294": {
                    "comments": {},
                    "currentTime": 1,
                    "note": {"noteId": "6abc7c36000000001203f294", "title": "正常笔记", "imageList": []},
                }
            }
        }
    }
    document = "<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>"
    note = extract_note_from_html(document)
    assert note is not None
    assert note["noteId"] == "6abc7c36000000001203f294"


def _redirected_response(target: str, *, text: str) -> httpx.Response:
    """构造「服务端把我们 302 走了」的响应，history 非空才是真不可用。"""

    origin = httpx.Request("GET", "https://www.xiaohongshu.com/explore/6abc7c36000000001203f294")
    hop = httpx.Response(302, request=origin, headers={"location": target})
    return httpx.Response(200, request=httpx.Request("GET", target), text=text, history=[hop])


def test_unavailable_note_reports_platform_message_instead_of_zero_media() -> None:
    """这条失败必须报「取不到内容」，而不是让用户以为重试能解决。"""

    response = _redirected_response(_UNAVAILABLE_URL, text=_empty_note_state())
    error = _page_error(response, has_token=True, cookie="a1=x")
    assert error.code == CODE_UNAVAILABLE
    assert "该内容暂时无法查看" in str(error)
    assert "未能下载" not in user_hint(error)


def test_unavailable_note_error_is_skippable_but_captcha_is_not() -> None:
    assert CODE_UNAVAILABLE in SKIPPABLE_CODES


def test_parse_note_reports_unavailable_note_via_redirect_chain() -> None:
    """不可用笔记经 302 落到 explore 首页，必须报原文而非「没能下载媒体」。"""

    note_id = "6abc7c36000000001203f294"
    target = (
        f"https://www.xiaohongshu.com/explore?target_note_id={note_id}"
        "&undertake_note_error=%E8%AF%A5%E5%86%85%E5%AE%B9%E6%9A%82%E6%97%B6%E6%97%A0%E6%B3%95%E6%9F%A5%E7%9C%8B"
    )
    empty_state = _empty_note_state()

    def handler(request: httpx.Request) -> httpx.Response:
        if "undertake_note_error" in str(request.url):
            return httpx.Response(200, text=empty_state)
        return httpx.Response(302, headers={"location": target})

    async def run() -> NoteParseError:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            try:
                await parse_note(client, f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=AB=", "a1=x")
            except NoteParseError as error:
                return error
            raise AssertionError("应当抛出 NoteParseError")

    error = asyncio.run(run())
    assert error.code == CODE_UNAVAILABLE
    assert "该内容暂时无法查看" in str(error)


# ── 伪造标记防护：undertake_note_error 位于 URL 上，任何人都能自己拼 ──


def test_forged_note_error_with_parseable_note_is_not_unavailable() -> None:
    """正常笔记加个人造参数：笔记明明取得到，不能被判成不可用。"""

    forged = _UNAVAILABLE_URL + "&fake=1"
    origin = httpx.Request("GET", "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc")
    body = _note_page("6abcb8ce000000001c01cbdc", "正常笔记")
    response = httpx.Response(200, request=httpx.Request("GET", forged), text=body)
    response.history = [httpx.Response(302, request=origin, headers={"location": forged})]
    assert extract_note_from_html(response.text) is not None
    assert is_forged_unavailable(response) is True
    assert effective_page(response) == "OK"


def test_note_error_without_redirect_is_treated_as_forged() -> None:
    """请求直接落在带该参数的页面上，说明参数是请求方自己带过去的。"""

    response = httpx.Response(
        200,
        request=httpx.Request("GET", _UNAVAILABLE_URL),
        text=_empty_note_state(),
    )
    assert not response.history
    assert is_forged_unavailable(response) is True
    assert effective_page(response) == "OK"


def test_genuine_note_error_from_server_redirect_is_kept() -> None:
    """服务端确实 302 且落地页取不到笔记时，该标记必须被采信。"""

    response = _redirected_response(_UNAVAILABLE_URL, text=_empty_note_state())
    assert is_forged_unavailable(response) is False
    assert effective_page(response) == "UNAVAILABLE"


def test_platform_message_is_sanitized_before_broadcast() -> None:
    """平台文案源自 URL，不能原样进群：去链接、去 @、压成一行、限长。"""

    payload = "@%E5%85%A8%E4%BD%93%E6%88%90%E5%91%98%20%E7%82%B9%E6%AD%A4http://evil.example/x%20%E9%A2%86%E5%8F%96%0A%E7%AC%AC%E4%BA%8C%E8%A1%8C"
    url = httpx.URL(f"https://www.xiaohongshu.com/explore?undertake_note_error={payload}")
    cleaned = note_error_message(url)
    assert "@" not in cleaned
    assert "http" not in cleaned
    assert "\n" not in cleaned
    assert len(cleaned) <= 40


def test_sanitize_keeps_ordinary_platform_message() -> None:
    assert _sanitize_platform_message("该内容暂时无法查看") == "该内容暂时无法查看"


# ── 无 Cookies 登录墙：必须给出可操作提示且一定发声 ──


def test_login_wall_without_cookie_is_reported_to_user() -> None:
    """未配置 Cookies 时撞登录墙，必须告诉用户去配置，而不是静默。"""

    response = _redirected_response(
        "https://www.xiaohongshu.com/login?redirectPath=x",
        text="<html></html>",
    )
    error = _page_error(response, has_token=True, cookie="")
    assert error.code == CODE_LOGIN
    hint = user_hint(error)
    assert "需要登录" in hint
    assert "Cookies" in hint
    assert should_report_failure(error, notify=False) is True


def test_login_wall_from_plain_redirect_is_detected() -> None:
    """最朴素的 302 → /login（无 token、无 cookie）也要识别。"""

    response = _redirected_response(
        "https://www.xiaohongshu.com/login?redirectPath=%2Fexplore%2Fabc",
        text="<html></html>",
    )
    assert effective_page(response) == "LOGIN"
    error = _page_error(response, has_token=False, cookie="")
    assert error.code == CODE_LOGIN
    assert should_report_failure(error, notify=False) is True


def test_parse_flow_uses_effective_page_not_raw_classify() -> None:
    """解析流程必须走 effective_page，否则伪造标记会被当真。"""

    note_id = "6abcb8ce000000001c01cbdc"
    forged = (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        "?xsec_token=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
        "&undertake_note_error=%E8%AF%A5%E5%86%85%E5%AE%B9%E6%9A%82%E6%97%B6%E6%97%A0%E6%B3%95%E6%9F%A5%E7%9C%8B"
    )
    body = _note_page(note_id, "正常笔记")

    def handler(_request: httpx.Request) -> httpx.Response:
        # 服务端从不重定向，页面里笔记完好；伪造只体现在 URL 参数上
        return httpx.Response(200, text=body)

    async def run() -> NoteResult:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            return await parse_note(client, forged, "a1=x")

    result = asyncio.run(run())
    assert result.note_id == note_id
    assert result.title == "正常笔记"


def test_sanitize_truncates_overlong_platform_message() -> None:
    assert len(_sanitize_platform_message("错" * 200)) == 40


def test_forged_marker_does_not_block_real_note_error_reporting() -> None:
    """URL 带伪造标记、页面又确实取不到笔记时，走 _page_error 裁决。

    这是 effective_page 真正生效的层次：首轮循环拿不到笔记后，
    兜底裁决必须无视伪造标记，按真实原因报错而不是误报「笔记不可用」。
    """

    forged = (
        "https://www.xiaohongshu.com/explore/6abc7c36000000001203f294"
        "?xsec_token=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        "&undertake_note_error=%E8%AF%A5%E5%86%85%E5%AE%B9%E6%9A%82%E6%97%B6%E6%97%A0%E6%B3%95%E6%9F%A5%E7%9C%8B"
    )

    # 页面里既没有笔记、也不是登录墙/风控页：真实原因是「取不到笔记数据」
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><script>window.__INITIAL_STATE__={}</script></html>")

    async def run() -> NoteParseError:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            try:
                await parse_note(client, forged, "a1=x")
            except NoteParseError as error:
                return error
            raise AssertionError("应当抛出 NoteParseError")

    error = asyncio.run(run())
    assert error.code == CODE_UNKNOWN
    assert "该内容暂时无法查看" not in str(error)


def test_forged_marker_does_not_force_extra_request_when_fallback_disabled() -> None:
    """关掉无登录回退时，伪造标记不能让正常笔记解析失败。

    若首轮用 classify_page 而非 effective_page，伪造参数会把页面判成不可用，
    笔记不会被提取；回退开关关闭时就没有任何补救路径，正常笔记直接失败。
    """

    note_id = "6abcb8ce000000001c01cbdc"
    forged = (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        "?xsec_token=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
        "&undertake_note_error=%E8%AF%A5%E5%86%85%E5%AE%B9"
    )
    body = _note_page(note_id, "正常笔记")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=body)

    async def run() -> NoteResult:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            return await parse_note(client, forged, "a1=x", fallback_without_cookie=False)

    result = asyncio.run(run())
    assert result.note_id == note_id
    assert len(requests) == 1, f"应当一次拿到笔记，实际请求 {len(requests)} 次"


# ── error_code 与 undertake_note_error 必须用同一套来源校验 ──
# 只防其中一个，另一个就是现成的绕过通道：任何人追加 &error_code=1
# 就能让正常笔记被报成「已下架」，同时这条笔记彻底解析不出来。


def _bare_response(url: str, text: str) -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("GET", url), text=text)


def test_forged_error_code_with_parseable_note_is_not_unavailable() -> None:
    note_id = "6abcb8ce000000001c01cbdc"
    url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=CB5="
    body = _note_page(note_id, "正常笔记")
    for query in ("&error_code=1", "&error_code=300031", "&errorCode=300031"):
        response = _bare_response(url + query, body)
        assert classify_page(response) == "UNAVAILABLE", query
        assert is_forged_unavailable(response) is True, query
        assert effective_page(response) == "OK", query


def test_forged_error_code_does_not_break_note_parsing() -> None:
    """笔记完好时，追加伪造 error_code 不能阻断解析。"""

    note_id = "6abcb8ce000000001c01cbdc"
    url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=CB5=&error_code=1"
    body = _note_page(note_id, "正常笔记")

    async def run() -> NoteResult:
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, text=body))
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            return await parse_note(client, url, "a1=x", fallback_without_cookie=False)

    assert asyncio.run(run()).title == "正常笔记"


def test_genuine_error_code_from_server_redirect_is_kept() -> None:
    """服务端 302 到带 error_code 的 404 页时，必须仍然判为不可用。"""

    target = "https://www.xiaohongshu.com/404?source=/404/sec_x&error_code=300031"
    response = _redirected_response(target, text="<html></html>")
    assert is_forged_unavailable(response) is False
    assert effective_page(response) == "UNAVAILABLE"


def test_forged_error_code_without_redirect_and_without_note_is_still_trusted_as_failure() -> None:
    """无重定向、无笔记、只有 error_code：不能据此报「已下架」，交由兜底处理。"""

    response = _bare_response("https://www.xiaohongshu.com/explore/abc?error_code=1", "<html></html>")
    assert effective_page(response) == "OK"


def test_sanitize_blocks_group_message_injection_payloads() -> None:
    """该文案源自 URL，必须挡住常见的群消息注入载荷。"""

    payloads = [
        "[CQ:at,qq=all] 点此领取",
        "&#91;CQ:at,qq=all&#93; 领取",
        "＠全体成员",
        "@\u200b全体成员",
        "hxxp://evil.example/x",
        "www.evil.example/x",
        "//evil.example/x",
        "[链接](//evil.example/x)",
        "控制\x00字符\x07",
        "第一行\n第二行",
    ]
    for payload in payloads:
        cleaned = _sanitize_platform_message(payload)
        # CQ 码依赖 `[CQ:key=value]` 的冒号与等号，标点被剥离后已无法被适配器解析
        assert "[CQ:at" not in cleaned and "CQ:at,qq=all" not in cleaned, payload
        assert "@" not in cleaned and "＠" not in cleaned, payload
        assert "\u200b" not in cleaned, payload
        assert ":" not in cleaned and "=" not in cleaned, payload
        assert "http" not in cleaned and "www" not in cleaned, payload
        assert "//" not in cleaned, payload
        assert "\n" not in cleaned and "\x00" not in cleaned, payload


def test_sanitize_preserves_ordinary_chinese_platform_message() -> None:
    assert _sanitize_platform_message("该内容暂时无法查看") == "该内容暂时无法查看"
    assert _sanitize_platform_message("笔记已被作者删除") == "笔记已被作者删除"

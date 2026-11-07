import json
import asyncio

import httpx

from gsuid_core.models import Event, Message
from XHSAnalyse.utils.parse.note import (
    CODE_LOGIN,
    CODE_COOKIE,
    CODE_CAPTCHA,
    CODE_NOTE_ID,
    CODE_UNKNOWN,
    ERROR_PRIORITY,
    SKIPPABLE_CODES,
    CODE_UNAVAILABLE,
    NoteParseError,
    pick_error,
    _page_error,
    parse_first_valid,
    should_report_failure,
)
from XHSAnalyse.utils.parse.urls import extract_urls, extract_note_urls
from XHSAnalyse.utils.parse.models import NoteResult
from XHSAnalyse.utils.parse.message import event_link_text


def test_auto_detection_ignores_links_inside_forwarded_nodes() -> None:
    event = Event(
        content=[
            Message(
                type="node",
                data=[{"type": "text", "data": "转发内容 https://xhslink.cn/o/inside"}],
            )
        ]
    )

    assert event_link_text(event) == ""


_SHARED_CARD = {
    "app": "com.tencent.structmsg",
    "desc": "新闻",
    "view": "news",
    "prompt": "[分享] 你曾经养过我哦，好久不见",
    "meta": {
        "news": {
            "title": "你曾经养过我哦，好久不见",
            "desc": "#爱弥斯 #cos #鸣潮",
            "jumpUrl": "https://www.xiaohongshu.com/discovery/item/68f1a2b3c4d5e6f708091a2b?xsec_token=ABxyz123",
            "preview": "https://sns-webpic-qc.xhscdn.com/preview.jpg",
            "tag": "小红书",
        }
    },
}


def test_shared_card_segment_yields_the_note_link() -> None:
    # 群友转发的小红书卡片：适配器原样上报时链接藏在 jumpUrl 里
    for segment in (
        Message(type="json", data=json.dumps(_SHARED_CARD, ensure_ascii=False)),
        Message(type="card", data=_SHARED_CARD),
        Message(type="card", data=json.dumps(_SHARED_CARD)),
    ):
        event = Event(content=[segment])
        text = event_link_text(event)
        assert extract_urls(text) == (
            "https://www.xiaohongshu.com/discovery/item/68f1a2b3c4d5e6f708091a2b?xsec_token=ABxyz123",
        )


def test_shared_card_inside_forwarded_node_is_ignored() -> None:
    event = Event(content=[Message(type="node", data=[Message(type="card", data=_SHARED_CARD)])])

    assert event_link_text(event) == ""


def test_auto_detection_keeps_direct_text_and_ignores_forwarded_nodes() -> None:
    event = Event(
        content=[
            Message(type="text", data="请解析 https://xhslink.cn/o/direct"),
            Message(type="node", data=[{"type": "text", "data": "https://xhslink.cn/o/inside"}]),
        ]
    )

    assert event_link_text(event) == "请解析 https://xhslink.cn/o/direct"


# ── 候选遍历：多链接场景下的跳过与错误选择 ──


NOTE_OK = "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc?xsec_token=BBBBBBBB="
NOTE_LOGIN = "https://www.xiaohongshu.com/explore/6abc8896000000001801a1a5?xsec_token=ABPvx983="
NOTE_GONE = "https://www.xiaohongshu.com/explore/6abcd4290000000012036e74?xsec_token=CCCC="

_LOGIN_HTML = "<html><title>小红书</title><body>login</body></html>"


def _page(note_id: str) -> str:
    note = {
        "noteId": note_id,
        "title": "公开笔记",
        "user": {"nickname": "作者"},
        "imageList": [{"urlDefault": "https://img/a.jpg"}],
    }
    state = {"note": {"noteDetailMap": {"x": {"note": note}}}}
    return "<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>"


def _router(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.startswith("/login"):
        return httpx.Response(200, text=_LOGIN_HTML)
    # 404 落地页本身不含笔记，是「已下架」的真实落地形态
    if path.startswith("/404"):
        return httpx.Response(200, text="<html></html>")
    if "6abc8896" in path:
        return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login?redirectPath=x"})
    if "6abcd429" in path:
        return httpx.Response(
            302, headers={"location": "https://www.xiaohongshu.com/404?source=/404/sec_x&error_code=300031"}
        )
    return httpx.Response(200, text=_page("6abcb8ce000000001c01cbdc"))


def _run_first_valid(urls: tuple[str, ...]) -> NoteResult:
    async def run() -> NoteResult:
        transport = httpx.MockTransport(_router)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            return await parse_first_valid(client, urls)

    return asyncio.run(run())


def test_candidate_loop_continues_after_login_wall() -> None:
    """同一条消息里可能混有仅该条要求登录的笔记，其余候选仍应解析。"""

    result = _run_first_valid((NOTE_LOGIN, NOTE_OK))
    assert result.note_id == "6abcb8ce000000001c01cbdc"


def test_candidate_loop_continues_after_unavailable_note() -> None:
    result = _run_first_valid((NOTE_GONE, NOTE_OK))
    assert result.note_id == "6abcb8ce000000001c01cbdc"


def test_candidate_loop_continues_after_direct_link_without_cookie() -> None:
    """无 token 直链缺配置是候选自身形态问题，不该阻断同消息里的分享链接。"""

    direct = "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc"
    result = _run_first_valid((direct, NOTE_OK))
    assert result.note_id == "6abcb8ce000000001c01cbdc"


def test_pick_error_prefers_most_actionable_code_regardless_of_input_order() -> None:
    """同一组候选不应因书写顺序不同而给出不同结论。"""

    login = NoteParseError("login", CODE_LOGIN)
    gone = NoteParseError("gone", CODE_UNAVAILABLE)
    assert pick_error([gone, login]).code == CODE_LOGIN
    assert pick_error([login, gone]).code == CODE_LOGIN


def test_all_candidates_failing_reports_pick_error() -> None:
    async def run() -> NoteParseError:
        transport = httpx.MockTransport(_router)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            client.cookies.clear()
            try:
                await parse_first_valid(client, (NOTE_GONE, NOTE_LOGIN))
            except NoteParseError as error:
                return error
            raise AssertionError("应当抛出 NoteParseError")

    assert asyncio.run(run()).code == CODE_LOGIN


# ── 自动解析路径：非笔记链接必须静默 ──
# 直接测生产函数 extract_note_urls，不在此复刻筛选表达式——复刻副本等于自证。


def test_auto_detection_ignores_profile_and_search_pages() -> None:
    """主页/搜索页/首页都在 xiaohongshu.com 下，自动解析不该对它们报错。"""

    for text in (
        "这几张图不错 https://www.xiaohongshu.com/",
        "她的主页 https://www.xiaohongshu.com/user/profile/5f1a2b3c000000000101d2e3",
        "搜索 https://www.xiaohongshu.com/search_result?keyword=abc",
        "看看 https://www.xiaohongshu.com/explore",
    ):
        assert extract_note_urls(text) == (), text


def test_auto_detection_keeps_note_and_share_links() -> None:
    assert extract_note_urls("看这个 https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc") != ()
    assert extract_note_urls("分享 https://xhslink.cn/o/AbCdEf") != ()


def test_auto_detection_filters_non_note_link_but_keeps_note_link_in_same_message() -> None:
    text = "主页 https://www.xiaohongshu.com/user/profile/5f1a2b3c000000000101d2e3 和笔记 https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc"
    kept = extract_note_urls(text)
    assert len(kept) == 1
    assert "6abcb8ce" in kept[0]


def test_automatic_path_stays_silent_for_non_note_failures() -> None:
    """短链展开后落到主页/搜索页时不是笔记，自动路径不该在群里报错。"""

    for code in (CODE_NOTE_ID, CODE_UNKNOWN):
        assert not should_report_failure(NoteParseError("x", code), notify=False), code


def test_automatic_path_reports_actionable_failures() -> None:
    """需要登录、需要 Cookies、下架、风控都是真问题，自动路径也要给出反馈。"""

    for code in (CODE_LOGIN, CODE_COOKIE, CODE_UNAVAILABLE, CODE_CAPTCHA):
        assert should_report_failure(NoteParseError("x", code), notify=False), code


def test_error_priority_only_contains_skippable_codes() -> None:
    """不可跳过的码会直接上抛，永不进入候选汇总，不该出现在优先级表里。"""

    assert CODE_CAPTCHA not in ERROR_PRIORITY
    assert set(ERROR_PRIORITY) == SKIPPABLE_CODES


# ── 自动解析路径：非笔记链接必须静默 ──
# 直接测生产函数 extract_note_urls，不在此复刻筛选表达式——复刻副本等于自证。


def test_should_report_failure_silences_local_environment_errors_on_auto_path() -> None:
    """磁盘满、权限不足等本地问题与链接无关，自动路径不该为此刷屏。"""

    assert not should_report_failure(OSError(28, "No space left on device"), notify=False)
    assert not should_report_failure(OSError(13, "Permission denied"), notify=False)
    # 显式命令仍要给出反馈
    assert should_report_failure(OSError(28, "No space left on device"), notify=True)


def test_should_report_failure_reports_actionable_errors_on_auto_path() -> None:
    assert should_report_failure(NoteParseError("x", CODE_LOGIN), notify=False)
    assert not should_report_failure(NoteParseError("x", CODE_NOTE_ID), notify=False)


def test_should_report_failure_silences_transport_errors_on_auto_path() -> None:
    """代理/网络故障是持续性状态，自动路径逐条播报会刷屏。"""

    for error in (
        httpx.ProxyError("down"),
        httpx.ConnectTimeout("timeout"),
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("refused"),
        TimeoutError("timeout"),
    ):
        assert not should_report_failure(error, notify=False), type(error).__name__


def test_should_report_failure_keeps_http_status_errors_visible_on_auto_path() -> None:
    """服务端返回异常状态码与单条链接有关，应当让用户知道。"""

    request = httpx.Request("GET", "https://www.xiaohongshu.com/explore/abc")
    response = httpx.Response(500, request=request)
    assert should_report_failure(httpx.HTTPStatusError("500", request=request, response=response), notify=False)


def test_should_report_failure_always_reports_for_explicit_command() -> None:
    """显式 rn 命令必须给用户反馈，哪怕失败原因与链接无关。"""

    for error in (OSError(28, "No space left"), httpx.ProxyError("down"), NoteParseError("x", CODE_NOTE_ID)):
        assert should_report_failure(error, notify=True), type(error).__name__


# ── 发言策略：直接驱动生产入口，断言真实行为而非源码形状 ──
# xhs_parse 通过 tests/conftest.py 的 SV 桩导入，因此可以真实调用入口函数。


class RecordingBot:
    """记录 bot.send 的内容，用于断言机器人在群里实际说了什么。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: object, **_kwargs: object) -> None:
        self.sent.append(str(message))

    async def unsend(self, _message_ids: object) -> None:
        return None


class FakeEvent:
    user_id = "u1"
    group_id = "g1"
    raw_text = ""
    text = ""


_FALLBACK_URL = "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc"


def _handle_with_failure(error: BaseException, *, notify: bool) -> list[str]:
    """让解析一定失败，返回该次处理中机器人实际发出的消息。"""

    async def run() -> list[str]:
        import XHSAnalyse.xhs_parse as module

        original = module.parse_first_valid

        async def failing(*_args: object, **_kwargs: object) -> NoteResult:
            raise error

        module.parse_first_valid = failing
        bot = RecordingBot()
        try:
            await module._handle_urls(
                bot,
                FakeEvent(),
                (_FALLBACK_URL,),
                notify=notify,
            )
        finally:
            module.parse_first_valid = original
        # 不在测试里手工清理 _processing_users：释放由生产代码的 finally 负责，
        # 测试若代劳就会掩盖 _release_processing 失效造成的用户永久静默。
        assert FakeEvent.user_id not in module._processing_users, "生产代码未释放并发门"
        return bot.sent

    return asyncio.run(run())


def test_auto_path_stays_silent_when_parse_fails_on_non_note_link() -> None:
    assert _handle_with_failure(NoteParseError("x", CODE_NOTE_ID), notify=False) == []


def test_auto_path_speaks_when_note_requires_login() -> None:
    """登录墙是真实问题，自动路径也要给出可操作的提示。"""

    login_error = _page_error(
        httpx.Response(
            200,
            request=httpx.Request("GET", "https://www.xiaohongshu.com/login?redirectPath=x"),
            text="<html></html>",
        ),
        has_token=True,
        cookie="",
    )
    sent = _handle_with_failure(login_error, notify=False)
    assert len(sent) == 1, sent
    assert "登录" in sent[0]


def test_command_path_always_speaks_even_for_non_note_failure() -> None:
    sent = _handle_with_failure(NoteParseError("x", CODE_NOTE_ID), notify=True)
    assert len(sent) == 2, sent
    assert "正在解析" in sent[0]


def test_auto_path_silences_transport_failure_but_command_path_reports_it() -> None:
    assert _handle_with_failure(httpx.ProxyError("down"), notify=False) == []
    assert _handle_with_failure(httpx.ProxyError("down"), notify=True) != []


def test_auto_path_silences_local_io_failure() -> None:
    assert _handle_with_failure(OSError(28, "No space left on device"), notify=False) == []


def test_auto_path_never_leaks_credentials_from_exception_text() -> None:
    """代理地址里的凭据不能出现在群里。"""

    sent = _handle_with_failure(httpx.ProxyError("user:secret@127.0.0.1:7890"), notify=True)
    assert sent, "显式命令应当给出反馈"
    assert all("secret" not in message for message in sent), sent


def test_auto_detection_does_not_reach_handler_for_profile_page() -> None:
    """主页链接不该进入解析流程，否则会白跑一次请求。"""

    async def run() -> list[tuple[str, ...]]:
        import XHSAnalyse.xhs_parse as module

        seen: list[tuple[str, ...]] = []
        original_handle = module._handle_urls
        original_text = module.event_link_text

        async def spy(_bot: object, _ev: object, urls: tuple[str, ...], **_kwargs: object) -> bool:
            seen.append(urls)
            return False

        module._handle_urls = spy
        module.event_link_text = lambda _ev: "看主页 https://www.xiaohongshu.com/user/profile/5f1a2b3c000000000101d2e3"
        bot = RecordingBot()
        try:
            await module.xhs_detect_links(bot, FakeEvent())
        finally:
            module._handle_urls = original_handle
            module.event_link_text = original_text
        return seen

    assert asyncio.run(run()) == []


def _capture_handle_urls_calls(text: str, trigger: str) -> list[bool]:
    """驱动真实入口，记录传给 _handle_urls 的 notify 实参。"""

    async def run() -> list[bool]:
        import XHSAnalyse.xhs_parse as module

        calls: list[bool] = []
        original_handle = module._handle_urls
        original_text = module.event_link_text

        async def spy(_bot: object, _ev: object, _urls: tuple[str, ...], *, notify: bool) -> bool:
            calls.append(notify)
            return False

        module._handle_urls = spy
        module.event_link_text = lambda _ev: text
        # get_settings 在自动路径会被调用，这里让它走真实配置即可
        try:
            if trigger == "command":
                event = FakeEvent()
                event.text = text
                await module.xhs_parse(RecordingBot(), event)
            else:
                await module.xhs_detect_links(RecordingBot(), FakeEvent())
        finally:
            module._handle_urls = original_handle
            module.event_link_text = original_text
        return calls

    return asyncio.run(run())


def test_command_entry_asks_handler_to_notify() -> None:
    """显式 rn 命令必须让 handler 开启提示。"""

    url = "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc"
    assert _capture_handle_urls_calls(url, "command") == [True]


def test_auto_detection_entry_asks_handler_to_stay_silent() -> None:
    """自动嗅探必须让 handler 保持静默，否则非笔记链接会刷屏。"""

    url = "看看 https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc"
    assert _capture_handle_urls_calls(url, "detect") == [False]


def _run_two_concurrent_handles(notify: bool) -> tuple[list[str], list[str]]:
    """让两次 _handle_urls 真正并发，第二次必然撞上并发门。"""

    import XHSAnalyse.xhs_parse as module

    gate = asyncio.Event()

    async def slow_parse(*_args: object, **_kwargs: object) -> NoteResult:
        # 第一条占住并发门，直到第二条已经跑完
        await gate.wait()
        raise NoteParseError("x", CODE_LOGIN)

    async def run() -> tuple[list[str], list[str]]:
        original = module.parse_first_valid
        module.parse_first_valid = slow_parse
        first_bot, second_bot = RecordingBot(), RecordingBot()
        try:
            first = asyncio.create_task(module._handle_urls(first_bot, FakeEvent(), (_FALLBACK_URL,), notify=notify))
            # 让第一条先进入临界区
            for _ in range(10):
                await asyncio.sleep(0)
            assert FakeEvent.user_id in module._processing_users, "第一条应已占住并发门"
            await module._handle_urls(second_bot, FakeEvent(), (_FALLBACK_URL,), notify=notify)
            gate.set()
            await first
        finally:
            module.parse_first_valid = original
        assert FakeEvent.user_id not in module._processing_users, "生产代码未释放并发门"
        return first_bot.sent, second_bot.sent

    return asyncio.run(run())


def test_concurrency_rejection_is_silent_on_auto_path() -> None:
    """群里连发多条链接时，逐条提示并发拒绝只会刷屏。"""

    _first, second = _run_two_concurrent_handles(notify=False)
    assert second == []


def test_concurrency_rejection_is_reported_on_command_path() -> None:
    _first, second = _run_two_concurrent_handles(notify=True)
    assert any("正在处理" in message for message in second), second


def test_media_failure_is_silent_on_auto_path() -> None:
    """笔记页可达但媒体全下失败属于 CDN/磁盘问题，自动路径不该逐条播报。"""

    async def run(notify: bool) -> list[str]:
        import XHSAnalyse.xhs_parse as module

        original_parse = module.parse_first_valid
        original_prepare = module.prepare_media

        async def ok_result(*_args: object, **_kwargs: object) -> NoteResult:
            return NoteResult(
                note_id="6abcb8ce000000001c01cbdc",
                title="t",
                author="a",
                desc="",
                publish_time="",
                type="image",
                video_quality=None,
                media=(),
            )

        async def no_media(*_args: object, **_kwargs: object) -> tuple[()]:
            return ()

        module.parse_first_valid = ok_result
        module.prepare_media = no_media
        bot = RecordingBot()
        try:
            await module._handle_urls(bot, FakeEvent(), (_FALLBACK_URL,), notify=notify)
        finally:
            module.parse_first_valid = original_parse
            module.prepare_media = original_prepare
        assert FakeEvent.user_id not in module._processing_users, "生产代码未释放并发门"
        return bot.sent

    assert asyncio.run(run(notify=False)) == []
    assert any("媒体" in message for message in asyncio.run(run(notify=True)))


def test_failure_gate_polarity_matches_should_report_failure() -> None:
    """门控极性必须与 should_report_failure 一致：该静默的静默、该发言的发言。

    仅在门控表达式被整体反相、同时又改成无条件发送时才会暴露。
    """

    silent = _handle_with_failure(NoteParseError("x", CODE_NOTE_ID), notify=False)
    speaking = _handle_with_failure(NoteParseError("x", CODE_LOGIN), notify=False)
    assert silent == [], silent
    assert speaking != [], "需要登录的失败必须发声"


def test_persist_refreshed_session_skips_cookie_less_mode(monkeypatch) -> None:
    """无 Cookies 模式不把游客会话写进用户配置。

    不设这道守卫时，``apply_refreshed_cookie("")`` 会把缓存里的游客会话拼成
    ``web_session=...`` 并落盘，用户显式留空的 Cookie 配置就被静默改成
    「带 Cookies 模式」。
    """

    import XHSAnalyse.xhs_parse as module

    written: list[str] = []
    monkeypatch.setattr(module, "_persist_cookie", written.append)
    monkeypatch.setattr(module, "apply_refreshed_cookie", lambda cookie: "web_session=guest")
    monkeypatch.setattr(module, "read_activated_session", lambda: "newsess")

    assert module._persist_refreshed_session("", activated_before="oldsess") == ""
    assert written == []

    # 配置里本来就有 Cookies 且本次确实激活过：写回，并把新 Cookie 交给
    # 本次解析继续使用。
    assert module._persist_refreshed_session("a1=x", activated_before="oldsess") == "web_session=guest"
    assert written == ["web_session=guest"]


def test_persist_refreshed_session_skips_when_not_activated_this_run(monkeypatch) -> None:
    """本次解析没有产生新会话：不拿缓存旧值覆盖用户配置。

    用户手工重新导出一份 Cookies 后，本地缓存里还留着上一次激活的旧值；
    若无条件按「缓存 ≠ 配置」写回，刚导入的新值会被旧值顶掉。判据因此绑定
    到「解析前快照是否变化」，而不是「缓存与配置是否不同」。
    """

    import XHSAnalyse.xhs_parse as module

    written: list[str] = []
    monkeypatch.setattr(module, "_persist_cookie", written.append)
    monkeypatch.setattr(module, "apply_refreshed_cookie", lambda cookie: "web_session=stale")

    monkeypatch.setattr(module, "read_activated_session", lambda: "same")
    assert module._persist_refreshed_session("a1=x", activated_before="same") == "a1=x"
    assert written == []

    # 缓存为空（本次没有任何激活）同样不写回。
    monkeypatch.setattr(module, "read_activated_session", lambda: "")
    assert module._persist_refreshed_session("a1=x", activated_before="") == "a1=x"
    assert written == []


def test_persist_refreshed_session_writes_on_first_activation(monkeypatch) -> None:
    """进程内首次激活（解析前缓存为空）同样要写回配置。"""

    import XHSAnalyse.xhs_parse as module

    written: list[str] = []
    monkeypatch.setattr(module, "_persist_cookie", written.append)
    monkeypatch.setattr(module, "apply_refreshed_cookie", lambda cookie: "web_session=fresh")
    monkeypatch.setattr(module, "read_activated_session", lambda: "fresh")

    assert module._persist_refreshed_session("a1=x", activated_before="") == "web_session=fresh"
    assert written == ["web_session=fresh"]


def test_persist_refreshed_session_keeps_cookie_when_unchanged(monkeypatch) -> None:
    """没有新会话时不写回配置，也不改动当前解析使用的 Cookie。"""

    import XHSAnalyse.xhs_parse as module

    written: list[str] = []
    monkeypatch.setattr(module, "_persist_cookie", written.append)
    monkeypatch.setattr(module, "apply_refreshed_cookie", lambda cookie: None)
    monkeypatch.setattr(module, "read_activated_session", lambda: "newsess")

    assert module._persist_refreshed_session("a1=x", activated_before="oldsess") == "a1=x"
    assert written == []

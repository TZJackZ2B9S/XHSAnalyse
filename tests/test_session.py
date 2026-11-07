"""web_session 会话管理测试。

覆盖：提取/替换会话、首页特征判定、节流、激活流程（Playwright 全 mock，
绝不触网），以及解析入口的自动续期接线。
"""

import json
import asyncio
from dataclasses import replace

import httpx
import pytest

from XHSAnalyse.utils.parse import note as note_mod, session as session_mod
from XHSAnalyse.utils.parse.session import (
    check_web_session,
    ensure_web_session,
    extract_web_session,
    replace_web_session,
    activate_web_session,
)

VALID_STATE = (
    '<script>window.__INITIAL_STATE__={"user":{"loggedIn":false,"userInfo":'
    '{"user_id":"6accdbe20000000005039c04","guest":true}}}</script>'
)
DEAD_STATE = (
    '<script>window.__INITIAL_STATE__={"user":{"loggedIn":false,'
    '"userInfo":{"userId":undefined,"redId":undefined}}}</script>'
)
NEW_SESSION = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
OLD_SESSION = "feedfacefeedfacefeedfacefeedfacefeedface"
COOKIE_WITH_SESSION = f"a1=abc; web_session={OLD_SESSION}; gid=xyz"


@pytest.fixture(autouse=True)
def _isolate_session_store(tmp_path, monkeypatch):
    """本文件的用例只额外需要「缓存文件落在临时目录」。

    节流状态与判定状态的隔离由 ``tests/conftest.py`` 的 autouse fixture
    统一提供；这里不再重复设置 ``_last_check_valid``，避免把三态判定的默认值
    固化成 ``True``（那正是本模块要防的「无证据却假定有效」）。
    """

    monkeypatch.setattr(session_mod, "_SESSION_STORE", tmp_path / "web_session.json")
    yield


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _read_cached_session_from(store) -> str:
    original = session_mod._SESSION_STORE
    session_mod._SESSION_STORE = store
    try:
        return session_mod._read_cached_session()
    finally:
        session_mod._SESSION_STORE = original


def test_read_cached_session_ignores_non_object_json(tmp_path) -> None:
    """缓存文件是合法 JSON 但不是对象时不能抛 AttributeError 冲出触发器。"""

    store = tmp_path / "web_session.json"
    for payload in ("null", "[]", '"abc"', "123"):
        store.write_text(payload, encoding="utf-8")
        assert _read_cached_session_from(store) == "", payload


def test_extract_web_session() -> None:
    assert extract_web_session(COOKIE_WITH_SESSION) == "feedfacefeedfacefeedfacefeedfacefeedface"
    assert extract_web_session("a1=abc") == ""
    assert extract_web_session("") == ""
    # 不是第一个键、前面无空格的形态同样可提取。
    assert extract_web_session("a1=1;web_session=0x; gid=2") == "0x"


def test_replace_web_session() -> None:
    replaced = replace_web_session(COOKIE_WITH_SESSION, NEW_SESSION)
    assert extract_web_session(replaced) == NEW_SESSION
    # 其余键原样保留。
    assert "a1=abc" in replaced and "gid=xyz" in replaced
    # 缺失时追加，空 Cookie 时从零构造。
    assert replace_web_session("a1=abc", NEW_SESSION) == f"a1=abc; web_session={NEW_SESSION}"
    assert replace_web_session("", NEW_SESSION) == f"web_session={NEW_SESSION}"
    # 空会话值不改写原串。
    assert replace_web_session(COOKIE_WITH_SESSION, "") == COOKIE_WITH_SESSION


def test_check_web_session_states(monkeypatch) -> None:
    """有效/失效页面分别判定，且同一窗口内复用结果。"""

    requests: list[str] = []

    async def fake_get(url, headers=None, **kwargs):
        requests.append(url)
        state = VALID_STATE if session_mod._last_check_valid else DEAD_STATE
        return httpx.Response(200, text=state, request=httpx.Request("GET", url))

    async def run() -> tuple[bool, bool, int, bool]:
        client = _client()
        monkeypatch.setattr(client, "get", fake_get)
        session_mod._last_check_valid = False
        invalid = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        count = len(requests)
        cached = await check_web_session(client, COOKIE_WITH_SESSION)
        session_mod._last_check_valid = True
        valid = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        await client.aclose()
        return invalid, cached, count, valid

    invalid, cached, count, valid = asyncio.run(run())
    assert invalid is False
    assert cached is False
    # 两次 force 检测各发一次请求，中间复用窗口内的结果不发请求。
    assert count == 1
    assert len(requests) == 2
    assert valid is True


def test_check_web_session_no_cookie_short_circuit(monkeypatch) -> None:
    """未配置 web_session 时直接放行，不发任何请求。"""

    async def run() -> tuple[bool, bool]:
        client = _client()

        async def fail_get(url, **kwargs):
            raise AssertionError("未配置 web_session 不应发起检测请求")

        monkeypatch.setattr(client, "get", fail_get)
        no_session = await check_web_session(client, "a1=abc")
        empty = await check_web_session(client, "")
        await client.aclose()
        return no_session, empty

    no_session, empty = asyncio.run(run())
    assert no_session is True
    assert empty is True


def test_check_web_session_http_error_keeps_previous(monkeypatch) -> None:
    """检测请求失败时沿用上一次结论，不把网络抖动误判成会话失效。"""

    async def run() -> bool:
        client = _client()

        async def fail_get(url, **kwargs):
            raise httpx.ConnectError("网络不可达")

        monkeypatch.setattr(client, "get", fail_get)
        session_mod._last_check_valid = True
        result = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        await client.aclose()
        return result

    assert asyncio.run(run()) is True


def test_check_web_session_login_wall_skips_verdict(monkeypatch) -> None:
    """IP 被风控踢到登录页时不算会话失效：沿用旧结论、不更新判定状态。"""

    activated: list[int] = []

    async def run() -> tuple[bool, bool, str]:
        client = _client()
        # 跟随重定向后落在 /login 的响应：url 指向 /login，正文是登录页壳
        login_url = httpx.URL("https://www.xiaohongshu.com/login?redirectPath=xx")

        class _LoginWallResponse:
            status_code = 200
            url = login_url
            text = "<html>登录</html>"

        async def fake_get(url, **kwargs):
            return _LoginWallResponse()

        async def fail_activate(*args, **kwargs):
            activated.append(1)
            return OLD_SESSION

        monkeypatch.setattr(client, "get", fake_get)
        monkeypatch.setattr(session_mod, "activate_web_session", fail_activate)
        session_mod._last_check_valid = True
        kept = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        check_at_updated = session_mod._last_check_at > 0.0
        # ensure_web_session 在「沿用旧结论 True」时不应触发激活
        cookie, refreshed = await ensure_web_session(client, COOKIE_WITH_SESSION)
        await client.aclose()
        activation = "activated" if activated else "skipped"
        if cookie != COOKIE_WITH_SESSION or refreshed:
            activation += "-bad"
        return kept, check_at_updated, activation

    kept, check_at_updated, activation = asyncio.run(run())
    assert kept is True  # 沿用上一次「有效」的结论
    assert check_at_updated is False  # 不记录判定时间：下次风控解除后重新检测
    assert activation == "skipped"


def test_check_web_session_redirect_status_skips_verdict(monkeypatch) -> None:
    """未跟随重定向拿到 302 时同样跳过判定，不误记会话失效。"""

    activated: list[int] = []

    async def run() -> tuple[bool, bool, str]:
        client = _client()
        request = httpx.Request("GET", session_mod.HOME_URL)
        page = httpx.Response(302, headers={"location": "/login"}, request=request)

        async def fake_get(url, **kwargs):
            return page

        async def fail_activate(*args, **kwargs):
            activated.append(1)
            return OLD_SESSION

        monkeypatch.setattr(client, "get", fake_get)
        monkeypatch.setattr(session_mod, "activate_web_session", fail_activate)
        session_mod._last_check_valid = True
        kept = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        check_at_updated = session_mod._last_check_at > 0.0
        cookie, refreshed = await ensure_web_session(client, COOKIE_WITH_SESSION)
        await client.aclose()
        activation = "activated" if activated else "skipped"
        if cookie != COOKIE_WITH_SESSION or refreshed:
            activation += "-bad"
        return kept, check_at_updated, activation

    kept, check_at_updated, activation = asyncio.run(run())
    assert kept is True
    assert check_at_updated is False
    assert activation == "skipped"


def test_activate_persists_and_resets_throttle(tmp_path, monkeypatch) -> None:
    """激活成功后写缓存并重置检测节流；失败同样计入节流窗口。"""

    async def fake_activation(proxy, *, output_logs=False):
        return NEW_SESSION

    async def failing_activation(proxy, *, output_logs=False):
        return ""

    async def run() -> tuple[str, dict, bool]:
        monkeypatch.setattr(session_mod, "_activate_with_browser", fake_activation)
        session = await activate_web_session()
        payload = json.loads(session_mod._SESSION_STORE.read_text(encoding="utf-8"))
        # 激活成功本身就是「会话有效」的一次带证据判定：结论与时间戳同时
        # 刷新，让 30 分钟检测缓存生效，下一次解析不会立刻再打一次首页。
        verdict_recorded = session_mod._last_check_valid is True and session_mod._last_check_at > 0.0
        monkeypatch.setattr(session_mod, "_activate_with_browser", failing_activation)
        monkeypatch.setattr(session_mod, "_last_activate_at", 0.0)
        failed = await activate_web_session()
        return session, payload, verdict_recorded and failed == ""

    session, payload, verdict_recorded = asyncio.run(run())
    assert session == NEW_SESSION
    assert payload["web_session"] == NEW_SESSION
    assert verdict_recorded is True


def test_activate_failure_is_throttled(monkeypatch) -> None:
    """激活失败也更新节流时间戳：窗口内的下一次调用不再拉起浏览器。"""

    attempts: list[int] = []

    async def failing_activation(proxy, *, output_logs=False):
        attempts.append(1)
        return ""

    async def run() -> tuple[str, str]:
        monkeypatch.setattr(session_mod, "_activate_with_browser", failing_activation)
        first = await activate_web_session()
        throttled_at = session_mod._last_activate_at
        second = await activate_web_session()
        return (first, second) if throttled_at > 0.0 else ("no-throttle", second)

    first, second = asyncio.run(run())
    assert first == ""
    assert second == ""  # 窗口内直接返回，不再次激活
    assert len(attempts) == 1
    assert session_mod._last_activate_at > 0.0


def test_activate_throttle_does_not_revive_stale_cache(monkeypatch) -> None:
    """节流窗口内不得把过期缓存当激活成功返回（防过期会话复活漏洞）。

    场景：缓存里有旧会话 X；一次激活失败；窗口内的后续调用必须返回空串
    而不是缓存 X——否则上层会把过期会话写回用户配置并重置复用窗口。
    """

    attempts: list[int] = []

    async def failing_activation(proxy, *, output_logs=False):
        attempts.append(1)
        return ""

    async def run() -> tuple[str, str, str]:
        monkeypatch.setattr(session_mod, "_activate_with_browser", failing_activation)
        session_mod._write_cached_session(OLD_SESSION)  # 预置过期缓存
        first = await activate_web_session()  # 真实尝试，失败
        second = await activate_web_session()  # 窗口内：不得返回 OLD_SESSION
        third = await activate_web_session()  # 仍在窗口内
        return first, second, f"{third}|attempts={len(attempts)}"

    first, second, third = asyncio.run(run())
    third_value, _, third_attempts = third.partition("|")
    assert first == ""
    assert second == ""  # 关键断言：不是 OLD_SESSION
    # 第三次仍在窗口内：返回空串且未再拉浏览器
    assert third_value == ""
    assert third_attempts == "attempts=1"
    assert len(attempts) == 1


def test_ensure_web_session_refresh_path(monkeypatch) -> None:
    """失效 → 激活 → 返回合并后的新 Cookie；有效 → 原样返回。"""

    async def dead(*args, **kwargs):
        return False

    async def alive(*args, **kwargs):
        return True

    async def mint_browser(*args, **kwargs):
        return NEW_SESSION

    async def run() -> tuple[tuple[str, bool], tuple[str, bool]]:
        client = _client()
        monkeypatch.setattr(session_mod, "check_web_session", dead)
        monkeypatch.setattr(session_mod, "_activate_with_browser", mint_browser)
        refreshed_pair = await ensure_web_session(client, COOKIE_WITH_SESSION)
        monkeypatch.setattr(session_mod, "check_web_session", alive)
        kept_pair = await ensure_web_session(client, COOKIE_WITH_SESSION)
        await client.aclose()
        return refreshed_pair, kept_pair

    (cookie, refreshed), (kept_cookie, kept_flag) = asyncio.run(run())
    assert refreshed is True
    assert extract_web_session(cookie) == NEW_SESSION
    assert kept_flag is False
    assert kept_cookie == COOKIE_WITH_SESSION


def test_ensure_web_session_no_cookie_activates(monkeypatch) -> None:
    """无 Cookie 模式：主动激活游客会话并返回，开启自动续期时也能拿 4K。"""

    mint_calls: list[int] = []

    async def mint_browser(proxy, *, output_logs=False):
        # mock 浏览器层：缓存写入走真实 activate_web_session 代码路径。
        mint_calls.append(1)
        return NEW_SESSION

    async def fail_check(*args, **kwargs):
        raise AssertionError("无 Cookie 模式不应做网络检测")

    async def run() -> tuple[tuple[str, bool], tuple[str, bool], int]:
        client = _client()
        monkeypatch.setattr(session_mod, "_activate_with_browser", mint_browser)
        monkeypatch.setattr(session_mod, "check_web_session", fail_check)
        first = await ensure_web_session(client, "")
        first_at = session_mod._last_anonymous_activate_at
        second = await ensure_web_session(client, "")
        await client.aclose()
        return first, second, (len(mint_calls) if first_at > 0.0 else -1)

    (cookie1, refreshed1), (cookie2, refreshed2), calls = asyncio.run(run())
    assert refreshed1 is True
    assert extract_web_session(cookie1) == NEW_SESSION
    # 复用窗口内第二次直接用缓存会话，不再激活。
    assert refreshed2 is False
    assert extract_web_session(cookie2) == NEW_SESSION
    assert calls == 1


def test_ensure_web_session_no_cookie_activation_failure(monkeypatch) -> None:
    """无 Cookie 模式激活失败：返回空 Cookie，解析按匿名降级继续。"""

    async def fail_mint(*args, **kwargs):
        return ""

    async def run() -> tuple[str, bool]:
        client = _client()
        monkeypatch.setattr(session_mod, "_activate_with_browser", fail_mint)
        result = await ensure_web_session(client, "")
        await client.aclose()
        return result

    cookie, refreshed = asyncio.run(run())
    assert cookie == ""
    assert refreshed is False


def test_random_activation_fingerprint_varies() -> None:
    """随机指纹生成器：字段齐全且多次调用存在组合差异。"""

    seen: set[tuple[str, str, str, str]] = set()
    for _ in range(40):
        fp = session_mod._random_activation_fingerprint()
        keys = set(fp)
        assert keys == {"user_agent", "viewport", "locale", "timezone_id"}
        viewport = fp["viewport"]
        assert isinstance(viewport, dict) and "width" in viewport and "height" in viewport
        ua = fp["user_agent"]
        locale = fp["locale"]
        tz = fp["timezone_id"]
        assert isinstance(ua, str) and ua.startswith("Mozilla/5.0")
        assert isinstance(locale, str) and isinstance(tz, str)
        seen.add((ua, f"{viewport}", locale, tz))
    # 40 次采样应命中多种组合（池大小 7*6*4*4=672，全同概率趋近零）。
    assert len(seen) > 1


def test_resolve_chromium_prefers_existing(monkeypatch, tmp_path) -> None:
    """Chromium 路径探测：命中第一个存在的候选，全缺失时返回空串。"""

    fake = tmp_path / "chromium"
    fake.write_bytes(b"")
    monkeypatch.setattr(
        session_mod,
        "_CHROMIUM_CANDIDATES",
        (str(tmp_path / "missing"), str(fake), "/usr/bin/chromium"),
    )
    assert session_mod._resolve_chromium() == str(fake)
    monkeypatch.setattr(
        session_mod,
        "_CHROMIUM_CANDIDATES",
        (str(tmp_path / "missing1"), str(tmp_path / "missing2")),
    )
    assert session_mod._resolve_chromium() == ""


def test_parse_note_refresh_disabled_skips_session(monkeypatch) -> None:
    """refresh_session=False 时不进入会话模块，直接走原有解析路径。"""

    async def fail_ensure(*args, **kwargs):
        raise AssertionError("refresh_session=False 不应触发会话检测")

    async def run() -> str:
        client = _client()
        monkeypatch.setattr(session_mod, "ensure_web_session", fail_ensure)
        # 返回空正文页面（无笔记数据），走完 fallback 后应报「无法提取」；
        # 会话检测被断言为绝不可触发（parse_note 不再持有 refresh_session）。
        page = httpx.Response(
            200,
            text="<html></html>",
            request=httpx.Request("GET", "https://www.xiaohongshu.com/explore/" + "a" * 24),
        )

        async def fake_get(url, headers=None, **kwargs):
            return page

        monkeypatch.setattr(client, "get", fake_get)
        try:
            await note_mod.parse_note(
                client,
                "https://www.xiaohongshu.com/explore/" + "a" * 24,
                COOKIE_WITH_SESSION,
            )
        except note_mod.NoteParseError as error:
            code = error.code
        finally:
            await client.aclose()
        return code

    assert asyncio.run(run()) == note_mod.CODE_UNKNOWN


def test_parse_first_valid_refreshes_once_for_all_candidates(monkeypatch) -> None:
    """会话刷新只发生在候选循环前：多候选不会重复检测，enrich 用新会话。"""

    ensure_calls: list[str] = []
    parse_cookies: list[str] = []
    enrich_cookies: list[str] = []

    async def fake_ensure(client, cookie, **kwargs):
        ensure_calls.append(cookie)
        return cookie.replace(OLD_SESSION, NEW_SESSION), True

    async def fake_parse(client, url, cookie, **kwargs):
        parse_cookies.append(cookie)
        return note_mod.NoteResult(
            note_id="a" * 24,
            title="t",
            author="a",
            desc="",
            publish_time="",
            type="image",
            video_quality=None,
            media=(),
            share_url=url,
        )

    async def fake_enrich(client, result, *, cookie=""):
        enrich_cookies.append(cookie)
        return result

    urls = (
        "https://www.xiaohongshu.com/explore/" + "a" * 24,
        "https://www.xiaohongshu.com/explore/" + "b" * 24,
    )

    async def run() -> None:
        client = _client()
        monkeypatch.setattr(note_mod, "ensure_web_session", fake_ensure)
        monkeypatch.setattr(note_mod, "parse_note", fake_parse)
        monkeypatch.setattr(note_mod, "enrich_author_profile", fake_enrich)
        for url in urls:
            await note_mod.parse_first_valid(
                client,
                (url,),
                COOKIE_WITH_SESSION,
                enrich_profile=True,
            )
        await client.aclose()

    asyncio.run(run())
    # 每条消息（每次 parse_first_valid 调用）只检测一次；两条独立消息共两次。
    assert ensure_calls == [COOKIE_WITH_SESSION, COOKIE_WITH_SESSION]
    assert len(parse_cookies) == 2
    assert all(extract_web_session(c) == NEW_SESSION for c in parse_cookies)
    assert enrich_cookies and extract_web_session(enrich_cookies[0]) == NEW_SESSION


def test_apply_refreshed_cookie_wiring(monkeypatch) -> None:
    """解析层把激活到的会话合并进配置 Cookie；无新会话时返回 None。"""

    monkeypatch.setattr(session_mod, "_read_cached_session", lambda: NEW_SESSION)
    merged = note_mod.apply_refreshed_cookie(COOKIE_WITH_SESSION)
    assert merged is not None and extract_web_session(merged) == NEW_SESSION
    # 会话未变化时不重复写回。
    same = note_mod.apply_refreshed_cookie(f"a1=abc; web_session={NEW_SESSION}")
    assert same is None
    monkeypatch.setattr(session_mod, "_read_cached_session", lambda: "")
    assert note_mod.apply_refreshed_cookie(COOKIE_WITH_SESSION) is None


def _login_wall_response() -> httpx.Response:
    """构造一个「跟随重定向后落在登录页」的响应。"""

    url = "https://www.xiaohongshu.com/login?redirectPath=xx"
    return httpx.Response(200, text="<html>登录</html>", request=httpx.Request("GET", url))


def test_check_web_session_login_wall_without_evidence_is_unusable(monkeypatch) -> None:
    """登录墙下若从未得出有证据的结论，必须判定为不可用并触发一次激活。

    首页对全部未登录访客强制跳登录页，判据不可用。此时若假定会话有效，
    失效会话将永久跳过续期，档位永远停在 720p。
    """

    activated: list[int] = []

    async def run() -> tuple[bool, str, bool]:
        client = _client()

        async def fake_get(url, **kwargs):
            return _login_wall_response()

        async def fake_activate(*args, **kwargs):
            activated.append(1)
            return NEW_SESSION

        monkeypatch.setattr(client, "get", fake_get)
        monkeypatch.setattr(session_mod, "activate_web_session", fake_activate)
        session_mod._last_check_valid = None
        verdict = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        cookie, refreshed = await ensure_web_session(client, COOKIE_WITH_SESSION)
        await client.aclose()
        return verdict, cookie, refreshed

    verdict, cookie, refreshed = asyncio.run(run())
    assert verdict is False
    assert activated == [1]
    assert refreshed is True
    assert extract_web_session(cookie) == NEW_SESSION


def test_record_session_verdict_drives_later_checks(monkeypatch) -> None:
    """解析结果回灌后，登录墙下沿用的就是这条有证据的结论。"""

    async def run() -> tuple[bool, bool]:
        client = _client()

        async def fake_get(url, **kwargs):
            return _login_wall_response()

        monkeypatch.setattr(client, "get", fake_get)
        session_mod.record_session_verdict(True)
        valid = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        session_mod.record_session_verdict(False)
        invalid = await check_web_session(client, COOKIE_WITH_SESSION, force=True)
        await client.aclose()
        return valid, invalid

    valid, invalid = asyncio.run(run())
    assert valid is True
    assert invalid is False


def _fake_media(note_id: str, share_url: str = "") -> note_mod.NoteResult:
    return note_mod.NoteResult(
        note_id=note_id,
        title="t",
        author="a",
        desc="",
        publish_time="",
        type="video",
        video_quality="2160p",
        media=(),
        share_url=share_url or f"https://www.xiaohongshu.com/explore/{note_id}",
    )


def test_parse_note_recovers_session_and_retries(monkeypatch) -> None:
    """带 Cookies 被拒时续期一次会话并重试，成功即用新会话继续解析。"""

    seen: list[str] = []

    async def run() -> tuple[bool, list[str]]:
        client = _client()
        note_id = "a" * 24
        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        ok = httpx.Response(200, text="<html></html>", request=httpx.Request("GET", url))

        async def fake_get(url_, headers=None, **kwargs):
            cookie = (headers or {}).get("Cookie", "")
            seen.append(cookie)
            return _login_wall_response() if extract_web_session(cookie) == OLD_SESSION else ok

        async def fake_activate(*args, **kwargs):
            return NEW_SESSION

        monkeypatch.setattr(client, "get", fake_get)
        monkeypatch.setattr(note_mod, "activate_web_session", fake_activate)
        monkeypatch.setattr(note_mod, "extract_note_from_html", lambda text: {"noteId": note_id})
        monkeypatch.setattr(note_mod, "collect_media", lambda *args, **kwargs: _fake_media(note_id, str(url)))
        result = await note_mod.parse_note(client, url, COOKIE_WITH_SESSION, session_recovery=True)
        await client.aclose()
        return result.cookie_expired, seen

    # 续期后重试成功：本次解析确实用上了被承认的会话，不算 Cookies 失效。
    expired, seen = asyncio.run(run())
    assert expired is False
    assert seen == [COOKIE_WITH_SESSION, f"a1=abc; web_session={NEW_SESSION}; gid=xyz"]
    # 只替换 web_session：a1 / gid 参与设备与风控画像，丢失会让同一份
    # Cookies 在中途变成另一个设备。
    assert "a1=abc" in seen[1] and "gid=xyz" in seen[1]


def test_parse_note_recovery_skips_retry_when_session_unchanged(monkeypatch) -> None:
    """激活返回同一个会话时不发无意义的重试请求。"""

    seen: list[str] = []

    async def run() -> int:
        client = _client()
        note_id = "a" * 24
        url = f"https://www.xiaohongshu.com/explore/{note_id}"

        async def fake_get(url_, headers=None, **kwargs):
            seen.append(extract_web_session((headers or {}).get("Cookie", "")))
            return _login_wall_response()

        async def same_session(*args, **kwargs):
            return OLD_SESSION

        monkeypatch.setattr(client, "get", fake_get)
        monkeypatch.setattr(note_mod, "activate_web_session", same_session)
        try:
            await note_mod.parse_note(client, url, COOKIE_WITH_SESSION, session_recovery=True)
        except note_mod.NoteParseError:
            pass
        count = len(seen)
        await client.aclose()
        return count

    # 首次带 Cookies 请求 + 匿名回落各一次；续期未换到会话，不发第三次。
    assert asyncio.run(run()) == 2


def test_parse_note_recovery_failure_falls_back_to_anonymous(monkeypatch) -> None:
    """续期拿不到会话时保持既有匿名回落行为，不改变失败路径。"""

    seen: list[str] = []

    async def run() -> tuple[bool, list[str]]:
        client = _client()
        note_id = "a" * 24
        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        ok = httpx.Response(200, text="<html></html>", request=httpx.Request("GET", url))

        async def fake_get(url_, headers=None, **kwargs):
            cookie = (headers or {}).get("Cookie", "")
            seen.append(extract_web_session(cookie))
            return ok if not cookie else _login_wall_response()

        async def fake_activate(*args, **kwargs):
            return ""

        monkeypatch.setattr(client, "get", fake_get)
        monkeypatch.setattr(note_mod, "activate_web_session", fake_activate)
        monkeypatch.setattr(note_mod, "extract_note_from_html", lambda text: {"noteId": note_id})
        monkeypatch.setattr(note_mod, "collect_media", lambda *args, **kwargs: _fake_media(note_id, str(url)))
        result = await note_mod.parse_note(client, url, COOKIE_WITH_SESSION, session_recovery=True)
        await client.aclose()
        return result.cookie_expired, seen

    expired, seen = asyncio.run(run())
    assert expired is True
    # 激活没换到会话时不发第二次带 Cookies 请求，直接匿名重试。
    assert seen == [OLD_SESSION, ""]


def test_parse_first_valid_records_session_verdict(monkeypatch) -> None:
    """解析结果回灌给会话管理层：成功记有效、Cookies 失效记不可用。"""

    verdicts: list[bool] = []

    async def fake_ensure(client, cookie, **kwargs):
        return cookie, False

    async def run(expired: bool) -> None:
        client = _client()
        session_mod._last_check_valid = None

        async def fake_parse(client_, url, cookie, **kwargs):
            return replace(_fake_media("a" * 24), cookie_expired=expired)

        monkeypatch.setattr(note_mod, "ensure_web_session", fake_ensure)
        monkeypatch.setattr(note_mod, "parse_note", fake_parse)
        url = "https://www.xiaohongshu.com/explore/" + "a" * 24
        await note_mod.parse_first_valid(client, (url,), COOKIE_WITH_SESSION)
        verdicts.append(bool(session_mod._last_check_valid))
        await client.aclose()

    asyncio.run(run(False))
    session_mod._last_check_valid = None
    asyncio.run(run(True))
    assert verdicts == [True, False]


def test_parse_first_valid_ensures_session_without_cookie(monkeypatch) -> None:
    """无 Cookies 模式同样进入会话保障，并把激活到的游客会话交给解析。"""

    ensured: list[str] = []
    used: list[str] = []

    async def fake_ensure(client, cookie, **kwargs):
        ensured.append(cookie)
        return "web_session=guest", True

    async def fake_parse(client, url, cookie, **kwargs):
        used.append(cookie)
        return _fake_media(url.rsplit("/", 1)[-1])

    async def run() -> None:
        client = _client()
        monkeypatch.setattr(note_mod, "ensure_web_session", fake_ensure)
        monkeypatch.setattr(note_mod, "parse_note", fake_parse)
        await note_mod.parse_first_valid(client, ("https://www.xiaohongshu.com/explore/" + "a" * 24,), "")
        await client.aclose()

    asyncio.run(run())
    # 空 Cookie 也必须进会话保障：4K 档位依赖 web_session，游客会话不需要登录。
    assert ensured == [""]
    assert used == ["web_session=guest"]


def test_parse_first_valid_keeps_verdict_on_login_wall_failure(monkeypatch) -> None:
    """解析失败不当作会话失效的证据：登录墙也可能来自笔记本身。

    只有「带 Cookies 被拒、匿名才取到」才是会话失效的可靠证据（由成功路径
    的 ``cookie_expired`` 回灌）。若把 ``CODE_LOGIN`` 失败一律记成失效，
    一条仅要求登录的笔记就会把有效会话标脏，白拉一次浏览器。
    """

    async def fake_ensure(client, cookie, **kwargs):
        return cookie, False

    async def fake_parse(client, url, cookie, **kwargs):
        raise note_mod.NoteParseError("需要登录", note_mod.CODE_LOGIN)

    async def run() -> None:
        client = _client()
        session_mod._last_check_valid = True
        monkeypatch.setattr(note_mod, "ensure_web_session", fake_ensure)
        monkeypatch.setattr(note_mod, "parse_note", fake_parse)
        try:
            await note_mod.parse_first_valid(
                client,
                ("https://www.xiaohongshu.com/explore/" + "a" * 24,),
                COOKIE_WITH_SESSION,
            )
        except note_mod.NoteParseError:
            pass
        await client.aclose()

    asyncio.run(run())
    assert session_mod._last_check_valid is True


def test_proxy_settings_shape() -> None:
    """httpx 风格的代理串必须转成 playwright 接受的 ``ProxySettings``。

    playwright 的 ``new_context`` 不接受字符串代理（会抛
    ``proxy: expected object, got string``），也要求凭据单独传；一旦形状不对，
    配置了代理的用户整条会话续期链会被吞成一条 warning 后静默失效。
    """

    assert session_mod._proxy_settings("") is None
    assert session_mod._proxy_settings("http://127.0.0.1:7890") == {"server": "http://127.0.0.1:7890"}
    assert session_mod._proxy_settings("socks5://127.0.0.1:1080") == {"server": "socks5://127.0.0.1:1080"}
    # 无 scheme 与带 path 的写法原样透传，保持 httpx 侧不变的语义。
    assert session_mod._proxy_settings("127.0.0.1:7890") == {"server": "127.0.0.1:7890"}
    assert session_mod._proxy_settings("http://127.0.0.1:7890/p") == {"server": "http://127.0.0.1:7890/p"}
    assert session_mod._proxy_settings("http://alice:s3cr3t@127.0.0.1:7890") == {
        "server": "http://127.0.0.1:7890",
        "username": "alice",
        "password": "s3cr3t",
    }
    # 空用户名同样算带凭据：否则口令会留在 server 里被 playwright 当主机名解析。
    assert session_mod._proxy_settings("http://:s3cr3t@127.0.0.1:7890") == {
        "server": "http://127.0.0.1:7890",
        "username": "",
        "password": "s3cr3t",
    }


def test_replace_web_session_handles_empty_value(monkeypatch) -> None:
    """配置里写成 ``web_session=``（空值）时替换而不是追加，避免重复键。"""

    replaced = replace_web_session("a1=abc; web_session=; gid=x", NEW_SESSION)
    assert replaced == f"a1=abc; web_session={NEW_SESSION}; gid=x"
    # 空值本身不算「有会话」，会话检测与续期都按缺失处理。
    assert extract_web_session("a1=abc; web_session=; gid=x") == ""


def test_ensure_web_session_keeps_other_cookie_keys(monkeypatch) -> None:
    """配置里只有设备键、没有 web_session 时：补会话但不丢 a1 / gid。

    整串替换成只剩 ``web_session`` 会让这次请求以「另一个设备」的指纹发出，
    与「续期不换设备」的约定相矛盾。
    """

    async def mint_browser(proxy, *, output_logs=False):
        return NEW_SESSION

    async def run() -> tuple[str, bool]:
        client = _client()
        monkeypatch.setattr(session_mod, "_activate_with_browser", mint_browser)
        result = await ensure_web_session(client, "a1=abc; gid=xyz")
        await client.aclose()
        return result

    cookie, refreshed = asyncio.run(run())
    assert refreshed is True
    assert extract_web_session(cookie) == NEW_SESSION
    assert "a1=abc" in cookie and "gid=xyz" in cookie

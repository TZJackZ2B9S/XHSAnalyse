"""``web_session`` 会话管理。

小红书视频清晰度由 Cookie 里的 ``web_session`` 决定：会话失效时平台只下发
720p 单档流，页面表现为 ``user.userInfo`` 缺少 ``user_id`` 字段。本模块负责：

1. 用首页 ``__INITIAL_STATE__`` 的 ``user.userInfo`` 判断当前 Cookie 是否有效；
2. 失效时用 Playwright 无头浏览器重新走 ``login/activate`` 激活流程换取新会话；
3. 把换取到的 ``web_session`` 合并回插件配置，持久化并热更新。

小红书已在首页对**所有**未登录访客（无论是否携带 Cookies）强制跳转
``/login``，该页面不含 ``userInfo.user_id``，因此首页判据在登录墙状态下
无法区分「会话失效」与「访客落地页」。此时不能假定会话有效：假定有效会让
失效会话永不触发续期，档位永久停在 720p。模块因此把判定做成三态，
登录墙下沿用最近一次**有证据**的结论（由 :func:`record_session_verdict`
按真实解析结果回灌），从未得出过结论时判定为不可用并触发一次激活。

激活接口要求 sec SDK 签名，纯 HTTP 无法调用，因此必须借助真实浏览器；
浏览器可执行任意页面脚本，所以激活期间屏蔽下载与弹窗，并用独立 profile
目录避免污染用户数据。
"""

from __future__ import annotations

import re
import json
import time
import random
import asyncio
from typing import TYPE_CHECKING, TypedDict
from pathlib import Path

import httpx

from gsuid_core.logger import logger

from ...utils.resource.RESOURCE_PATH import CACHE_PATH

if TYPE_CHECKING:
    from playwright.async_api import Route as _Route, ProxySettings as _ProxySettings

try:
    from playwright.async_api import Error as _PlaywrightError
except ImportError as error:
    # playwright 是可选依赖。未安装时把错误类型降级成 ``OSError``，让激活
    # 路径的异常捕获保持具体类型，而不是退化成裸 ``Exception`` 兜底。
    # 同时把导入失败的原因打出来：自动续期整条链都依赖它，静默降级会让
    # 「档位一直停在 720p」变成一个无从下手的现象。
    _PlaywrightError = OSError
    logger.warning(f"[XHSAnalyse] playwright 不可用，web_session 自动续期将失效：{error!r}")

HOME_URL = "https://www.xiaohongshu.com/"
# 会话有效性检测：user.userInfo 带 24 位 hex 的 user_id 说明服务端承认这条会话；
# 失效时该字段整个缺失（userId 为 undefined）。guest 与登录态无关，不作判据。
_USER_ID_RE = re.compile(r'"userInfo":\{"user_id":"([0-9a-f]{24})"')
# Cookie 串中截取 web_session 值，用于合并与去重。
# 兼容 ``; web_session=`` 与 ``;web_session=`` 两种分隔写法；值允许为空，
# 否则配置里写成 ``web_session=``（空值）时会被当成「没有这个键」，替换时
# 走追加分支产出重复的 web_session。
_WEB_SESSION_RE = re.compile(r"(?:^|;)\s*web_session=([^;]*)")
# 替换时保留原有分隔符，只换会话值本身。
_WEB_SESSION_VALUE_RE = re.compile(r"((?:^|;)\s*web_session=)[^;]*")
_SESSION_STORE = CACHE_PATH / "web_session.json"

# 检测与激活的节流窗口（秒）。检测本身是一次页面请求，高频解析场景下
# 不能每次都打；激活则更重（真实浏览器），同一会话生命周期内只应发生一次。
_CHECK_INTERVAL = 1800.0
_ACTIVATE_INTERVAL = 300.0

# 无 Cookie 模式下的会话保障：解析需要 4K 档位时同样需要有效 web_session，
# 因此检测到无会话也走一次激活，并把结果缓存供后续解析复用。
_NO_COOKIE_ACTIVATE_INTERVAL = 1800.0


class _LocaleRegion(TypedDict):
    locale: str
    timezone_id: str


class _Viewport(TypedDict):
    width: int
    height: int


class _ActivationFingerprint(TypedDict):
    """一次激活使用的随机化环境特征。"""

    user_agent: str
    viewport: _Viewport
    locale: str
    timezone_id: str


# 激活时随机化的设备/环境特征池：同一套指纹反复激活是被标记的主要原因，
# 每次激活随机组合可以显著降低会话/设备级行为画像的聚集度。
_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
)
_VIEWPORTS: tuple[_Viewport, ...] = (
    {"width": 1920, "height": 1080},
    {"width": 1680, "height": 1050},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 2560, "height": 1440},
)
# locale 与 timezone 必须成对取样：en-US 配 Asia/Shanghai 这类组合在真实
# 设备上几乎不会出现，反而成为可识别的画像信号。
_LOCALE_REGIONS: tuple[_LocaleRegion, ...] = (
    {"locale": "zh-CN", "timezone_id": "Asia/Shanghai"},
    {"locale": "zh-TW", "timezone_id": "Asia/Taipei"},
    {"locale": "zh-HK", "timezone_id": "Asia/Hong_Kong"},
    {"locale": "en-US", "timezone_id": "Asia/Singapore"},
)
# Chromium 可执行路径探测顺序：发行版打包路径优先，playwright 自带兜底。
_CHROMIUM_CANDIDATES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)


def _random_activation_fingerprint() -> _ActivationFingerprint:
    """生成一组随机化的激活指纹（UA / 视口 / 语言·时区成对）。"""

    region = random.choice(_LOCALE_REGIONS)
    viewport = random.choice(_VIEWPORTS)
    return {
        "user_agent": random.choice(_USER_AGENTS),
        "viewport": {"width": viewport["width"], "height": viewport["height"]},
        "locale": region["locale"],
        "timezone_id": region["timezone_id"],
    }


def _resolve_chromium() -> str:
    """返回第一个存在的 Chromium 可执行路径，找不到返回空串。"""

    for path in _CHROMIUM_CANDIDATES:
        if Path(path).is_file():
            return path
    return ""


def _proxy_settings(proxy: str) -> _ProxySettings | None:
    """把 httpx 风格的代理 URL 转成 playwright 需要的 ``ProxySettings``。

    playwright 不接受字符串代理（``new_context`` 会直接报
    ``proxy: expected object, got string``），凭据也不能内联进 ``server``，
    必须拆成 ``username`` / ``password`` 单独传。无凭据时原样透传，保住
    ``host:port`` 这类不带 scheme 的写法。
    """

    if not proxy:
        return None
    parsed = httpx.URL(proxy)
    # 只认「用户名或口令」任一存在就算带凭据：``http://:pass@host`` 这种
    # 空用户名写法同样不能让口令留在 server 里。
    if not (parsed.username or parsed.password):
        return {"server": proxy}
    # httpx 的 netloc 不含 userinfo（0.26 起即为纯 host:port），可直接作 server。
    server = f"{parsed.scheme}://{parsed.netloc.decode()}"
    settings: _ProxySettings = {
        "server": server,
        "username": parsed.username,
        "password": parsed.password,
    }
    return settings


_check_lock = asyncio.Lock()
_activate_lock = asyncio.Lock()
_last_check_at: float = 0.0
# 三态：``None`` 表示「尚未得出有证据的结论」。登录墙下首页判据不可用，
# 此时既不能假定有效（失效会话将永不续期），也不该反复激活（每条消息
# 拉起一次浏览器），只能等真实解析结果回灌。
_last_check_valid: bool | None = None
_last_activate_at: float = 0.0
_last_anonymous_activate_at: float = 0.0

_ACTIVATION_TIMEOUT = 45.0
# 取 cookie 的轮询间隔：sec SDK 一般在首帧后 1s 内完成激活，1s 一跳最坏会
# 白等近 1s，250ms 足够密且开销可忽略。
_COOKIE_POLL_MS = 250


def extract_web_session(cookie: str) -> str:
    """从 Cookie 串中取出 ``web_session`` 值，不存在时返回空串。"""

    match = _WEB_SESSION_RE.search(cookie)
    return match.group(1) if match is not None else ""


def replace_web_session(cookie: str, session: str) -> str:
    """把 Cookie 串中的 ``web_session`` 替换为新值，缺失时追加到末尾。"""

    if not session:
        return cookie
    if _WEB_SESSION_RE.search(cookie) is not None:
        return _WEB_SESSION_VALUE_RE.sub(lambda match: f"{match.group(1)}{session}", cookie, count=1)
    if not cookie:
        return f"web_session={session}"
    return f"{cookie}; web_session={session}"


def _read_cached_session() -> str:
    if not _SESSION_STORE.exists():
        return ""
    try:
        payload = json.loads(_SESSION_STORE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        logger.warning(f"[XHSAnalyse] web_session 缓存文件损坏，忽略：{error}")
        return ""
    if not isinstance(payload, dict):
        logger.warning("[XHSAnalyse] web_session 缓存文件不是对象，忽略")
        return ""
    raw = payload.get("web_session")
    return raw if isinstance(raw, str) else ""


def _cached_session_saved_at() -> float:
    """缓存文件里的写入时间戳；读不到或字段不合法时返回 0。

    进程内的 :data:`_last_anonymous_activate_at` 在重启后归零，只靠它会让
    每次重启后的第一条链接都重新拉一次浏览器；磁盘上的 ``saved_at`` 才是
    跨进程可用的复用依据。
    """

    if not _SESSION_STORE.exists():
        return 0.0
    try:
        payload = json.loads(_SESSION_STORE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        logger.warning(f"[XHSAnalyse] web_session 缓存文件损坏，忽略：{error}")
        return 0.0
    if not isinstance(payload, dict):
        return 0.0
    saved_at = payload.get("saved_at")
    return saved_at if isinstance(saved_at, (int, float)) else 0.0


def _write_cached_session(session: str) -> None:
    _SESSION_STORE.write_text(
        json.dumps({"web_session": session, "saved_at": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )


def _reset_check_state() -> None:
    """会话刚被换新，把「有效」记为一次带证据的判定。

    激活成功本身就是证据（浏览器确实拿到了新会话），因此一并刷新检测时间
    戳，让 :data:`_CHECK_INTERVAL` 缓存生效。否则下一次解析会立刻再打一次
    首页，而首页判据在登录墙下给不出更好的结论。
    """

    global _last_check_at, _last_check_valid
    _last_check_at = time.monotonic()
    _last_check_valid = True


def record_session_verdict(valid: bool) -> None:
    """用真实解析结果回灌会话判定，供后续解析复用。

    首页判据失效后，唯一可靠的证据来自解析本身：带 Cookies 取到笔记说明
    会话可用，被拒后匿名才取到则说明会话已失效。解析层把结论回灌到这里，
    下次解析就能据此决定是否需要激活，而不是在每一条消息里反复试错。
    """

    global _last_check_at, _last_check_valid
    _last_check_at = time.monotonic()
    _last_check_valid = valid


def read_activated_session() -> str:
    """读取最近一次激活到并持久化的 ``web_session`` 值。"""

    return _read_cached_session()


def apply_refreshed_session_needed(cookie: str) -> bool:
    """判断调用方是否需要把新激活的会话写回配置。

    只有当配置里的 ``web_session`` 与最近激活的会话不同时才需要写回，
    避免重复落盘与无意义的配置变更。
    """

    refreshed = read_activated_session()
    return bool(refreshed) and extract_web_session(cookie) != refreshed


async def check_web_session(
    client: httpx.AsyncClient,
    cookie: str,
    *,
    force: bool = False,
) -> bool:
    """判断当前 Cookie 的 ``web_session`` 是否仍被小红书承认。

    结果在 :data:`_CHECK_INTERVAL` 内复用，避免每次解析都请求一次首页。
    未配置 ``web_session`` 时直接返回 ``True``——无 Cookie 解析本就允许，
    会话检测只对配置了 Cookie 的用户有意义。

    返回值恒为 ``bool``：内部的三态判定在出口处一律折叠成「已证明有效」
    与「未证明有效」，调用方只需按后者决定是否续期。判定结论不可得
    （登录墙、请求异常、尚无证据）时返回 ``False``。
    """

    global _last_check_at, _last_check_valid
    if not extract_web_session(cookie):
        return True
    now = time.monotonic()
    if not force and _last_check_at > 0.0 and now - _last_check_at < _CHECK_INTERVAL:
        return _last_check_valid is True
    async with _check_lock:
        # 双重检查：等待锁期间可能已有并发调用完成检测。
        now = time.monotonic()
        if not force and _last_check_at > 0.0 and now - _last_check_at < _CHECK_INTERVAL:
            return _last_check_valid is True
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            ),
        }
        if cookie:
            headers["Cookie"] = cookie
        try:
            response = await client.get(HOME_URL, headers=headers)
        except httpx.HTTPError as error:
            logger.warning(f"[XHSAnalyse] web_session 检测请求失败：{error}")
            return _last_check_valid is True
        # 小红书已对全部未登录访客（携带 Cookies 与否都一样）在首页强制跳转
        # ``/login``，该页不含 ``userInfo.user_id``，因此「落在登录墙」既可能
        # 是访客常态页、也可能叠加了出口 IP 风控，两者都会让首页判据失效。
        # 这种情况下沿用最近一次**有证据**的结论；从未得出过结论时判定为
        # 不可用——无法证明有效就不能假定有效，否则失效会话永久跳过续期。
        final_url = str(response.url)
        if "/login" in final_url or response.status_code in (301, 302, 303, 307, 308):
            logger.debug("[XHSAnalyse] 首页落在登录墙，本次无法得出 web_session 判定")
            return _last_check_valid is True
        valid = _USER_ID_RE.search(response.text) is not None
        _last_check_at = time.monotonic()
        _last_check_valid = valid
        return valid


async def activate_web_session(
    proxy: str = "",
    *,
    output_logs: bool = False,
) -> str:
    """用 Playwright 无头浏览器重新激活，返回新的 ``web_session``。

    激活流程：访问首页 → 页面 sec SDK 自动调用 ``login/activate`` → 服务端
    通过 ``Set-Cookie`` 下发新会话（HttpOnly，JS 读不到，必须从浏览器
    cookie jar 取）。返回空串表示激活失败。

    成功与失败都更新 :data:`_last_activate_at`：失败同样计入节流窗口，
    否则环境持续不可用（playwright 未装、浏览器异常、风控）时每条消息、
    每个候选链接都会拉起一次无头浏览器。窗口内的重复调用直接返回空串，
    不回退到缓存会话——缓存的会话可能早已过期，把它当激活成功返回会让
    上层把过期值写回用户配置。
    """

    global _last_activate_at
    # 无锁快路径：窗口内直接返回，避免多个候选/并发消息都排到激活锁后面
    # 干等一次完整的浏览器启动（最长 _ACTIVATION_TIMEOUT 秒）。锁内会再次
    # 检查，所以这条捷径只影响等待时间，不影响节流语义。
    if _last_activate_at > 0.0 and time.monotonic() - _last_activate_at < _ACTIVATE_INTERVAL:
        return ""
    async with _activate_lock:
        now = time.monotonic()
        if _last_activate_at > 0.0 and now - _last_activate_at < _ACTIVATE_INTERVAL:
            return ""
        try:
            session = await _activate_with_browser(proxy, output_logs=output_logs)
        finally:
            # 无论成败（包括协程被取消）都记账，否则浏览器已经跑过一次、
            # 节流却未生效，下一次调用会立刻再拉一个。
            _last_activate_at = time.monotonic()
        if not session:
            logger.warning("[XHSAnalyse] web_session 激活未取得新会话，稍后自动重试")
            return ""
        _write_cached_session(session)
        _reset_check_state()
        logger.info(f"[XHSAnalyse] web_session 激活成功：{session[:8]}…（已缓存并待写回配置）")
        return session


_MEDIA_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
)


async def _route_media(route: _Route) -> None:
    """屏蔽媒体与字体请求，只放行激活必需的页面、脚本与接口。"""

    url = route.request.url
    lowered = url.lower()
    if lowered.endswith(_MEDIA_SUFFIXES) or "sns-video" in url or "sns-img" in url:
        await route.abort()
        return
    await route.continue_()


async def _run_activation(proxy: str) -> str:
    """启动无头浏览器完成激活，返回新 ``web_session``（失败返回空串）。

    每次激活使用随机化的 UA / 视口 / 语言 / 时区与全新浏览器上下文，
    避免同一套设备指纹反复激活被平台画像标记。
    """

    from playwright.async_api import async_playwright

    executable = _resolve_chromium()
    fingerprint = _random_activation_fingerprint()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=executable or None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=str(fingerprint["user_agent"]),
                viewport=fingerprint["viewport"],
                locale=str(fingerprint["locale"]),
                timezone_id=str(fingerprint["timezone_id"]),
                proxy=_proxy_settings(proxy),
            )
            await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = await context.new_page()
            await page.route("**/*", _route_media)
            # ``commit`` 在响应头到达时就返回，不等 DOM 就绪：激活只需要页面
            # 的 sec SDK 跑起来，而会话是从 context 的 cookie jar 里取的，不
            # 依赖 DOM。实测首页 domcontentloaded 要 1.9s、commit 只要 0.4s，
            # 整次激活从 5.5s 降到 2.9s，轮询仍会等 SDK 把 web_session 写进去。
            await page.goto(
                HOME_URL,
                wait_until="commit",
                timeout=int(_ACTIVATION_TIMEOUT * 1000),
            )
            session = ""
            deadline = time.monotonic() + _ACTIVATION_TIMEOUT
            while time.monotonic() < deadline:
                for cookie in await context.cookies():
                    if cookie["name"] == "web_session" and cookie["value"]:
                        session = cookie["value"]
                if session:
                    break
                await page.wait_for_timeout(_COOKIE_POLL_MS)
            return session
        finally:
            await browser.close()


async def _activate_with_browser(proxy: str, *, output_logs: bool) -> str:
    """驱动无头 Chromium 完成一次访问并取出 ``web_session``。

    只捕获浏览器自动化的可预期异常：playwright 自身错误（启动失败、导航
    超时、页面被关闭）与系统级 ``OSError``（可执行文件缺失、权限不足）。
    未知异常继续上抛，避免把编程错误伪装成「激活失败」。
    """

    del output_logs
    try:
        return await _run_activation(proxy)
    except ImportError as error:
        # 带上原始异常：浏览器依赖缺失有多种成因（包没装、子依赖缺失、
        # 运行环境与测试环境不是同一个解释器），只说「未安装」无法定位。
        logger.warning(f"[XHSAnalyse] 无法导入 playwright，跳过自动激活：{error!r}")
        return ""
    except (_PlaywrightError, OSError) as error:
        logger.warning(f"[XHSAnalyse] web_session 激活浏览器执行失败：{error}")
        return ""


async def ensure_web_session(
    client: httpx.AsyncClient,
    cookie: str,
    *,
    proxy: str = "",
    output_logs: bool = False,
    force_check: bool = False,
) -> tuple[str, bool]:
    """解析入口的会话保障：检测 → 失效则激活 → 返回可用 Cookie。

    返回 ``(cookie, refreshed)``：``cookie`` 是应当用于本次解析的 Cookie 串
    （可能已带新会话），``refreshed`` 表示本次是否发生了激活。激活失败时
    返回原 Cookie，由解析流程按原有降级路径继续。

    无 Cookie 模式同样会激活：4K 档位依赖有效 ``web_session``，匿名访客
    会话不需要登录，因此检测到未配置会话时主动激活一个并缓存，供本次与
    后续解析复用（独立 :data:`_NO_COOKIE_ACTIVATE_INTERVAL` 节流）。
    """

    if extract_web_session(cookie):
        if await check_web_session(client, cookie, force=force_check):
            return cookie, False
        session = await activate_web_session(proxy, output_logs=output_logs)
        if not session:
            return cookie, False
        new_cookie = replace_web_session(cookie, session)
        if new_cookie == cookie:
            return cookie, False
        return new_cookie, True
    return await _ensure_anonymous_session(proxy, cookie=cookie, output_logs=output_logs)


async def _ensure_anonymous_session(
    proxy: str,
    *,
    cookie: str = "",
    output_logs: bool = False,
) -> tuple[str, bool]:
    """没有可用 ``web_session`` 时的会话保障：缓存有效则复用，否则激活。

    ``cookie`` 是调用方原有的其余 Cookie 键（``a1`` / ``webId`` / ``gid`` 等）。
    它们参与小红书的设备与风控画像，必须原样保留——把整串换成只剩一个
    ``web_session``，等于在这次请求中途把设备换掉，正是最容易被风控盯上的
    形态。会话失效（而非缺失）的场景由 :func:`ensure_web_session` 的另一
    条分支处理。
    """

    global _last_anonymous_activate_at
    cached = _read_cached_session()
    age = time.time() - _cached_session_saved_at() if cached else float("inf")
    if cached and age < _NO_COOKIE_ACTIVATE_INTERVAL:
        # 缓存会话在复用窗口内：直接使用，不做网络检测（游客会话本来就是
        # 激活即用的，失效表现只是档位降级，不值得为它多打一次首页）。
        # 判据用磁盘上的 ``saved_at`` 而不是进程内时间戳，否则每次重启后的
        # 第一条链接都要白等一次浏览器启动。
        return replace_web_session(cookie, cached), False
    session = await activate_web_session(proxy, output_logs=output_logs)
    if not session:
        return cookie, False
    _last_anonymous_activate_at = time.monotonic()
    if output_logs:
        logger.info("[XHSAnalyse] 未检测到可用 web_session，已自动激活游客会话用于本次解析")
    return replace_web_session(cookie, session), True

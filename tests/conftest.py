"""测试环境：以文件级包对象加载插件内层包，避免执行触发器注册入口。"""

import sys
import importlib.util
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parents[1]
INNER_PACKAGE = PLUGIN_ROOT / "XHSAnalyse"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# 预注册命名空间包对象，使叶子模块的相对导入可解析；不执行 __init__.py，
# 避免在测试环境中触发 Plugins/SV 注册。
for package_name, package_path in (
    ("XHSAnalyse", INNER_PACKAGE),
    ("XHSAnalyse.utils", INNER_PACKAGE / "utils"),
    ("XHSAnalyse.utils.parse", INNER_PACKAGE / "utils" / "parse"),
    ("XHSAnalyse.utils.media", INNER_PACKAGE / "utils" / "media"),
    ("XHSAnalyse.utils.resource", INNER_PACKAGE / "utils" / "resource"),
    ("XHSAnalyse.xhs_config", INNER_PACKAGE / "xhs_config"),
):
    if package_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package_name,
            package_path / "__init__.py",
            submodule_search_locations=[str(package_path)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载测试包：{package_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module


# ── 让 xhs_parse 可在测试进程导入 ──
# SV() 依赖调用栈里出现 plugins/buildin_plugins 路径段来推断插件名，
# 测试进程里不满足该前提会抛 ValueError；这里用最小桩替换 SV 装饰器，
# 使 xhs_parse 的入口函数可被真实调用与断言（形状断言挡不住结构改动）。
def _install_sv_stub() -> None:
    import types

    import gsuid_core.sv as real_sv

    class _StubSV:
        def __init__(self, name: str, *args: object, **kwargs: object) -> None:
            self.name = name

        def _passthrough(self, *args: object, **kwargs: object):
            def decorator(func):
                return func

            return decorator

        on_command = _passthrough
        on_message = _passthrough
        on_fullmatch = _passthrough
        on_prefix = _passthrough
        on_keyword = _passthrough
        on_regex = _passthrough
        on_file = _passthrough
        on_meta = _passthrough

    import sys

    sys.modules["gsuid_core.sv"] = types.SimpleNamespace(
        SV=_StubSV,
        Plugins=real_sv.Plugins,
        config_plugins=real_sv.config_plugins,
        get_plugin_available_prefix=real_sv.get_plugin_available_prefix,
    )


_install_sv_stub()


@pytest.fixture(autouse=True)
def _isolate_session_cache(tmp_path, monkeypatch):
    """把 web_session 缓存与判定状态隔离到临时目录。

    测试绝不允许读写线上 ``data/XHSAnalyse/cache/web_session.json``：解析入口
    会据此决定是否把新会话写回插件配置，一旦共享同一份缓存，跑测试就可能
    悄悄改掉真实用户的 Cookies 配置。
    """

    from XHSAnalyse.utils.parse import session as session_mod

    monkeypatch.setattr(session_mod, "_SESSION_STORE", tmp_path / "web_session.json")
    monkeypatch.setattr(session_mod, "_last_check_at", 0.0)
    monkeypatch.setattr(session_mod, "_last_check_valid", None)
    monkeypatch.setattr(session_mod, "_last_activate_at", 0.0)
    monkeypatch.setattr(session_mod, "_last_anonymous_activate_at", 0.0)
    yield


@pytest.fixture(autouse=True)
def _forbid_real_browser_activation(monkeypatch):
    """测试环境默认不拉起真实 Chromium。

    解析入口在无 Cookies 或会话失效时会真的启动无头浏览器；一旦某个用例
    忘了打桩，测试就会变成一次真实的网络与浏览器调用（数秒级、且会消耗
    平台的激活配额）。这里统一兜底成「激活不可用」，需要激活语义的用例
    再自行覆盖 ``_activate_with_browser``。
    """

    from XHSAnalyse.utils.parse import session as session_mod

    async def _activation_unavailable(proxy: str, *, output_logs: bool = False) -> str:
        del proxy, output_logs
        return ""

    monkeypatch.setattr(session_mod, "_activate_with_browser", _activation_unavailable)
    yield

"""测试环境：以文件级包对象加载插件内层包，避免执行触发器注册入口。"""

import sys
import importlib.util
from pathlib import Path

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

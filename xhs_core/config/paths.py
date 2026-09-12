"""纯逻辑层使用的数据路径。"""

from pathlib import Path

from gsuid_core.data_store import get_res_path

MAIN_PATH: Path = get_res_path("XHSAnalyse")
CONFIG_PATH: Path = MAIN_PATH / "config.json"
CACHE_PATH: Path = MAIN_PATH / "cache"
CACHE_PATH.mkdir(parents=True, exist_ok=True)

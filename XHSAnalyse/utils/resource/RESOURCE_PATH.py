"""兼容入口：路径常量实现在 ``xhs_core.config.paths``。"""

from xhs_core.config.paths import MAIN_PATH, CACHE_PATH, CONFIG_PATH

COOKIE_PATH = MAIN_PATH / "xhs_ck.txt"

__all__ = ["CACHE_PATH", "CONFIG_PATH", "COOKIE_PATH", "MAIN_PATH"]

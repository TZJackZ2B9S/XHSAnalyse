"""兼容入口：配置实现在 ``xhs_core.config``。"""

from xhs_core.config.xhs_config import XhsSettings, GsCoreXHSConfig, get_settings

__all__ = ["GsCoreXHSConfig", "XhsSettings", "get_settings"]

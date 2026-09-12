"""兼容入口：配置实现在 ``xhs_core.config``。"""

from xhs_core.config.xhs_config import XhsSettings, XHSAnalyseConfig, get_settings

__all__ = ["XHSAnalyseConfig", "XhsSettings", "get_settings"]

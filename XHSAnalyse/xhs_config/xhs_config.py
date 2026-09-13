"""XHSAnalyse 配置实例与类型化读取。"""

from dataclasses import dataclass

from gsuid_core.utils.plugins_config.models import GsIntConfig, GsStrConfig, GsBoolConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_default import CONFIG_DEFAULT
from ..utils.resource.RESOURCE_PATH import CONFIG_PATH

XHSAnalyseConfig = StringConfig("XHSAnalyse", CONFIG_PATH, CONFIG_DEFAULT)
# 插件通过符号链接安装时，Core 无法从真实源码路径反推插件名，显式绑定以关联控制台配置组。
XHSAnalyseConfig.plugin_name = "XHSAnalyse"

# 升级时清理旧 options 与范围元数据，避免控制台沿用旧值。
_render_scale_config = XHSAnalyseConfig.get_config("renderScale")
if isinstance(_render_scale_config, GsIntConfig):
    config_changed = False
    if _render_scale_config.options:
        _render_scale_config.options = []
        config_changed = True
    if _render_scale_config.max_value != 500:
        _render_scale_config.max_value = 500
        config_changed = True
    bounded_scale = max(100, min(500, _render_scale_config.data))
    if _render_scale_config.data != bounded_scale:
        _render_scale_config.data = bounded_scale
        config_changed = True
    if config_changed:
        XHSAnalyseConfig.write_config()

# 核心会保留孤儿键，显式删除已废弃的卡片压缩配置。
if "renderCompression" in XHSAnalyseConfig.config:
    XHSAnalyseConfig.config.pop("renderCompression")
    XHSAnalyseConfig.write_config()

_VIDEO_HEIGHTS = {
    "4k": 2160,
    "2160p": 2160,
    "2k": 1440,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
}


@dataclass(frozen=True, slots=True)
class XhsSettings:
    """单次业务使用的配置快照。"""

    cookie: str
    proxy: str
    detect_links: bool
    video_quality: str
    max_media_size: int
    fetch_retries: int
    prefer_original_image: bool
    prefer_hdr_video: bool
    fallback_without_cookie: bool
    convert_live_photo: bool
    video_send_type: str
    render_card: bool
    render_scale: float
    output_logs: bool

    @property
    def target_video_height(self) -> int:
        return _VIDEO_HEIGHTS[self.video_quality]


def _str(name: str) -> str:
    item = XHSAnalyseConfig.get_config(name)
    if not isinstance(item, GsStrConfig):
        raise TypeError(f"XHSAnalyse 配置 {name} 类型错误")
    return item.data


def _bool(name: str) -> bool:
    item = XHSAnalyseConfig.get_config(name)
    if not isinstance(item, GsBoolConfig):
        raise TypeError(f"XHSAnalyse 配置 {name} 类型错误")
    return item.data


def _int(name: str) -> int:
    item = XHSAnalyseConfig.get_config(name)
    if not isinstance(item, GsIntConfig):
        raise TypeError(f"XHSAnalyse 配置 {name} 类型错误")
    return item.data


def _render_scale(value: int) -> float:
    return max(1.0, min(5.0, value / 100))


def get_settings() -> XhsSettings:
    max_size = max(0, _int("maxMediaSize"))
    video_quality = _str("videoQuality").strip().lower()
    if video_quality not in _VIDEO_HEIGHTS:
        video_quality = "1080p"
    return XhsSettings(
        cookie=_str("cookie").strip(),
        proxy=_str("proxy").strip(),
        detect_links=_bool("detectLinks"),
        video_quality=video_quality,
        max_media_size=(max_size or 512) * 1024 * 1024,
        fetch_retries=max(1, _int("fetchRetries")),
        prefer_original_image=_bool("preferOriginalImage"),
        prefer_hdr_video=_bool("preferHdrVideo"),
        fallback_without_cookie=_bool("fallbackWithoutCookie"),
        convert_live_photo=_bool("convertLivePhoto"),
        video_send_type=_str("videoSendType").strip().lower(),
        render_card=_bool("renderCard"),
        render_scale=_render_scale(_int("renderScale")),
        output_logs=_bool("outputLogs"),
    )


__all__ = ["XHSAnalyseConfig", "XhsSettings", "get_settings"]

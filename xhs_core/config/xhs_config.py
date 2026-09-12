"""GsCoreXHS 配置读取。"""

from dataclasses import dataclass

from gsuid_core.utils.plugins_config.models import GsIntConfig, GsStrConfig, GsBoolConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .paths import CONFIG_PATH
from .config_default import CONFIG_DEFAULT

GsCoreXHSConfig = StringConfig("GsCoreXHS", CONFIG_PATH, CONFIG_DEFAULT)


@dataclass(frozen=True, slots=True)
class XhsSettings:
    """单次业务使用的配置快照。"""

    cookie: str
    detect_links: bool
    max_media_size: int
    fetch_retries: int
    convert_live_photo: bool
    video_send_type: str
    output_logs: bool


def _str(name: str) -> str:
    item = GsCoreXHSConfig.get_config(name)
    if not isinstance(item, GsStrConfig):
        raise TypeError(f"GsCoreXHS 配置 {name} 类型错误")
    return item.data


def _bool(name: str) -> bool:
    item = GsCoreXHSConfig.get_config(name)
    if not isinstance(item, GsBoolConfig):
        raise TypeError(f"GsCoreXHS 配置 {name} 类型错误")
    return item.data


def _int(name: str) -> int:
    item = GsCoreXHSConfig.get_config(name)
    if not isinstance(item, GsIntConfig):
        raise TypeError(f"GsCoreXHS 配置 {name} 类型错误")
    return item.data


def get_settings() -> XhsSettings:
    max_size = max(0, _int("maxMediaSize"))
    return XhsSettings(
        cookie=_str("cookie").strip(),
        detect_links=_bool("detectLinks"),
        max_media_size=(max_size or 512) * 1024 * 1024,
        fetch_retries=max(1, _int("fetchRetries")),
        convert_live_photo=_bool("convertLivePhoto"),
        video_send_type=_str("videoSendType").strip().lower(),
        output_logs=_bool("outputLogs"),
    )

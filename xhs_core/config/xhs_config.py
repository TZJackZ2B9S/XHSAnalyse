"""XHSAnalyse 配置读取。"""

from dataclasses import dataclass

from gsuid_core.utils.plugins_config.models import GsIntConfig, GsStrConfig, GsBoolConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .paths import CONFIG_PATH
from .config_default import CONFIG_DEFAULT

XHSAnalyseConfig = StringConfig("XHSAnalyse", CONFIG_PATH, CONFIG_DEFAULT)


@dataclass(frozen=True, slots=True)
class XhsSettings:
    """单次业务使用的配置快照。"""

    cookie: str
    detect_links: bool
    video_quality: str
    max_media_size: int
    fetch_retries: int
    prefer_original_image: bool
    prefer_hdr_video: bool
    fallback_without_cookie: bool
    convert_live_photo: bool
    video_send_type: str
    output_logs: bool

    @property
    def target_video_height(self) -> int:
        return {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720}.get(
            self.video_quality,
            1080,
        )


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


def get_settings() -> XhsSettings:
    max_size = max(0, _int("maxMediaSize"))
    video_quality = _str("videoQuality").strip().lower()
    if video_quality not in {"2160p", "1440p", "1080p", "720p"}:
        video_quality = "1080p"
    return XhsSettings(
        cookie=_str("cookie").strip(),
        detect_links=_bool("detectLinks"),
        video_quality=video_quality,
        max_media_size=(max_size or 512) * 1024 * 1024,
        fetch_retries=max(1, _int("fetchRetries")),
        prefer_original_image=_bool("preferOriginalImage"),
        prefer_hdr_video=_bool("preferHdrVideo"),
        fallback_without_cookie=_bool("fallbackWithoutCookie"),
        convert_live_photo=_bool("convertLivePhoto"),
        video_send_type=_str("videoSendType").strip().lower(),
        output_logs=_bool("outputLogs"),
    )

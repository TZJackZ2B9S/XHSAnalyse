"""GsCoreXHS 配置定义。"""

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
)

CONFIG_DEFAULT: dict[str, GSC] = {
    "cookie": GsStrConfig(
        title="小红书 Cookie",
        desc="直接链接和高画质视频需要 Cookie；留空时仅支持带 xsec_token 的分享链接。",
        data="",
        secret=True,
    ),
    "detectLinks": GsBoolConfig(
        title="自动解析链接",
        desc="普通消息中出现小红书链接时自动解析，无需发送命令。",
        data=True,
    ),
    "maxMediaSize": GsIntConfig(
        title="单文件大小上限（MB）",
        desc="单个图片或视频超过此大小时跳过；0 使用 512 MB 安全上限。",
        data=512,
        max_value=4096,
    ),
    "fetchRetries": GsIntConfig(
        title="媒体下载重试次数",
        desc="单个媒体下载失败后的最大尝试次数。",
        data=3,
        max_value=10,
    ),
    "convertLivePhoto": GsBoolConfig(
        title="合成 Live Photo",
        desc="将小红书实况图的静态封面和视频合成为 Motion Photo JPEG。",
        data=True,
    ),
    "videoSendType": GsStrConfig(
        title="视频发送方式",
        desc="base64 兼容性最好；file 以 file:// 发送，要求 Bot 端能访问 Core 同一路径。",
        data="base64",
        options=["base64", "file"],
    ),
    "outputLogs": GsBoolConfig(
        title="详细日志",
        desc="记录短链展开、笔记获取和媒体下载过程。",
        data=False,
    ),
}

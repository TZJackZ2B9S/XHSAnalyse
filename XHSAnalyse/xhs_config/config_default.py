"""XHSAnalyse 配置定义。"""

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
)

CONFIG_DEFAULT: dict[str, GSC] = {
    "cookie": GsStrConfig(
        title="Cookies",
        desc=(
            "小红书网页 Cookie。无 Cookies 时分享链接通常最高只能获取 720p；"
            "有 Cookies 也不保证达到目标画质，最终仍取决于原视频提供的清晰度。"
        ),
        data="",
        secret=True,
    ),
    "proxy": GsStrConfig(
        title="代理服务器地址",
        desc="留空使用直连；可填写 HTTP 或 HTTPS 代理地址，例如 http://127.0.0.1:7890。",
        data="",
    ),
    "detectLinks": GsBoolConfig(
        title="自动解析消息中的小红书链接",
        desc="开启后，普通消息中出现小红书链接会自动解析，无需发送 rn 命令。",
        data=True,
    ),
    "videoQuality": GsStrConfig(
        title="视频清晰度选择",
        desc=(
            "选择下载画质上限：2160p / 1440p / 1080p / 720p。"
            "无 Cookies 时通常最高 720p；有 Cookies 也不保证达到所选画质。"
            "若原视频最高清晰度低于所选值，则自动按实际最高清晰度下载。"
        ),
        data="1080p",
        options=["2160p", "1440p", "1080p", "720p"],
    ),
    "videoSendType": GsStrConfig(
        title="视频发送方式",
        desc=(
            "base64：由 GsCore 编码后发送，兼容性最好，但大视频占用内存较高；"
            "file：视频落盘后以 file:// 发送，节省内存，但要求 Bot 端能访问 Core 同一路径。"
        ),
        data="base64",
        options=["base64", "file"],
    ),
    "renderCard": GsBoolConfig(
        title="是否开启卡片渲染",
        desc="发送媒体前生成一张带封面、标题和互动数据的卡片；渲染失败时改为发送文案消息。",
        data=True,
    ),
    "renderScale": GsIntConfig(
        title="卡片渲染精度",
        desc="调整卡片输出精度，范围 100%～500%；数值越高越清晰，同时会增加图片体积和内存占用。",
        data=150,
        max_value=500,
    ),
    "preferOriginalImage": GsBoolConfig(
        title="图片优先原图",
        desc="开启后优先还原 CI 原图直链；关闭时使用页面提供的图片地址。",
        data=True,
    ),
    "preferHdrVideo": GsBoolConfig(
        title="视频优先 HDR",
        desc="同一清晰度存在多个流时优先选择 HDR；不会为了 HDR 降低目标清晰度。",
        data=True,
    ),
    "fallbackWithoutCookie": GsBoolConfig(
        title="Cookies 失败后无登录回退",
        desc="Cookies 失效或被风控时，是否删除 Cookies 重试无登录模式。",
        data=True,
    ),
    "convertLivePhoto": GsBoolConfig(
        title="合成 Live Photo",
        desc="将实况图封面和视频合成为 Motion Photo JPEG；关闭后按普通图片处理。",
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
    "outputLogs": GsBoolConfig(
        title="详细日志",
        desc="记录短链展开、笔记获取、画质选择和媒体下载过程。",
        data=False,
    ),
}

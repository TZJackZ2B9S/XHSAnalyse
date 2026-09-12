# XHSAnalyse

GsCore 小红书解析插件。支持分享链接、笔记链接、图片、视频、HDR 和 Live 图，不包含扫码登录。

## 安装

在 GsCore 的 `gsuid_core/plugins/` 目录执行：

```bash
git clone https://github.com/<your-name>/XHSAnalyse.git
```

重启 GsCore。Live 图转 JPEG 需要 `ffmpeg`，HEIF 封面另需 `heif-convert`。

## 使用

```text
xhs https://xhslink.cn/o/xxxx
xhs https://www.xiaohongshu.com/explore/xxxxxxxxxxxxxxxxxxxxxxxx
xhs帮助
```

开启自动解析后，直接发送小红书链接也会解析。

## 配置

配置文件：`data/XHSAnalyse/config.json`。

| 配置项 | 说明 |
| --- | --- |
| Cookies | 小红书网页 Cookie；无 Cookies 时视频通常最高 720p，有 Cookies 也不保证达到目标画质 |
| 自动解析消息中的小红书链接 | 普通消息出现链接时自动解析 |
| 视频清晰度选择 | `2160p` / `1440p` / `1080p` / `720p`；源视频不足时自动取实际最高画质 |
| 视频发送方式 | `base64` 兼容性好；`file` 节省内存，但 Bot 端需能访问 Core 同一路径 |
| 图片优先原图 | 优先还原 CI 原图直链 |
| 视频优先 HDR | 有 HDR 流时优先选择 |
| Cookies 失败后无登录回退 | Cookie 失效时重试无登录模式 |
| 合成 Live Photo | 合成 Motion Photo JPEG |
| 单文件大小上限 | 超过限制的媒体跳过 |
| 媒体下载重试次数 | 下载失败后的最大尝试次数 |
| 详细日志 | 输出调试日志 |

## 说明

- 无 Cookies 时分享链接通常只能获取 720p；有 Cookies 时也受源视频实际清晰度限制。
- 选择 1080p 但源视频只有 720p，会下载 720p；选择 720p 但源视频有 1080p，最多下载 720p。
- 插件不使用 Puppeteer、Playwright 或 Chromium。

## 许可

MIT

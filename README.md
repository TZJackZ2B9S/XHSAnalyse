<div align="center">
  <img src="ICON.png" width="128" alt="XHSAnalyse">

  <h1>XHSAnalyse</h1>
</div>

GsCore 小红书解析插件。支持分享链接、笔记链接、图片、视频、HDR 和 Live 图。

## 安装

将本插件目录放入 GsCore 的 `gsuid_core/plugins/` 目录（或通过 WebConsole 安装插件）。

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
| Cookies | 小红书网页 Cookie； |
| 代理服务器地址 | HTTP/HTTPS 代理地址；留空使用直连 |
| 自动解析消息中的小红书链接 | 普通消息出现链接时自动解析 |
| 视频清晰度选择 | `2160p` / `1440p` / `1080p` / `720p`；源视频不足时自动取实际最高画质 |
| 视频发送方式 | `base64` 兼容性好；`file` 节省内存，但 Bot 端需能访问 Core 同一路径 |
| 是否开启卡片渲染 | 默认开启；失败时自动发送文案消息，可在配置中关闭 |
| 卡片渲染精度 | 50%～200%；默认 150%，数值越高越清晰但图片更大、内存占用更高 |
| 图片优先原图 | 优先还原 CI 原图直链 |
| 视频优先 HDR | 有 HDR 流时优先选择 |
| Cookies 失败后无登录回退 | Cookie 失效时重试无登录模式 |
| 合成 Live Photo | 合成 Motion Photo JPEG |
| 单文件大小上限 | 超过限制的媒体跳过 |
| 媒体下载重试次数 | 下载失败后的最大尝试次数 |
| 详细日志 | 输出调试日志 |

## 说明

- 卡片或文案会作为独立消息发送；单媒体直接发送，多媒体才使用合并转发。
- 无 Cookies 时分享链接通常只能获取 720p；有 Cookies 时也受源视频实际清晰度限制。
- 插件不依赖浏览器。

## 许可

MIT

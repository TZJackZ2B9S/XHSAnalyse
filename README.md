<div align="center">
  <img src="ICON.png" width="128" alt="XHSAnalyse">

  <h1>XHSAnalyse</h1>
</div>

GsCore 的小红书笔记解析与媒体下载插件。支持分享短链、笔记直链、无水印图片、视频、HDR 和 Live 图；不使用浏览器，也不包含扫码登录。

## 功能

- 自动识别消息中的小红书链接，也可通过 `小红书 <链接>` 手动解析。
- 支持 `xhslink.com` / `xhslink.cn` 分享短链和 `xiaohongshu.com` 笔记链接。
- 图片优先还原 CI 原图，视频按可播放性、HDR、分辨率、码率和编码选择最高画质。
- 支持 Live 图合成 Motion Photo JPEG，兼容 OPPO / 小米 / Google 相册。
- 解析结果以合并转发发送，标题、作者、发布时间和封面在首条节点。
- 媒体流式落盘，限制单文件大小，避免大视频占满内存。

## 安装

在 GsCore 的 `gsuid_core/plugins/` 目录执行：

```bash
git clone https://github.com/<your-name>/XHSAnalyse.git
```

重启 GsCore 后，插件会按 `pyproject.toml` 自动安装 `httpx`、`aiofiles`。

Live 图封面转 JPEG 需要 `ffmpeg`，HEIF 封面还需要 `heif-convert`。缺少工具时普通图片、视频仍可正常使用，Live 图会跳过。

```bash
sudo apt update
sudo apt install -y ffmpeg libheif-examples
```

## 使用

```text
小红书 https://xhslink.com/xxxx
xhs https://www.xiaohongshu.com/explore/xxxxxxxxxxxxxxxxxxxxxxxx
小红书帮助
```

开启 `自动解析链接` 后，直接发送小红书链接也会触发解析。解析结果会以合并转发发送。

## 配置

配置文件：`data/XHSAnalyse/config.json`，也可在 Web 控制台修改。

| 配置项 | 说明 |
| --- | --- |
| `cookie` | 小红书网页 Cookie；直链和高画质视频需要，留空时只支持带 `xsec_token` 的分享链接 |
| `detectLinks` | 普通消息中出现小红书链接时自动解析 |
| `maxMediaSize` | 单个媒体大小上限，单位 MB；`0` 使用 512 MB 安全上限 |
| `fetchRetries` | 媒体下载失败后的重试次数 |
| `convertLivePhoto` | 是否把实况图封面和视频合成为 Motion Photo JPEG |
| `videoSendType` | `base64` 兼容性最好；`file` 以 `file://` 发送，需要 Bot 端能访问 Core 同一路径 |
| `outputLogs` | 是否输出详细调试日志 |

## 说明

- 插件不会调用 Puppeteer、Playwright 或 Chromium，也不提供扫码登录。
- 小红书接口会变化；如果直链解析失败，优先使用 App 分享出的短链。
- 临时媒体下载到 `data/XHSAnalyse/cache`，发送完成后删除；启动时会清理超过 24 小时的残留文件。

## 测试

```bash
PYTHONPATH=/path/to/gsuid_core python -m pytest -q
```

测试覆盖 URL 解析、笔记状态解析、HDR/EF 视频选流、Live 图识别、Motion Photo XMP 和转发节点构建。

## 许可

MIT

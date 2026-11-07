# XHSAnalyse

GsCore 小红书解析插件：发送分享链接或笔记链接，取回无水印图片、视频、HDR 与 Live 图。

## 安装

在 `gsuid_core/plugins/` 下执行 `git clone --depth 1 https://github.com/TZJackZ2B9S/XHSAnalyse.git XHSAnalyse`，重启 Core；也可在 WebConsole 插件页安装。

依赖 `httpx`、`aiofiles`、`pillow`、`pytakumi`、`qrcode[pil]`，Core 启动时会自动装。以下外部工具可选，缺失时插件照常加载、只影响对应功能：

- `ffmpeg`：Live 图转 JPEG
- `heif-convert`：HEIF 封面
- `playwright` + Chromium：`autoRefreshSession` 自动续期

Debian/Ubuntu 用 `sudo apt install -y ffmpeg libheif-examples`；Docker 部署要在容器内装，重建容器后需重装。

## 使用

```text
rn https://xhslink.cn/o/xxxx
rn https://www.xiaohongshu.com/explore/xxxxxxxxxxxxxxxxxxxxxxxx
rn帮助
```

命令 `rn`（别名 `小红书·解析链接`、`小红书·下载笔记`、`XHS·去水印`），支持 xhslink 短链和 xiaohongshu / rednote 笔记链接，一条消息可带多个链接。开启 `detectLinks` 后，普通消息里的链接也会自动解析。

## 配置

配置文件 `data/XHSAnalyse/config.json`，也可在 WebConsole 修改。

| 配置项 | 说明 |
| --- | --- |
| `cookie` | 小红书网页 Cookie，真正起作用的是 `web_session` |
| `autoRefreshSession` | 解析前检测并自动续期 `web_session` |
| `proxy` | HTTP/HTTPS 代理地址，留空直连 |
| `detectLinks` | 自动解析普通消息里的笔记链接 |
| `videoQuality` | 画质上限 `2160p` / `1440p` / `1080p` / `720p`，默认 `1080p` |
| `videoSendType` | `base64` 兼容性最好；`file` 省内存但要求 Bot 端能访问同一路径 |
| `imageSendType` | `framework` 跟随 Core 设置；`file` 走 `file://`，保留 Live 图与 HDR 原格式 |
| `renderCard` / `renderScale` | 是否渲染封面卡片；卡片精度 100～500，默认 150 |
| `preferOriginalImage` / `preferHdrVideo` | 优先还原原图直链；同清晰度多流时优先 HDR |
| `fallbackWithoutCookie` / `convertLivePhoto` | Cookie 失效时回退匿名模式；合成 Motion Photo JPEG |
| `maxMediaSize` / `fetchRetries` | 单文件大小上限 MB（0 用 512）；单个媒体下载重试次数 |
| `outputLogs` | 输出解析、画质选择与下载过程的调试日志 |

## 注意

- 无 Cookies 时视频通常最高 720p；有 Cookies 也受源视频实际清晰度限制。
- 发送方式选 `file` 时，Bot 端必须能访问 Core 的同一路径（同机或共享目录），否则媒体发不出去。
- Core 不读取系统代理环境变量，需要代理时填 `proxy`。

## 许可

MIT。卡片 UI 设计参考 [karin-plugin-kkk](https://github.com/ikenxuan/karin-plugin-kkk)。

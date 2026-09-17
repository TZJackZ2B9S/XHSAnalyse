<div align="center">
  <img src="ICON.png" width="128" alt="XHSAnalyse">

  <h1>XHSAnalyse</h1>
</div>

GsCore 小红书解析插件。支持分享链接、笔记链接、图片、视频、HDR 和 Live 图。

## 安装

在 GsCore 的 `gsuid_core/plugins/` 目录执行：

```bash
git clone --depth 1 https://github.com/TZJackZ2B9S/XHSAnalyse.git XHSAnalyse
```

重启 GsCore。也可以直接在 WebConsole 的插件页安装，或把仓库下载后解压到 `gsuid_core/plugins/XHSAnalyse`。

Live 图转 JPEG 需要 `ffmpeg`，HEIF 封面另需 `heif-convert`。缺少时插件仍可加载，只有对应功能不可用。

Debian/Ubuntu：

```bash
sudo apt update
sudo apt install -y ffmpeg libheif-examples
```

Docker 部署的 GsCore 要在容器内装，宿主机装了容器里也用不到：

```bash
docker exec -it <容器名> apt update
docker exec -it <容器名> apt install -y ffmpeg libheif-examples
```

容器重启后依然有效；重建容器则需要在镜像的 Dockerfile 里加上 `RUN apt-get update && apt-get install -y ffmpeg libheif-examples`。

部分 HEIF 封面需要较新的 libheif；如果 `heif-convert` 转换失败，可从 [libheif 官方仓库](https://github.com/strukturag/libheif)自行编译安装。

## 使用

```text
rn https://xhslink.cn/o/xxxx
rn https://www.xiaohongshu.com/explore/xxxxxxxxxxxxxxxxxxxxxxxx
rn帮助
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
| 图片发送方式 | `framework` 跟随 GsCore 图片发送配置；`file` 使用本地文件路径发送，适合 Live/HDR 和大图 |
| 是否开启卡片渲染 | 默认开启；失败时自动发送文案消息，可在配置中关闭 |
| 卡片渲染精度 | 100%～500%；默认 150%，数值越高越清晰但图片更大、内存占用更高 |
| 图片优先原图 | 优先还原 CI 原图直链 |
| 视频优先 HDR | 有 HDR 流时优先选择 |
| Cookies 失败后无登录回退 | Cookie 失效时重试无登录模式 |
| 合成 Live Photo | 合成 Motion Photo JPEG |
| 单文件大小上限 | 超过限制的媒体跳过 |
| 媒体下载重试次数 | 下载失败后的最大尝试次数 |
| 详细日志 | 输出调试日志 |

## 说明

- 封面下载完成后会立即开始卡片渲染和发送，其余媒体继续并发下载。
- 卡片或文案会作为独立消息发送；单媒体直接发送，多媒体才使用合并转发。
- 实况图会保留动态视频并按配置合成为 Motion Photo；HDR 图保留原始 UHDR/HEIF 媒体发送，卡片仅使用临时预览图渲染。
- `file` 图片模式使用 GsCore 原生 `image + file://` 协议，要求 NB 适配器与 GsCore 位于同一台机器（或共享相同媒体路径），避免 Base64 大帧导致连接断开。
- 优先从页面提供的原始流多档候选中按配置选择 2160p / 1440p / 1080p / 720p；源不足时按实际最高画质下载。
- 无 Cookies 时分享链接通常只能获取 720p；有 Cookies 时也受源视频实际清晰度限制。
- 插件不依赖浏览器。

## 致谢

- 卡片 UI 设计参考自 [karin-plugin-kkk](https://github.com/ikenxuan/karin-plugin-kkk)。

## 许可

MIT

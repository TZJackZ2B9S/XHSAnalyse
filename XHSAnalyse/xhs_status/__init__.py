"""XHSAnalyse 状态指标。"""

import asyncio
from pathlib import Path

from PIL import Image

from gsuid_core.status.plugin_status import register_status

from ..utils.resource.RESOURCE_PATH import CACHE_PATH


async def cache_count() -> int:
    paths = await asyncio.to_thread(lambda: tuple(CACHE_PATH.glob("xhs_*")))
    return len(paths)


async def cache_size_mb() -> float:
    paths = await asyncio.to_thread(lambda: tuple(CACHE_PATH.glob("xhs_*")))
    total = sum(path.stat().st_size for path in paths if path.is_file())
    return round(total / 1024 / 1024, 1)


def _icon() -> Image.Image:
    return Image.open(Path(__file__).parents[2] / "ICON.png")


register_status(
    _icon(),
    "XHSAnalyse",
    {
        "缓存文件": cache_count,
        "缓存大小MB": cache_size_mb,
    },
)

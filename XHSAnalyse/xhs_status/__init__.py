"""XHSAnalyse 状态指标。"""

import asyncio
from pathlib import Path

from PIL import Image

from gsuid_core.status.plugin_status import register_status

from ..utils.resource.RESOURCE_PATH import CACHE_PATH


def _cache_stats() -> tuple[int, int]:
    count = 0
    total = 0
    for path in CACHE_PATH.glob("xhs_*"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            continue
        count += 1
        total += stat.st_size
    return count, total


async def cache_count() -> int:
    count, _ = await asyncio.to_thread(_cache_stats)
    return count


async def cache_size_mb() -> float:
    _, total = await asyncio.to_thread(_cache_stats)
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

"""GsCoreXHS 状态指标。"""

from PIL import Image

from xhs_core.config.paths import CACHE_PATH
from gsuid_core.status.plugin_status import register_status


async def cache_count() -> int:
    return len(tuple(CACHE_PATH.glob("xhs_*")))


async def cache_size_mb() -> float:
    total = sum(path.stat().st_size for path in CACHE_PATH.glob("xhs_*") if path.is_file())
    return round(total / 1024 / 1024, 1)


def _icon() -> Image.Image:
    from pathlib import Path

    return Image.open(Path(__file__).parents[2] / "ICON.png")


register_status(
    _icon(),
    "GsCoreXHS",
    {
        "缓存文件": cache_count,
        "缓存大小MB": cache_size_mb,
    },
)

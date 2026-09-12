"""GsCoreXHS 插件入口。"""

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from gsuid_core.sv import Plugins  # noqa: E402 - xhs_core 必须先加入 sys.path

Plugins(
    name="GsCoreXHS",
    allow_empty_prefix=True,
    alias=["xhs", "小红书"],
)

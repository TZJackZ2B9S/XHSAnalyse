"""测试环境：加载可独立测试的 xhs_core 包。"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

"""生成 XHSAnalyse 帮助图。"""

import json
from pathlib import Path

import aiofiles
from PIL import Image

from gsuid_core.sv import get_plugin_available_prefix
from gsuid_core.help.model import PluginHelp
from gsuid_core.help.draw_new_plugin_help import get_new_help

from ..version import XHSAnalyse_version

ROOT_PATH = Path(__file__).parents[2]
ICON = ROOT_PATH / "ICON.png"
HELP_DATA = Path(__file__).parent / "help.json"


async def _load_help() -> dict[str, PluginHelp]:
    async with aiofiles.open(HELP_DATA, "rb") as file:
        return json.loads(await file.read())


async def get_help() -> bytes:
    help_image = await get_new_help(
        plugin_name="XHSAnalyse",
        plugin_info={f"v{XHSAnalyse_version}": ""},
        plugin_icon=Image.open(ICON),
        plugin_help=await _load_help(),
        plugin_prefix=get_plugin_available_prefix("XHSAnalyse"),
        help_mode="dark",
        banner_sub_text="小红书笔记与媒体解析",
        enable_cache=True,
    )
    return help_image if isinstance(help_image, bytes) else help_image.encode()

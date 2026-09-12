"""GsCoreXHS 帮助入口。"""

from PIL import Image

from gsuid_core.sv import SV, get_plugin_available_prefix
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.help.utils import register_help

from .get_help import ICON, get_help

sv_help = SV("小红书帮助")


@sv_help.on_fullmatch(("帮助", "help"), block=True)
async def send_help(bot: Bot, _ev: Event) -> None:
    await bot.send(await get_help())


register_help(
    "GsCoreXHS",
    f"{get_plugin_available_prefix('GsCoreXHS')}帮助",
    Image.open(ICON),
)

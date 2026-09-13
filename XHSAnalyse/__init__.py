"""XHSAnalyse 插件入口。"""

from gsuid_core.sv import Plugins, config_plugins

plugin = Plugins(
    name="XHSAnalyse",
    force_prefix=["rn"],
    allow_empty_prefix=False,
    alias=["小红书"],
)

# 旧版本曾把 allow_empty_prefix 写成 True，框架会用持久化值覆盖代码默认值并生成重复触发器。
if "XHSAnalyse" in config_plugins:
    plugin.set(
        pm=6,
        force_prefix=["rn"],
        allow_empty_prefix=False,
        alias=["小红书"],
    )

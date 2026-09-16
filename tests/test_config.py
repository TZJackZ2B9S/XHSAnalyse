from XHSAnalyse.xhs_config.xhs_config import _render_scale
from XHSAnalyse.xhs_config.config_default import CONFIG_DEFAULT


def test_card_and_proxy_settings_are_exposed_in_gscore_config() -> None:
    assert CONFIG_DEFAULT["renderCard"].title == "是否开启卡片渲染"
    assert CONFIG_DEFAULT["renderScale"].title == "卡片渲染精度"
    assert CONFIG_DEFAULT["renderScale"].options == []
    assert CONFIG_DEFAULT["renderScale"].max_value == 500
    assert "renderCompression" not in CONFIG_DEFAULT
    assert CONFIG_DEFAULT["proxy"].title == "代理服务器地址"
    assert CONFIG_DEFAULT["proxy"].data == ""
    assert CONFIG_DEFAULT["imageSendType"].title == "图片发送方式"
    assert CONFIG_DEFAULT["imageSendType"].data == "framework"
    assert CONFIG_DEFAULT["imageSendType"].options == ["framework", "file"]


def test_render_scale_is_limited_to_100_through_500_percent() -> None:
    assert _render_scale(50) == 1.0
    assert _render_scale(100) == 1.0
    assert _render_scale(500) == 5.0
    assert _render_scale(600) == 5.0

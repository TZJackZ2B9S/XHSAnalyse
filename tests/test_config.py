from XHSAnalyse.xhs_config.config_default import CONFIG_DEFAULT


def test_card_and_proxy_settings_are_exposed_in_gscore_config() -> None:
    assert CONFIG_DEFAULT["renderCard"].title == "是否开启卡片渲染"
    assert CONFIG_DEFAULT["renderScale"].title == "卡片渲染精度"
    assert CONFIG_DEFAULT["proxy"].title == "代理服务器地址"
    assert CONFIG_DEFAULT["proxy"].data == ""

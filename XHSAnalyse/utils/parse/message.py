"""从事件消息中提取可自动解析的链接文本。"""

from gsuid_core.models import Event


def _flatten_message_data(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(
            part
            for key, item in value.items()
            for part in (_flatten_message_data(key), _flatten_message_data(item))
            if part
        )
    if isinstance(value, (list, tuple)):
        return " ".join(part for item in value for part in (_flatten_message_data(item),) if part)
    return ""


# 适配器把平台卡片原样上报时用的段名：OneBot 系多为 json/xml，
# QQ 官方与部分适配器用 card/share/markdown。core 的 msg_process 对未知段
# 只做透传（原样留在 event.content），所以这里必须自己认这些名字。
_LINK_SEGMENT_TYPES = frozenset({"text", "json", "xml", "card", "share", "markdown"})


def event_link_text(event: Event) -> str:
    """提取普通文本和分享卡片文本，忽略合并转发节点。

    卡片段（QQ 结构化分享、小程序）里链接藏在嵌套的 ``jumpUrl`` 等字段，
    这里把 data 摊平成文本，交给 ``extract_urls`` 统一识别。
    """

    parts = [event.raw_text, event.text]
    for message in event.content:
        if message.type not in _LINK_SEGMENT_TYPES:
            continue
        data_text = _flatten_message_data(message.data)
        if data_text:
            parts.append(data_text)
    return " ".join(part.strip() for part in parts if part and part.strip())

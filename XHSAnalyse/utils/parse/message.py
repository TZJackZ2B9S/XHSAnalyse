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


def event_link_text(event: Event) -> str:
    """提取普通文本和分享卡片文本，忽略合并转发节点。"""

    parts = [event.raw_text, event.text]
    for message in event.content:
        if message.type not in {"text", "json", "xml", "markdown", "share"}:
            continue
        data_text = _flatten_message_data(message.data)
        if data_text:
            parts.append(data_text)
    return " ".join(part.strip() for part in parts if part and part.strip())

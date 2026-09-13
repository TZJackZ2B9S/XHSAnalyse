from gsuid_core.models import Event, Message
from XHSAnalyse.utils.parse.message import event_link_text


def test_auto_detection_ignores_links_inside_forwarded_nodes() -> None:
    event = Event(
        content=[
            Message(
                type="node",
                data=[{"type": "text", "data": "转发内容 https://xhslink.cn/o/inside"}],
            )
        ]
    )

    assert event_link_text(event) == ""


def test_auto_detection_keeps_direct_text_and_ignores_forwarded_nodes() -> None:
    event = Event(
        content=[
            Message(type="text", data="请解析 https://xhslink.cn/o/direct"),
            Message(type="node", data=[{"type": "text", "data": "https://xhslink.cn/o/inside"}]),
        ]
    )

    assert event_link_text(event) == "请解析 https://xhslink.cn/o/direct"

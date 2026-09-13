import json
from pathlib import Path


def test_help_examples_do_not_duplicate_plugin_prefix() -> None:
    help_path = Path(__file__).parents[1] / "XHSAnalyse" / "xhs_help" / "help.json"
    help_data = json.loads(help_path.read_text(encoding="utf-8"))
    rendered = ["rn" + item["eg"] for category in help_data.values() for item in category["data"]]

    assert all("rnrn" not in example for example in rendered)
    assert "rn https://xhslink.cn/o/xxxx" in rendered
    assert "rn帮助" in rendered

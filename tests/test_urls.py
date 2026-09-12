from XHSAnalyse.utils.parse.urls import explore_url, extract_urls, is_short_link, extract_note_id


def test_extract_urls_decodes_escapes_and_deduplicates() -> None:
    message = (
        "看看 https://xhslink.com/a1b2c3 和 "
        "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=abc\\u0026xsec_source=pc"
    )
    urls = extract_urls(message)
    assert urls == (
        "https://xhslink.com/a1b2c3",
        "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=abc&xsec_source=pc",
    )


def test_extract_urls_strips_chinese_punctuation() -> None:
    urls = extract_urls("https://xhslink.com/abc，请解析")
    assert urls == ("https://xhslink.com/abc",)


def test_note_id_and_short_link() -> None:
    url = "https://www.xiaohongshu.com/discovery/item/0123456789abcdef01234567"
    assert extract_note_id(url) == "0123456789abcdef01234567"
    assert is_short_link("https://xhslink.cn/abc")
    assert not is_short_link("https://xiaohongshu.com/explore/0123456789abcdef01234567")


def test_explore_url_replaces_path_and_keeps_origin() -> None:
    url = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=token"
    assert explore_url(url, "abcdef0123456789abcdef01") == (
        "https://www.xiaohongshu.com/explore/abcdef0123456789abcdef01?xsec_token=token"
    )

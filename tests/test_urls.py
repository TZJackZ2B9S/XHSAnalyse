from XHSAnalyse.utils.parse.urls import (
    explore_url,
    extract_urls,
    is_note_link,
    is_short_link,
    extract_note_id,
)


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


def test_extract_urls_supports_prefix_without_space() -> None:
    urls = extract_urls("xhshttps://xhslink.cn/abc")
    assert urls == ("https://xhslink.cn/abc",)


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


def test_is_note_link_accepts_note_ids_and_share_links() -> None:
    assert is_note_link("https://www.xiaohongshu.com/explore/0123456789abcdef01234567")
    assert is_note_link("https://www.xiaohongshu.com/discovery/item/0123456789abcdef01234567")
    assert is_note_link("https://xhslink.cn/o/AbCdEf")
    assert is_note_link("https://xhslink.com/a/xyz")


def test_is_note_link_rejects_non_note_pages_on_same_host() -> None:
    """主页、搜索页、活动页都在 xiaohongshu.com 下，自动解析必须跳过它们。"""

    assert not is_note_link("https://www.xiaohongshu.com/")
    assert not is_note_link("https://www.xiaohongshu.com/explore")
    assert not is_note_link("https://www.xiaohongshu.com/user/profile/5f1a2b3c000000000101d2e3")
    assert not is_note_link("https://www.xiaohongshu.com/search_result?keyword=abc")


def test_extract_note_id_only_matches_url_path_not_query() -> None:
    """query 里的 /explore/<id> 片段（登录跳转、主页参数）不算笔记 ID。"""

    assert extract_note_id("https://www.xiaohongshu.com/login?redirectPath=/explore/6abcb8ce000000001c01cbdc") == ""
    assert extract_note_id("https://www.xiaohongshu.com/user/profile/abc?note=/explore/6abcb8ce000000001c01cbdc") == ""
    with_query = "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc?x=1"
    assert extract_note_id(with_query) == "6abcb8ce000000001c01cbdc"


def test_is_note_link_rejects_url_carrying_note_id_only_in_query() -> None:
    """这类链接若被判为笔记，会被拼成畸形地址并在群里报错。"""

    assert not is_note_link("https://www.xiaohongshu.com/user/profile/abc?note=/explore/6abcb8ce000000001c01cbdc")
    assert not is_note_link("https://www.xiaohongshu.com/login?redirectPath=/explore/6abcb8ce000000001c01cbdc")


def test_is_note_link_accepts_profile_prefixed_share_form() -> None:
    """官方分享形态之一：作者主页前缀 + 作品 ID，不能误杀成非笔记。"""

    url = (
        "https://www.xiaohongshu.com/user/profile/5f1a2b3c000000000101d2e3/"
        "6abcb8ce000000001c01cbdc?xsec_token=AB=&xsec_source=pc_user"
    )
    assert is_note_link(url)
    assert extract_note_id(url) == "6abcb8ce000000001c01cbdc"


def test_explore_url_rewrites_profile_prefixed_form_without_duplicating_path() -> None:
    """主页前缀形态改写成 explore 地址时必须截断前缀，否则拼出畸形路径。"""

    url = "https://www.xiaohongshu.com/user/profile/5f1a2b3c000000000101d2e3/6abcb8ce000000001c01cbdc?xsec_token=AB="
    assert explore_url(url, "6abcb8ce000000001c01cbdc") == (
        "https://www.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc?xsec_token=AB="
    )


def test_extract_urls_handles_international_and_mobile_hosts() -> None:
    assert extract_urls("https://www.rednote.com/explore/6abcb8ce000000001c01cbdc") != ()
    assert extract_urls("https://m.xiaohongshu.com/explore/6abcb8ce000000001c01cbdc") != ()

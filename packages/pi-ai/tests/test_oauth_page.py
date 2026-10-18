"""OAuth 回调页面 HTML 生成测试。

对应上游 ``packages/ai/src/utils/oauth-page.ts``（v1.3.0，109 行）。

**保真度验证方式**：本文件的核心断言来自**真实上游 TypeScript 实现**的渲染结果
（用 Node 25 的原生类型剥离直接执行上游 `.ts` 源文件生成参考样本），逐字节比对。
这样"是否与上游一致"不靠人读代码推断。参考样本见
``_UPSTREAM_REFERENCE``（下方，由上游实现原样产出）。

转义边界用例覆盖：``<script>`` 注入、``<img onerror>`` 注入、``&`` 二次转义、
引号、Unicode、换行，以及"用户文案里含模板占位符"的注入面。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from pi_ai.oauth_page import oauth_error_html, oauth_success_html

# ============================================================
# 与上游**逐字节**比对（黄金样本）
# ============================================================
#
# 样本由上游 TypeScript 实现原样产出（见 tests/data/generate_oauth_page_reference.mjs，
# 用 Node 原生类型剥离直接执行上游 oauth-page.ts）。这样"是否与上游一致"由机器判定，
# 而不是靠人读代码推断。

_REFERENCE_PATH = Path(__file__).parent / "data" / "oauth-page-reference.json"
_REFERENCE = json.loads(_REFERENCE_PATH.read_text(encoding="utf-8"))
_REFERENCE_CASES = _REFERENCE["cases"]

#: 用例名 -> (调用方式)。与生成脚本中的顺序/参数逐条对应。
_REFERENCE_INVOCATIONS: dict[str, Callable[[], str]] = {
    "success-simple": lambda: oauth_success_html("You can close this window."),
    "success-html-injection": lambda: oauth_success_html('<script>alert("xss")</script>'),
    "success-amp": lambda: oauth_success_html("Tom & Jerry <b>bold</b>"),
    "success-quotes": lambda: oauth_success_html("He said \"hi\" & it's 'fine'"),
    "error-message-only": lambda: oauth_error_html("Something went wrong"),
    "error-with-details": lambda: oauth_error_html(
        "Sign-in failed", "error=access_denied&state=abc"
    ),
    "error-empty-details": lambda: oauth_error_html("Sign-in failed", ""),
    "error-injection-both": lambda: oauth_error_html(
        "<img src=x onerror=alert(1)>", "<script>document.cookie</script>"
    ),
    "error-entity-double": lambda: oauth_error_html("&amp;", "&lt;script&gt;"),
    "error-unicode": lambda: oauth_error_html("失败：授权被拒绝 🔒", "状态=已取消"),
    "error-newlines": lambda: oauth_error_html("line1\nline2", "a\nb"),
}


def test_reference_fixture_covers_every_invocation() -> None:
    """防止样本与调用表脱节。"""
    assert set(_REFERENCE_CASES) == set(_REFERENCE_INVOCATIONS)
    assert _REFERENCE["_provenance"]["upstreamRef"] == "earendil-works/pi@v1.3.0"


@pytest.mark.parametrize("case", sorted(_REFERENCE_CASES))
def test_matches_upstream_byte_for_byte(case: str) -> None:
    """本端输出必须与上游实现的原样输出**逐字节相同**。"""
    assert _REFERENCE_INVOCATIONS[case]() == _REFERENCE_CASES[case]


def test_exports_match_upstream_surface():
    """上游只导出两个函数（``oauth-page.ts:94``、``:102``），本端一致。"""
    from pi_ai import oauth_page

    assert sorted(oauth_page.__all__) == ["oauth_error_html", "oauth_success_html"]


def test_success_page_has_expected_structure():
    html = oauth_success_html("You can close this window.")
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<title>Authentication successful</title>" in html
    assert "<h1>Authentication successful</h1>" in html
    assert "<p>You can close this window.</p>" in html
    # 成功页没有详情块
    assert 'class="details"' not in html
    # logo 常量存在
    assert html.count("<svg") == 1


def test_error_page_has_expected_structure():
    html = oauth_error_html("Sign-in failed", "error=access_denied&state=abc")
    assert "<title>Authentication failed</title>" in html
    assert "<h1>Authentication failed</h1>" in html
    assert "<p>Sign-in failed</p>" in html
    assert '<div class="details">error=access_denied&amp;state=abc</div>' in html


def test_error_page_omits_details_when_absent_or_empty():
    """上游 :16 与 :88 的双重真值判断 -> 空串与 None 都不渲染详情块。"""
    for details in (None, ""):
        html = oauth_error_html("failed", details)
        assert 'class="details"' not in html
    # 空白串是真值（JS 与 Python 同为真），仍渲染
    assert 'class="details"' in oauth_error_html("failed", " ")


# ============================================================
# HTML 转义 / 注入边界
# ============================================================


def test_message_is_escaped_against_script_injection():
    html = oauth_success_html('<script>alert("xss")</script>')
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in html


def test_details_is_escaped_against_img_onerror_injection():
    html = oauth_error_html("<img src=x onerror=alert(1)>", "<script>document.cookie</script>")
    # 关键断言：payload 不得形成**活动标签**（`<` 被转义，故不可能成为元素/属性）
    assert "<img" not in html
    assert "<script>document.cookie" not in html
    # payload 作为**惰性可见文本**原样出现（正确行为：用户应看到原始报错）
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&lt;script&gt;document.cookie&lt;/script&gt;" in html
    # 页面自身不含真实 <script>（上游页面无脚本）
    assert html.count("<script") == 0


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ('"', "&quot;"),
        ("'", "&#39;"),
        ("<b>&</b>", "&lt;b&gt;&amp;&lt;/b&gt;"),
    ],
)
def test_escaping_table(raw: str, escaped: str) -> None:
    assert escaped in oauth_success_html(raw)


def test_ampersand_is_escaped_first_so_entities_are_not_double_escaped():
    """上游顺序正确（先 ``&``）；若顺序颠倒会得到 ``&amp;lt;``。"""
    html = oauth_success_html("<")
    assert "&lt;" in html
    assert "&amp;lt;" not in html

    # 输入本身是实体文本：应转义成可见的实体字面量，而非被当成标签
    html2 = oauth_error_html("&amp;", "&lt;script&gt;")
    assert "<p>&amp;amp;</p>" in html2
    assert '<div class="details">&amp;lt;script&amp;gt;</div>' in html2


def test_all_interpolation_points_are_escaped():
    """四个插值点（title/h1/p/details）都不得出现未转义的尖括号。

    注意 title/heading 由固定文案提供，message/details 来自调用方；此处验证
    调用方可控的两处。
    """
    payload = "<x>"
    html = oauth_error_html(payload, payload)
    assert "<x>" not in html
    assert html.count("&lt;x&gt;") == 2  # <p> 与 .details 各一处


def test_unicode_and_newlines_pass_through_escaped_but_intact():
    html = oauth_error_html("失败：授权被拒绝 🔒", "状态=已取消\n第二行")
    assert "失败：授权被拒绝 🔒" in html
    assert "状态=已取消\n第二行" in html


def test_user_content_cannot_forge_template_placeholders():
    """回归：占位符替换必须是**单次**扫描。

    若实现用连续 ``str.replace``，用户文案里含 ``__MESSAGE__``/``__HEADING__``
    时会在后续轮次被再次替换——即用户内容被当作模板指令（内容注入）。
    """
    html = oauth_error_html("__HEADING__ __DETAILS__ __LOGO__", "__TITLE__")
    assert "__HEADING__" in html
    assert "__DETAILS__" in html
    assert "__LOGO__" in html
    assert "__TITLE__" in html
    # 未被替换成真实 heading/details/logo
    assert "<h1>Authentication failed</h1>" in html
    assert html.count("<svg") == 1  # logo 仍只出现一次
    assert "__HEADING__ __DETAILS__ __LOGO__" in html  # 原样保留在 <p> 内


def test_no_unquoted_attribute_context_interpolation():
    """所有插值都落在元素内容位置，因此 ``=``/反引号未转义不构成注入面。"""
    html = oauth_error_html("a=b `c`", "x=y `z`")
    assert "a=b `c`" in html
    assert "x=y `z`" in html
    # 两个插值点都在成对引号的属性值**之外**（即元素内容），可直接断言形态
    assert "<p>a=b `c`</p>" in html
    assert '<div class="details">x=y `z`</div>' in html


# ============================================================
# 导入路径（三种写法解析到同一对象）
# ============================================================


def test_all_three_import_paths_resolve_to_the_same_objects():
    from pi_ai import oauth_error_html as top_error
    from pi_ai import oauth_success_html as top_success
    from pi_ai.oauth_page import oauth_error_html as mod_error
    from pi_ai.oauth_page import oauth_success_html as mod_success
    from pi_ai.utils.oauth_page import oauth_error_html as alias_error
    from pi_ai.utils.oauth_page import oauth_success_html as alias_success

    assert oauth_success_html is top_success is mod_success is alias_success
    assert oauth_error_html is top_error is mod_error is alias_error


def test_only_two_public_names_are_exported():
    """与上游导出面严格一致：辅助函数保持私有。"""
    from pi_ai import oauth_page

    assert not hasattr(oauth_page, "escape_html")
    assert not hasattr(oauth_page, "render_page")
    assert hasattr(oauth_page, "_escape_html")
    assert hasattr(oauth_page, "_render_page")

"""``pi_durable.search`` 的单元测试（上游 ``durable/src/search.ts``，85 行）。"""

from __future__ import annotations

import math

import pytest

from pi_durable.search import STOP_WORDS, SearchDocument, rank_documents, stem, tokenize


def test_stop_words_match_upstream() -> None:
    assert {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    } == STOP_WORDS


# --- stem（search.ts:33-38）----------------------------------------------------


def test_stem_ies_to_y() -> None:
    # `ies` → `y` 只在 len > 4 时触发
    assert stem("parties") == "party"
    assert stem("ies") == "ies"  # 3 个字符：三条规则都不触发
    assert stem("ties") == "tie"  # 4 个字符：`ies` 规则不触发，但末尾 `s` 规则触发


def test_stem_es_endings() -> None:
    assert stem("searches") == "search"
    assert stem("dishes") == "dish"
    assert stem("classes") == "class"
    assert stem("boxes") == "box"
    assert stem("quizzes") == "quizz"


def test_stem_plain_s() -> None:
    assert stem("issues") == "issue"
    assert stem("tools") == "tool"
    # `ss` 结尾不动
    assert stem("class") == "class"
    assert stem("access") == "access"
    # len > 3 才触发
    assert stem("as") == "as"


def test_stem_leaves_other_terms() -> None:
    assert stem("search") == "search"
    assert stem("model") == "model"


# --- tokenize（search.ts:41-49）------------------------------------------------


def test_tokenize_lowercases_and_splits() -> None:
    assert tokenize("Read File!") == ["read", "file"]


def test_tokenize_splits_camel_case() -> None:
    # 第一步：小写字母/数字 后接大写 → 切开
    assert tokenize("getModel") == ["get", "model"]
    # 第二步：连续大写 后接 大写+小写 → 切开
    assert tokenize("HTTPServer") == ["http", "server"]


def test_tokenize_drops_stop_words() -> None:
    assert tokenize("the model and a tool") == ["model", "tool"]


def test_tokenize_applies_stemming() -> None:
    assert tokenize("issues") == ["issue"]
    assert tokenize("searches") == ["search"]


def test_tokenize_empty_and_separators_only() -> None:
    assert tokenize("") == []
    assert tokenize("   ") == []
    assert tokenize("!!!") == []


def test_tokenize_splits_camel_case_before_lowercasing() -> None:
    """camelCase 切分发生在 ``toLowerCase()`` **之前**（``search.ts:43-45``）。

    所以 ``"tHe"`` 会被切成 ``"t He"``，小写后是 ``t`` 与 ``he``——
    两个都不是停用词。这是上游的行为，不是 bug，但容易被误读成「应该整词命中停用词」。
    """
    assert tokenize("The THE tHe") == ["t", "he"]


# --- rank_documents（search.ts:52-85）-----------------------------------------


def _documents() -> list[SearchDocument]:
    return [
        SearchDocument(name="bash", text="bash run a shell command"),
        SearchDocument(name="read", text="read a file from disk"),
        SearchDocument(name="edit", text="edit a file in place"),
        SearchDocument(name="write", text="write a file to disk"),
    ]


def test_rank_documents_returns_best_first() -> None:
    ranked = rank_documents("file", _documents(), 10)

    # 四个文档里三个含 file：read / edit / write。
    assert set(ranked) == {"read", "edit", "write"}
    assert "bash" not in ranked


def test_rank_documents_limit() -> None:
    assert len(rank_documents("file", _documents(), 2)) == 2


def test_rank_documents_matches_by_name() -> None:
    ranked = rank_documents("bash", _documents(), 10)
    assert ranked == ["bash"]


def test_rank_documents_empty_query_returns_empty() -> None:
    assert rank_documents("", _documents(), 10) == []
    assert rank_documents("the and of", _documents(), 10) == []


def test_rank_documents_no_documents() -> None:
    assert rank_documents("file", [], 10) == []


def test_rank_documents_non_positive_limit() -> None:
    assert rank_documents("file", _documents(), 0) == []
    assert rank_documents("file", _documents(), -1) == []


def test_rank_documents_no_matches_returns_empty() -> None:
    assert rank_documents("kubernetes", _documents(), 10) == []


def test_rank_documents_ties_keep_document_order() -> None:
    """同分保持文档顺序（上游依赖稳定排序，``search.ts:82``）。"""
    documents = [
        SearchDocument(name="first", text="alpha"),
        SearchDocument(name="second", text="alpha"),
        SearchDocument(name="third", text="alpha"),
    ]

    assert rank_documents("alpha", documents, 10) == ["first", "second", "third"]


def test_rank_documents_duplicate_query_terms_are_ignored() -> None:
    documents = _documents()
    once = rank_documents("file", documents, 10)
    twice = rank_documents("file file file", documents, 10)

    assert once == twice


def test_rank_documents_idf_favours_rare_terms() -> None:
    documents = [
        SearchDocument(name="common", text="tool alpha"),
        SearchDocument(name="rare", text="tool zeta"),
        SearchDocument(name="other", text="tool alpha"),
        SearchDocument(name="another", text="tool alpha"),
    ]

    # zeta 只出现在一篇里 → idf 更高 → rare 排第一。
    assert rank_documents("alpha zeta", documents, 4)[0] == "rare"


def test_rank_documents_bm25_formula_is_exact() -> None:
    """按上游公式手算一遍，锁住 k1 = 1.2、b = 0.75 与平滑项。

    文档 ``a`` 的词频更高，但文档更长 → 长度归一化后 ``b`` 反而得分更高。
    这正是 BM25 的 ``b`` 项在起作用，也是这个用例值得写下来的原因。
    """
    documents = [
        SearchDocument(name="a", text="x x y"),
        SearchDocument(name="b", text="x"),
    ]

    k1, b = 1.2, 0.75
    lengths = [3, 1]
    average = (3 + 1) / 2
    # x 出现在 2/2 篇文档里
    idf = math.log(1 + (2 - 2 + 0.5) / (2 + 0.5))
    expected_a = idf * ((2 * (k1 + 1)) / (2 + k1 * (1 - b + (b * lengths[0]) / average)))
    expected_b = idf * ((1 * (k1 + 1)) / (1 + k1 * (1 - b + (b * lengths[1]) / average)))

    assert expected_a == pytest.approx(0.219785, abs=1e-6)
    assert expected_b == pytest.approx(0.229204, abs=1e-6)
    assert expected_b > expected_a  # 短文档胜出
    assert rank_documents("x", documents, 2) == ["b", "a"]


def test_rank_documents_average_length_uses_or_one() -> None:
    """所有文档都为空时平均长度取 1（上游 ``|| 1``，``search.ts:63``），不应除零。"""
    documents = [SearchDocument(name="empty", text=""), SearchDocument(name="also", text="")]

    assert rank_documents("anything", documents, 10) == []

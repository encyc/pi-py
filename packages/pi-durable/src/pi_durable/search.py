"""BM25 工具检索 —— 移植 ``durable/src/search.ts``（85 行）。

上游注释写明它是「coding agent 的 tool search 排名的一份拷贝，durable 自带
自己需要的那部分」（``search.ts:1-4``）。两个消费者：

* ``tool-search/tool-search.ts:9`` 的 ``rankDocuments``（``tool_search`` 工具）
* ``codemode/codemode.ts`` 的 ``searchTools()``（尚未移植）

算法是 Okapi BM25，``k1 = 1.2``、``b = 0.75``（``search.ts:53-54``），
返回按分数降序的**文档名**；同分保持文档顺序（上游依赖 JS ``Array.sort``
自 ES2019 起稳定；Python ``sorted`` 同样稳定，见 :func:`rank_documents`）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "STOP_WORDS",
    "SearchDocument",
    "rank_documents",
    "stem",
    "tokenize",
]


@dataclass(frozen=True, slots=True)
class SearchDocument:
    """一篇可检索文档：``name`` 是返回的标识，``text`` 参与打分（``search.ts:6``）。"""

    name: str
    text: str


#: 上游 ``STOP_WORDS``（``search.ts:8-30``），逐词一致。
STOP_WORDS: frozenset[str] = frozenset(
    {
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
    }
)

# 上游两个 replace 里的正则（``search.ts:43-44``）。Python 的 re 与 JS 在这里
# 语义一致：`[a-z0-9]` / `[A-Z]` 都是 ASCII 类，不做 Unicode 折叠。
_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# 上游 `/(ches|shes|sses|xes|zes)$/`（``search.ts:35``）。
_PLURAL_ES = re.compile(r"(ches|shes|sses|xes|zes)$")


def stem(term: str) -> str:
    """朴素单数化，让 ``issues`` 命中 ``issue``、``searches`` 命中 ``search``。

    逐条对应上游 ``stem()``（``search.ts:33-38``）：
    ``ies`` → ``y``；``ches|shes|sses|xes|zes`` → 去掉 ``es``；
    其余以 ``s`` 结尾且不是 ``ss`` → 去掉 ``s``。长度门槛（``> 4`` / ``> 3``）
    是**字符数**，在 Python 里同样是 ``len(term)``。
    """
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 4 and _PLURAL_ES.search(term):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    """切词：小写、在 camelCase 边界与非字母数字处切分、去停用词、单数化。

    对应上游 ``tokenize()``（``search.ts:41-49``）。两步 camelCase 切分不可省：
    第一步 ``aB`` → ``a B``（``getModel`` → ``get Model``），
    第二步 ``ABc`` → ``A Bc``（``HTTPServer`` → ``HTTP Server``）。

    上游用 ``split`` 后 ``filter(term => term.length > 0)`` 丢掉空串；
    Python 的 ``re.split`` 同样会产出空串（首尾或连续分隔符），这里显式过滤。
    """
    spaced = _CAMEL_LOWER_UPPER.sub(r"\1 \2", text)
    spaced = _CAMEL_ACRONYM.sub(r"\1 \2", spaced)
    return [
        stem(term)
        for term in _NON_ALNUM.split(spaced.lower())
        if len(term) > 0 and term not in STOP_WORDS
    ]


def rank_documents(
    query: str,
    documents: Sequence[SearchDocument],
    limit: int,
) -> list[str]:
    """Okapi BM25 排名：最匹配的文档名，最好的在前（``search.ts:52-85``）。

    ``k1 = 1.2``、``b = 0.75``。查询词去重后参与打分；
    文档平均长度按 token 数算，为 0 时取 1（上游 ``|| 1``，``search.ts:63``）。

    分数为 0 的文档不进结果（上游 ``if (score > 0)``，``search.ts:79``）。
    ``limit <= 0``、查询无有效词、或没有文档时返回空列表。
    """
    k1 = 1.2
    b = 0.75

    # 上游用 `[...new Set(...)]` 去重且保留首次出现顺序（``search.ts:55``）；
    # `dict.fromkeys` 在 Python 里同样保序去重。
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms or not documents or limit <= 0:
        return []

    term_counts: list[dict[str, int]] = []
    for document in documents:
        counts: dict[str, int] = {}
        for term in tokenize(document.text):
            counts[term] = counts.get(term, 0) + 1
        term_counts.append(counts)

    lengths = [sum(counts.values()) for counts in term_counts]
    average_length = sum(lengths) / len(documents) or 1

    # 文档频率：含该词的文档篇数（``search.ts:66``）。
    frequencies = {term: sum(1 for counts in term_counts if term in counts) for term in query_terms}
    idf = {
        term: math.log(1 + (len(documents) - frequency + 0.5) / (frequency + 0.5))
        for term, frequency in frequencies.items()
    }

    scored: list[tuple[int, str, float]] = []
    for index, document in enumerate(documents):
        score = 0.0
        for term in query_terms:
            count = term_counts[index].get(term)
            if not count:
                continue
            norm = k1 * (1 - b + (b * lengths[index]) / average_length)
            score += idf[term] * ((count * (k1 + 1)) / (count + norm))
        if score > 0:
            # 带上原始下标，让同分保持文档顺序（上游依赖稳定排序，``search.ts:82``）。
            scored.append((index, document.name, score))

    scored.sort(key=lambda entry: -entry[2])
    return [name for _, name, _ in scored[:limit]]

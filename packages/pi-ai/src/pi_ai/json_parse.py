"""工具参数 JSON 解析。

对应上游 ``utils/json-parse.ts``。两条路径，语义**不同**，不可混用：

- :func:`parse_streaming_json` —— 流式增量期间使用。容忍不完整 JSON，
  永远返回 dict（失败退化为 ``{}``）。对应上游 ``parseStreamingJson``。
- :func:`parse_tool_call_arguments` —— 工具调用**收尾**时使用。不保留部分解析
  前缀：模型给出的完整参数若不是合法 JSON 对象，``arguments`` 置 ``{}`` 并返回
  ``arguments_error``，由工具参数校验报回模型（上游 v1.3.0，issue #10897）。

上游 v0.85.1 在收尾处也调用 ``parseStreamingJson``，导致"参数 JSON 末尾损坏时
只用解析成功的前缀字段静默执行工具"（#10897）。本模块区分两条路径以消除该问题。

**为什么自带 repair_json 而不用 json-repair 库**：上游的 ``repairJson`` 只做
"字符串内转义修复"（补转义裸控制字符、给非法转义字符前的反斜杠加倍），**不做
结构补全**。Python 的 ``json-repair`` 是通用结构修复器，会在两个上游明确要求
**报错**的场景上"贴心地"修好：

============  ==================================  ==========================
输入          上游 repairJson + parseToolCallArgs   json-repair
============  ==================================  ==========================
``time":.5``  不修复 → argumentsError                修复为 ``0.5`` → 静默执行
切断的 JSON   不补全 → argumentsError                补全 → 静默执行
============  ==================================  ==========================

两者都恰好落进 issue #10897 想消灭的"静默执行"里，故收尾路径必须用上游语义
（:func:`repair_json`）。``json-repair`` 仅保留给**流式路径**做兜底，这是本端
``PORTING.md`` 已登记的既有偏离。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "repair_json",
    "parse_json_with_repair",
    "parse_streaming_json",
    "parse_tool_call_arguments",
]

#: JSON 字符串里合法的单字符转义。
#: **注意不含 ``"u"``**：``\uXXXX`` 由下面的四位十六进制分支单独处理，把 ``u``
#: 放进本集合会让截断的 ``\u`` 被当成合法转义放行（上游 issue #10868 的成因）。
_VALID_JSON_ESCAPES = frozenset('"\\/bfnrt')

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _escape_control_character(char: str) -> str:
    """把字符串内的裸控制字符转成其转义形式。对应上游 ``escapeControlCharacter``。"""
    mapping = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    if char in mapping:
        return mapping[char]
    return f"\\u{ord(char):04x}"


def repair_json(text: str) -> str:
    """修复 JSON 字符串字面量中的畸形转义。对应上游 ``repairJson``。

    逐字符扫描，只处理**字符串内部**：

    - 裸控制字符（``0x00``–``0x1f``）→ 其转义形式；
    - ``\\`` 后跟合法单字符转义 → 原样保留；
    - ``\\u`` 后跟恰好四位十六进制 → 原样保留（消费四位）；
    - ``\\`` 后跟其他字符（含不完整的 ``\\u``）→ **反斜杠加倍**，使其成为字面量；
    - 字符串末尾的孤立 ``\\`` → 反斜杠加倍。

    修复后文本与输入不同时调用方才应重新解析（见 :func:`parse_json_with_repair`），
    因此本函数在"无从修复"的输入上必须**原样返回**——那是"模型发了畸形 JSON"的
    判定信号。
    """
    repaired: list[str] = []
    in_string = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            index += 1
            continue

        if char == "\\":
            next_char = text[index + 1] if index + 1 < length else None
            if next_char is None:
                repaired.append("\\\\")
                index += 1
                continue

            if next_char == "u":
                digits = text[index + 2 : index + 6]
                if len(digits) == 4 and all(d in _HEX_DIGITS for d in digits):
                    repaired.append(f"\\u{digits}")
                    index += 6
                    continue

            if next_char in _VALID_JSON_ESCAPES:
                repaired.append(f"\\{next_char}")
                index += 2
                continue

            repaired.append("\\\\")
            index += 1
            continue

        repaired.append(_escape_control_character(char) if ord(char) <= 0x1F else char)
        index += 1

    return "".join(repaired)


def parse_json_with_repair(text: str) -> Any:
    """严格优先、**仅转义修复**兜底的 JSON 解析。对应上游 ``parseJsonWithRepair``。

    与上游逐分支等价：

    1. ``json.loads`` 成功即返回；
    2. 失败则 :func:`repair_json`；**修复结果与原文不同**时才重新解析；
    3. 修复未改变文本 → 抛出原始解析错误。

    第 3 条是关键：它使"模型发来一段散文"或"JSON 被截断"不再被静默当成可执行参数。
    """
    try:
        return json.loads(text)
    except ValueError as error:
        repaired = repair_json(text)
        if repaired != text:
            return json.loads(repaired)
        raise error


def parse_streaming_json(text: str | None) -> dict[str, Any]:
    """容错解析**不完整**的 JSON。对应上游 ``parseStreamingJson``。

    永远返回 dict。分层回退：

    1. :func:`parse_json_with_repair`（严格 + 转义修复）
    2. ``json-repair``（结构补全）—— **本端偏离**：上游此处用 ``partial-json``。
       两者都能解析被截断的增量，但结果细节可能不同（例如截断的 ``\\u00``：
       上游给 ``{"text": "ab"}``，json-repair 给 ``{"text": "ab\\\\u00"}``）。
       该偏离已在 ``PORTING.md``「有意偏离」登记。
    3. ``{}``
    """
    if not text or not text.strip():
        return {}
    try:
        parsed = parse_json_with_repair(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    try:
        from json_repair import repair_json as structural_repair

        repaired = structural_repair(text, return_objects=True)
    except Exception:  # noqa: BLE001 - 修复器对任意畸形输入都必须不抛出
        return {}
    return repaired if isinstance(repaired, dict) else {}


def parse_tool_call_arguments(text: str | None) -> tuple[dict[str, Any], str | None]:
    """解析**已结束**工具调用的完整参数文本。

    对应上游 ``parseToolCallArguments``。返回 ``(arguments, arguments_error)``：

    - 合法 JSON 对象 → ``(obj, None)``
    - 空/纯空白 → ``({}, None)``（无参数工具调用）
    - 其余（散文、数组、标量、被截断、无法转义修复的畸形 JSON）
      → ``({}, "错误说明 + 模型原文")``

    与流式路径的关键差别：**绝不保留部分解析前缀，也绝不结构补全**。调用方应把
    ``arguments_error`` 写入 :attr:`~pi_ai.types.ToolCall.arguments_error`，使工具
    参数校验以"模型自己发的原文"失败，而不是拿空参数静默执行。
    """
    if not text or not text.strip():
        return {}, None
    try:
        parsed = parse_json_with_repair(text)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must be a JSON object")
        return parsed, None
    except Exception as error:  # noqa: BLE001 - 任何失败都必须转成 arguments_error
        return {}, f"{error}\n\nReceived arguments:\n{text}"

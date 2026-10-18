"""工具参数 JSON 解析测试。

用例逐条对应上游 ``test/json-parse.test.ts``（v1.3.0）：
- ``parseStreamingJson`` 的 ``\\u`` 转义处理（issue #10868）
- ``parseToolCallArguments`` 收尾语义（issue #10897）
"""

from __future__ import annotations

import pytest

from pi_ai.json_parse import (
    parse_json_with_repair,
    parse_streaming_json,
    parse_tool_call_arguments,
    repair_json,
)

# ============================================================
# parseStreamingJson（M-5：\u 转义不得丢字段）
# ============================================================


def test_streaming_keeps_invalid_u_escape_as_literal_text():
    """#10868：非法 ``\\u`` 转义按字面量保留，**不丢字段**。"""
    assert parse_streaming_json(r'{"text":"\url{x}"}') == {"text": "\\url{x}"}
    assert parse_streaming_json(r'{"path":"a.tex","content":"\usepackage{x}","mode":"w"}') == {
        "path": "a.tex",
        "content": "\\usepackage{x}",
        "mode": "w",
    }


def test_streaming_decodes_valid_u_escape():
    """合法 ``\\uXXXX`` 仍被解码，其后非法转义按字面量保留。"""
    assert parse_streaming_json(r'{"text":"caf\u00e9 \url"}') == {"text": "café \\url"}


def test_streaming_cut_off_u_escape_is_a_documented_deviation():
    """**已登记的偏离**：截断的 ``\\u`` 在流式路径的结果与上游不同。

    上游 ``{"text":"ab\\u00`` → ``{"text": "ab"}``（``partial-json`` 丢弃不完整
    转义序列）；本端用 ``json-repair`` 兜底，得到 ``{"text": "ab\\\\u00"}``。

    影响面很小：流式路径的结果只是给 UI 的中间态，**收尾值**由
    ``parse_tool_call_arguments`` 决定且已与上游逐字一致。若日后要求逐字对齐，
    需改为移植 ``partial-json`` 的部分解析语义。见 ``PORTING.md``「有意偏离」。
    """
    assert parse_streaming_json(r'{"text":"ab\u00') == {"text": "ab\\u00"}


def test_streaming_returns_empty_dict_for_blank_and_hopeless_input():
    assert parse_streaming_json("") == {}
    assert parse_streaming_json(None) == {}
    assert parse_streaming_json("   ") == {}


def test_streaming_falls_back_to_structural_repair():
    """流式路径仍容忍结构不完整（本端保留 json-repair 兜底）。"""
    assert parse_streaming_json('{"a": 1, "b": [1,2') == {"a": 1, "b": [1, 2]}


def test_streaming_returns_empty_dict_for_non_object():
    assert parse_streaming_json("[1,2]") == {}
    assert parse_streaming_json("123") == {}


# ============================================================
# repairJson（M-5 的核心：窄修复，不做结构补全）
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 非法单字符转义 -> 反斜杠加倍
        (r'"\url"', r'"\\url"'),
        # 合法转义原样保留
        (r'"a\nb"', r'"a\nb"'),
        (r'"a\\b"', r'"a\\b"'),
        # 合法 \uXXXX 原样保留
        (r'"caf\u00e9"', r'"caf\u00e9"'),
        # 不完整的 \u（不足 4 位十六进制）-> 反斜杠加倍
        (r'"a\u00"', r'"a\\u00"'),
        # 非十六进制 -> 反斜杠加倍
        (r'"a\uZZZZ"', r'"a\\uZZZZ"'),
        # 字符串末尾孤立反斜杠 -> 加倍
        ('"a\\', '"a\\\\'),
        # 字符串外的反斜杠不动
        (r"\url", r"\url"),
    ],
)
def test_repair_json_is_escape_only(raw: str, expected: str) -> None:
    assert repair_json(raw) == expected


def test_repair_json_escapes_raw_control_characters():
    assert repair_json('"a\nb"') == r'"a\nb"'
    assert repair_json('"a\tb"') == r'"a\tb"'
    assert repair_json('"a\x01b"') == r'"a\u0001b"'


def test_repair_json_is_identity_for_cut_off_structure():
    """结构不完整时 repairJson **原样返回**——这是"模型发了畸形 JSON"的判定信号。"""
    raw = '{"kind":"seek","clipId":"clip-fl'
    assert repair_json(raw) == raw


def test_parse_json_with_repair_raises_when_repair_is_a_no_op():
    with pytest.raises(ValueError):
        parse_json_with_repair('{"a":')


# ============================================================
# parseToolCallArguments（M-4：收尾不静默执行）
# ============================================================


def test_tool_call_arguments_parses_complete_json_and_repairs_escapes():
    assert parse_tool_call_arguments(r'{"kind":"seek","note":"\url"}') == (
        {"kind": "seek", "note": "\\url"},
        None,
    )


def test_tool_call_arguments_empty_text_means_no_arguments():
    assert parse_tool_call_arguments("") == ({}, None)
    assert parse_tool_call_arguments(None) == ({}, None)
    assert parse_tool_call_arguments("  ") == ({}, None)


def test_tool_call_arguments_does_not_keep_valid_prefix_of_malformed_json():
    """#10897 核心：不保留"解析成功的前缀字段"。"""
    raw = '{"action":"apply","actions":[{"kind":"seek","time":.5}]}'
    arguments, error = parse_tool_call_arguments(raw)
    assert arguments == {}
    assert error is not None
    assert "\n\nReceived arguments:\n" in error
    assert error.endswith(raw)


def test_tool_call_arguments_does_not_complete_cut_off_json():
    """#10897 核心：截断的 JSON 不得被补全执行。"""
    raw = '{"kind":"seek","clipId":"clip-fl'
    arguments, error = parse_tool_call_arguments(raw)
    assert arguments == {}
    assert error is not None
    assert error.endswith(raw)


def test_tool_call_arguments_rejects_non_object_json():
    assert parse_tool_call_arguments("[1,2]") == (
        {},
        "Tool arguments must be a JSON object\n\nReceived arguments:\n[1,2]",
    )
    assert parse_tool_call_arguments("null")[0] == {}
    assert parse_tool_call_arguments("123")[0] == {}
    assert parse_tool_call_arguments('"str"')[0] == {}


def test_tool_call_arguments_accepts_nested_object():
    raw = '{"action":"apply","actions":[{"kind":"seek","time":0.5}]}'
    assert parse_tool_call_arguments(raw) == (
        {"action": "apply", "actions": [{"kind": "seek", "time": 0.5}]},
        None,
    )

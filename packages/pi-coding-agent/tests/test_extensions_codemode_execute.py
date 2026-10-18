"""Tier-B④-2a-2 测试：`execute.ts` 47–439 的纯逻辑。

不跑沙箱、不起 worker：全部是「拿到值 → 转成模型能看的东西」。
"""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pi_ai import TextContent
from pi_coding_agent.extensions.codemode.execute import (
    ARGS_PREVIEW_CHARS,
    CHARS_PER_TOKEN,
    DEFAULT_MAX_OUTPUT_TOKENS,
    IMAGE_EXTENSIONS,
    MAX_CONCURRENT_MODEL_CALLS,
    MODEL_TYPES,
    check_classifier_context,
    check_images_context,
    create_limiter,
    describe_value,
    format_call_summary,
    format_error,
    format_output,
    is_store_entry_data,
    join_adjacent_text,
    preview_args,
    read_codemode_store,
    save_images,
    spill_output,
    text_of,
    to_model_info,
    to_model_type,
    to_provider,
    to_script_value,
    truncate_output,
    truncate_text,
    value_text,
    with_article,
)


@dataclass
class _Result:
    content: list[Any]
    structured_content: Any = None
    is_error: bool = False


@dataclass
class _Tool:
    name: str = "read"
    output_schema: dict[str, Any] | None = None


# ============================================================
# 预览与文本
# ============================================================


def test_truncate_text_keeps_the_ellipsis_inside_the_budget():
    assert truncate_text("abcdef", 10) == "abcdef"
    assert truncate_text("abcdefghij", 8) == "abcde..."
    long_text = truncate_text("x" * 500, ARGS_PREVIEW_CHARS)
    assert len(long_text) == ARGS_PREVIEW_CHARS
    assert long_text.endswith("...")


def test_preview_args_is_compact_json_and_empty_when_unserializable():
    assert preview_args(None) == ""
    assert preview_args({"a": 1}) == '{"a":1}'  # 紧凑，无空格


def test_text_of_joins_text_blocks_only():
    result = _Result(content=[TextContent(text="a"), TextContent(text="b")])
    assert text_of(result) == "a\nb"
    assert text_of(_Result(content=[])) == ""


# ============================================================
# 模型类型 / provider / 目录
# ============================================================


def test_to_model_type_accepts_only_the_three_known_types():
    assert {"chat", "image", "classifier"} == MODEL_TYPES
    for value in ("chat", "image", "classifier"):
        assert to_model_type(value) == value
    with pytest.raises(ValueError, match='Use "chat", "image", or "classifier"'):
        to_model_type("video")


def test_to_provider_rejects_non_strings():
    assert to_provider(None) is None
    assert to_provider("anthropic") == "anthropic"
    with pytest.raises(ValueError, match="provider must be a string"):
        to_provider(7)


def test_to_model_info_drops_headers_because_they_can_carry_credentials():
    info = to_model_info({"id": "m", "headers": {"Authorization": "secret"}})
    assert info == {"id": "m"}


def test_with_article():
    assert with_article("image") == "an image"
    assert with_article("classifier") == "a classifier"


# ============================================================
# describe_value
# ============================================================


def test_describe_value_covers_every_branch():
    assert describe_value(None) == "null"
    assert describe_value([]) == "an empty array"
    assert describe_value([1]) == "an array"
    assert describe_value({}) == "{}"
    assert describe_value({"prompt": 1}) == "{ prompt }"
    assert describe_value({str(i): i for i in range(8)}).endswith(", ... }")
    assert describe_value("s") == "a string"


# ============================================================
# 上下文校验
# ============================================================


def _good_classifier() -> dict[str, Any]:
    return {
        "state": {"a": 1},
        "questions": {
            "q": {"type": "bool", "instructions": "?", "criteria": {"true": "y", "false": "n"}}
        },
    }


def test_check_classifier_context_accepts_the_documented_shape():
    assert check_classifier_context(_good_classifier()) is not None


def test_check_classifier_context_names_the_expected_shape_and_docs():
    """错误信息要带期望形状与文档路径 —— 这正是它存在的意义。"""
    with pytest.raises(ValueError) as excinfo:
        check_classifier_context("nope")
    message = str(excinfo.value)
    assert "models.classify() expects a context object" in message
    assert "Expected context:" in message
    assert 'See "Classify" in' in message


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: c.update(state="x"), "context.state must be an object"),
        (lambda c: c.update(images="x"), "context.images must be an array"),
        (lambda c: c.update(images=[{"type": "text"}]), "must be an image block"),
        (lambda c: c.update(questions={}), "must map question IDs"),
        (lambda c: c.update(questions={"q": 1}), "must be a question object"),
        (
            lambda c: c.update(questions={"q": {"instructions": "?"}}),
            '.type must be "choice", "score", or "bool"',
        ),
    ],
)
def test_check_classifier_context_reports_each_problem(mutate: Any, expected: str):
    context = _good_classifier()
    mutate(context)
    with pytest.raises(ValueError, match=expected.replace("(", r"\(")):
        check_classifier_context(context)


def test_check_classifier_context_validates_each_question_kind():
    choice = {
        "state": {},
        "questions": {
            "q": {"type": "choice", "instructions": "?", "criteria": {"a": "A", "b": "B"}}
        },
    }
    assert check_classifier_context(choice) is not None
    score = {
        "state": {},
        "questions": {"q": {"type": "score", "instructions": "?", "criteria": ["low", "high"]}},
    }
    assert check_classifier_context(score) is not None


def test_check_images_context_matches_the_documented_shape():
    ok = {"input": [{"type": "text", "text": "a cat"}]}
    assert check_images_context(ok) is not None
    with pytest.raises(ValueError) as excinfo:
        check_images_context({"prompt": "a cat"})
    message = str(excinfo.value)
    assert "context.input must be a non-empty array" in message
    assert 'See "Generate images" in' in message
    with pytest.raises(ValueError, match="must be a text or image block"):
        check_images_context({"input": [{"type": "video"}]})


# ============================================================
# 限流器
# ============================================================


async def test_create_limiter_caps_concurrency_and_preserves_order():
    assert MAX_CONCURRENT_MODEL_CALLS == 4
    run = create_limiter(2)
    active = 0
    peak = 0
    finished: list[int] = []

    async def job(index: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        finished.append(index)
        return index

    results = await asyncio.gather(*(run(lambda i=i: job(i)) for i in range(5)))
    assert peak <= 2
    assert results == [0, 1, 2, 3, 4]
    assert finished == [0, 1, 2, 3, 4]


# ============================================================
# store
# ============================================================


def test_is_store_entry_data_requires_set_object_and_string_delete_list():
    assert is_store_entry_data({"set": {}, "delete": []})
    assert not is_store_entry_data(None)
    assert not is_store_entry_data({"set": {}, "delete": [1]})
    assert not is_store_entry_data({"set": []})


def _store_entry(set_: dict[str, Any], delete: list[str]) -> dict[str, Any]:
    return {
        "type": "custom",
        "customType": "codemode-store",
        "data": {"set": set_, "delete": delete},
    }


def test_read_codemode_store_applies_delete_before_set_per_entry():
    """每条先 delete 再 set（上游顺序）—— 同一分支上后写的覆盖先写的。"""
    branch = [
        _store_entry({"a": 1, "b": 2}, []),
        _store_entry({"a": 9}, ["b"]),
    ]
    assert read_codemode_store(branch) == {"a": 9}


def test_read_codemode_store_ignores_other_entries():
    branch = [
        {"type": "message", "id": "1"},
        {"type": "custom", "customType": "other", "data": {"set": {"x": 1}, "delete": []}},
        {"type": "custom", "customType": "codemode-store", "data": {"set": {}, "delete": []}},
    ]
    assert read_codemode_store(branch) == {}


# ============================================================
# 输出格式化
# ============================================================


def test_value_text_matches_the_script_text_helper():
    assert value_text("s") == "s"
    assert value_text({"a": 1}) == '{"a":1}'


def test_format_output_numbers_text_items_only_when_there_are_several():
    single = format_output([{"type": "text", "text": "one"}])
    assert [b.text for b in single] == ["one"]

    several = format_output([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])
    assert [b.text for b in several] == ["==> text 1/2 <==\none", "==> text 2/2 <==\ntwo"]


def test_format_output_collects_console_lines_into_one_trailing_block():
    items = format_output(
        [
            {"type": "text", "text": "out"},
            {"type": "text", "text": "log1", "console": True},
            {"type": "text", "text": "log2", "console": True},
        ]
    )
    assert [b.text for b in items] == ["out", "<console_output>\nlog1\nlog2\n</console_output>"]


def test_format_output_keeps_images_in_place():
    items = format_output(
        [{"type": "text", "text": "a"}, {"type": "image", "data": "aW1n", "mime_type": "image/png"}]
    )
    assert items[0].text == "a"
    assert items[1].type == "image"


def test_join_adjacent_text_puts_each_part_on_its_own_line():
    joined = join_adjacent_text([TextContent(text="a"), TextContent(text="b")])
    assert [b.text for b in joined] == ["a\nb"]
    # 已经以换行结尾就不重复加
    joined2 = join_adjacent_text([TextContent(text="a\n"), TextContent(text="b")])
    assert [b.text for b in joined2] == ["a\nb"]


def test_join_adjacent_text_splits_around_images():
    image = format_output([{"type": "image", "data": "aW1n", "mime_type": "image/png"}])[0]
    joined = join_adjacent_text([TextContent(text="a"), image, TextContent(text="b")])
    assert len(joined) == 3


# ============================================================
# 错误
# ============================================================


def test_format_call_summary_lists_calls_because_they_are_not_undone():
    assert format_call_summary([]) == "No tool calls were made."

    @dataclass
    class _Call:
        name: str
        status: str

    summary = format_call_summary([_Call("read", "ok")])
    assert "they are not undone" in summary
    assert "read (ok)" in summary


@pytest.mark.parametrize(
    ("kind", "prefix"),
    [
        ("script", "Error: boom"),
        ("timeout", "Script timed out: boom"),
        ("aborted", "Script aborted: boom"),
        ("sandbox", "Script sandbox failed: boom"),
    ],
)
def test_format_error_heads_each_kind(kind: str, prefix: str):
    text = format_error({"error": {"kind": kind, "message": "boom"}}, [])
    assert text.startswith(prefix)
    assert "No tool calls were made." in text


def test_format_error_prefers_the_stack_for_script_errors():
    text = format_error({"error": {"kind": "script", "message": "m", "stack": "STACK"}}, [])
    assert text.startswith("STACK")


# ============================================================
# 落盘与截断
# ============================================================


async def test_spill_output_writes_the_full_text():
    spilled = await spill_output("hello")
    assert "path" in spilled
    path = spilled["path"]
    try:
        assert await asyncio.to_thread(Path(path).read_text, encoding="utf-8") == "hello"
    finally:
        os.unlink(path)


async def test_save_images_labels_each_image_and_dedupes():
    data = base64.b64encode(b"png-bytes").decode()
    items = [
        TextContent(text="before"),
        format_output([{"type": "image", "data": data, "mime_type": "image/png"}])[0],
        format_output([{"type": "image", "data": data, "mime_type": "image/png"}])[0],
    ]
    out = await save_images(items)
    labels = [
        b.text for b in out if getattr(b, "type", None) == "text" and b.text.startswith("[Image")
    ]
    assert len(labels) == 2, labels  # 每张图前各一个标签
    assert labels[0] == labels[1]  # 同样的图只存一次 → 同一个路径
    assert "image/png" in labels[0]
    # 清理：两条标签指向同一个文件
    path = labels[0].split("saved to ")[1].split(" (")[0]
    os.unlink(path)


async def test_save_images_throws_for_an_unsupported_type():
    """上游对**没有扩展名**的类型是 ``throw``（try/catch 只包住写文件）。

    照抄上游语义：类型不支持就抛，而不是降级成一个标签。
    """
    image = format_output(
        [{"type": "image", "data": base64.b64encode(b"x").decode(), "mime_type": "image/tiff"}]
    )[0]
    with pytest.raises(ValueError, match="No file extension for image type image/tiff"):
        await save_images([image])
    # 但**写文件失败**只是标签里的一句说明（见下一条）
    assert IMAGE_EXTENSIONS.get("image/tiff") is None


async def test_truncate_output_keeps_head_and_tail_and_spills_the_full_text():
    text = "x" * (DEFAULT_MAX_OUTPUT_TOKENS * CHARS_PER_TOKEN * 2)
    result = await truncate_output([TextContent(text=text)], DEFAULT_MAX_OUTPUT_TOKENS)
    body = result["items"][0].text
    assert body.startswith("Warning: truncated output")
    assert "tokens truncated" in body
    assert "[Full output: " in body
    assert result["full_output_path"]
    os.unlink(result["full_output_path"])


async def test_truncate_output_passes_short_output_through():
    items = [TextContent(text="short")]
    assert (await truncate_output(items, DEFAULT_MAX_OUTPUT_TOKENS))["items"] == items


def test_image_extensions_cover_what_the_sandbox_accepts():
    assert IMAGE_EXTENSIONS == {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }


# ============================================================
# to_script_value
# ============================================================


def test_to_script_value_prefers_structured_content_for_schema_tools():
    tool = _Tool(output_schema={"type": "object"})
    result = _Result(content=[], structured_content={"n": 1})
    assert to_script_value(tool, result) == {"n": 1}


def test_to_script_value_returns_text_for_other_tools():
    assert to_script_value(_Tool(), _Result(content=[TextContent(text="hi")])) == "hi"


def test_to_script_value_raises_with_the_tool_error_text():
    """``is_error`` 落在结果对象上（本仓没有上游的 ``AgentToolCallOutcome`` 包装）。"""
    with pytest.raises(ValueError, match="boom"):
        to_script_value(_Tool(), _Result(content=[TextContent(text="boom")], is_error=True))
    with pytest.raises(ValueError, match='Tool "read" failed'):
        to_script_value(_Tool(), _Result(content=[], is_error=True))

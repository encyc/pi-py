"""``codemode`` 脚本执行的**纯逻辑部分**（Tier-B④-2a-2）。

对应上游 ``extensions/codemode/execute.ts`` 的 **47–439 行**：从脚本/工具拿到值，
转成模型能看的东西 —— 预览、校验、格式化、store 读取、图像落盘、输出截断。
**不含** ``executeCodemode``(440–579) 与 globals(580–772)（那些是 ④-2b，要碰沙箱与
``ctx.execute_tool``）。

上游文件头：*"Runs one codemode script in the sandbox. Split from tool.ts and loaded through
execute.lazy.ts so the sandbox runtime only loads when a script runs."*

**有意偏离**

* **`AgentToolCallOutcome` 无对应物**：上游的 outcome 是 ``{ result, isError }`` 两个字段；
  本仓 ``AgentToolResult`` **自带 ``is_error``**，没有外层包装类型。故
  :func:`to_script_value` 直接收结果对象读 ``is_error``（见其 docstring）。
* ``writeOutputFile(prefix, ext, data)`` → :func:`pi_coding_agent.output_files.save_output_file`
  （**共享模块**，MCP 用的是同一个上游函数）。
* ``formatSize`` → :func:`pi_coding_agent.truncate.format_size`（本仓早已有，行为等价已核对）。
* ``Buffer.from(data, "base64")`` → :func:`base64.b64decode`。
* ``Promise`` 的 ``Promise.all`` 保持顺序 → ``asyncio.gather`` 同样保序。
* ``MAX_CONCURRENT_CODEMODE_WORKERS`` / ``activeCodemodeWorkers`` 属 ④-2b（worker 配额），此处不声明。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import math
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from pi_ai import ImageContent, TextContent
from pi_codemode import (
    CodemodeSandbox,
    CodemodeTool,
    load_quickjs_wasm,
    parse_codemode_source,
)
from pi_codemode.declarations import render_tool_sample, to_codemode_identifier

from ...extensions.types import ToolDefinition, ToolNamespace
from ...output_files import save_output_file
from ...truncate import format_size
from ...usage_totals import combine_usage
from ..tool_search.tool import (
    DEFAULT_TOOL_SEARCH_LIMIT,
    Bm25Ranker,
    create_tool_search_document,
)
from .tool import (
    CODEMODE_DOCS_PATH,
    CODEMODE_STORE_ENTRY_TYPE,
    CodemodeNestedCall,
    CodemodeStoreEntryData,
    CodemodeToolDetails,
    CodemodeToolOptions,
    get_codemode_callable_tools,
    to_codemode_declaration,
)

#: 预览嵌套调用参数时最多留多少字符（上游 ``ARGS_PREVIEW_CHARS``）。
ARGS_PREVIEW_CHARS = 200
#: 预览错误时最多留多少字符（上游 ``ERROR_PREVIEW_CHARS``）。
ERROR_PREVIEW_CHARS = 500
#: 一个脚本可同时在飞的 ``models.classify()`` / ``models.generateImages()`` 调用数。
MAX_CONCURRENT_MODEL_CALLS = 4
#: QuickJS VM 的堆上限（上游注释：worker 与 pi 同进程，不限的话失控脚本能涨到 wasm32 的 4 GiB）。
CODEMODE_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
#: 脚本工具可接受的模型类型（上游 ``MODEL_TYPES``）。
MODEL_TYPES: frozenset[str] = frozenset({"chat", "image", "classifier"})
#: 脚本输出的默认 token 预算（上游 ``DEFAULT_MAX_OUTPUT_TOKENS``）。
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
#: 估算 token 数时每个 token 折合多少字符。
CHARS_PER_TOKEN = 4


def truncate_text(text: str, max_chars: int) -> str:
    """超长则截断成 ``<前 max_chars-3 个字符>...``（上游 ``truncateText``）。"""
    return f"{text[: max_chars - 3]}..." if len(text) > max_chars else text


def preview_args(args: Any) -> str:
    """把参数压成一行预览；不可序列化时返回空串（上游 ``previewArgs``）。"""
    if args is None:
        return ""
    try:
        rendered = json.dumps(args, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""
    return truncate_text(rendered, ARGS_PREVIEW_CHARS)


def text_of(result: Any) -> str:
    """结果里所有文本块连起来（上游 ``textOf``）。"""
    blocks = getattr(result, "content", None) or []
    return "\n".join(block.text for block in blocks if getattr(block, "type", None) == "text")


def to_model_type(value: Any) -> str:
    """校验脚本给的模型类型（上游 ``toModelType``）。"""
    if isinstance(value, str) and value in MODEL_TYPES:
        return value
    raise ValueError(
        f'Unknown model type {json.dumps(value)}. Use "chat", "image", or "classifier".'
    )


def to_provider(value: Any) -> str | None:
    """校验脚本给的 provider（上游 ``toProvider``）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("provider must be a string")
    return value


def to_model_info(model: dict[str, Any]) -> dict[str, Any]:
    """给脚本看的目录条目；**丢掉 ``headers``**（models.json 的 headers 可能含凭据）。"""
    info = dict(model)
    info.pop("headers", None)
    return info


def with_article(word: str) -> str:
    """``an image`` / ``a classifier``（上游 ``withArticle``）。"""
    return f"{'an' if word[:1].lower() in 'aeiou' and word[:1] else 'a'} {word}"


def describe_value(value: Any) -> str:
    """错误消息里的一个脚本值：``undefined`` / ``a string`` / ``an array`` / 它的键。

    上游对 ``undefined`` 与 ``null`` 都走 ``String(value)`` → ``"undefined"`` / ``"null"``。
    本端没有 ``undefined``，``None`` 记为 ``"null"``（不同于上游的 ``undefined``，
    因为 Python 里 ``None`` 就是那个"没有值"）。
    """
    if value is None:
        return "null"
    if isinstance(value, (list, tuple)):
        return "an empty array" if len(value) == 0 else "an array"
    if isinstance(value, dict):
        keys = list(value)
        if not keys:
            return "{}"
        head = ", ".join(keys[:6])
        return f"{{ {head}{', ...' if len(keys) > 6 else ''} }}"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):
        return "a boolean"
    return f"a {type(value).__name__}"


#: 脚本里 ``models.classify()`` 期望的上下文形状（上游 ``CLASSIFIER_CONTEXT_SHAPE``）。
CLASSIFIER_CONTEXT_SHAPE = (
    '{ state: { ... }, images?: [{ type: "image", data: <base64>, mimeType }], '
    'questions: { <id>: { type: "choice", instructions, criteria: { <label>: <meaning> } } | '
    '{ type: "score", instructions, criteria: [<lowest level>, ..., <highest level>] } | '
    '{ type: "bool", instructions, criteria: { true: <meaning>, false: <meaning> } } } }'
)


def _is_string_list(values: Sequence[Any]) -> bool:
    return len(values) > 0 and all(isinstance(value, str) for value in values)


def check_classifier_context(context: Any) -> Any:
    """校验脚本给的分类器上下文。

    让脚本的笔误以**期望的形状**报错，而不是留到 provider 那边炸（上游 ``checkClassifierContext``）。
    返回原对象（本端不做类型转换）。
    """

    def fail(problem: str) -> ValueError:
        return ValueError(
            f"models.classify() {problem}. Expected context: {CLASSIFIER_CONTEXT_SHAPE}. "
            f'See "Classify" in {CODEMODE_DOCS_PATH}.'
        )

    if not isinstance(context, dict):
        raise fail(
            f"expects a context object as its second argument, got {describe_value(context)}"
        )
    state = context.get("state")
    if not isinstance(state, dict):
        raise fail(f"context.state must be an object, got {describe_value(state)}")
    images = context.get("images")
    if images is not None:
        if not isinstance(images, list):
            raise fail(f"context.images must be an array, got {describe_value(images)}")
        for index, image in enumerate(images):
            if not (
                isinstance(image, dict)
                and image.get("type") == "image"
                and isinstance(image.get("data"), str)
                and isinstance(image.get("mimeType"), str)
            ):
                raise fail(
                    f"context.images[{index}] must be an image block, got {describe_value(image)}"
                )
    questions = context.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise fail(
            f"context.questions must map question IDs to questions, got {describe_value(questions)}"
        )
    for question_id, question in questions.items():
        at = f"context.questions.{question_id}"
        if not isinstance(question, dict):
            raise fail(f"{at} must be a question object, got {describe_value(question)}")
        instructions = question.get("instructions")
        if not isinstance(instructions, str):
            raise fail(f"{at}.instructions must be a string")
        kind = question.get("type")
        criteria = question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not _is_string_list(list(criteria.values())):
                raise fail(
                    f'{at} is a "choice" question, so criteria must map each label to its meaning'
                )
        elif kind == "score":
            if not isinstance(criteria, list) or not _is_string_list(criteria):
                raise fail(
                    f'{at} is a "score" question, so criteria must list the levels as strings, lowest first'
                )
        elif kind == "bool":
            if not (
                isinstance(criteria, dict)
                and isinstance(criteria.get("true"), str)
                and isinstance(criteria.get("false"), str)
            ):
                raise fail(
                    f'{at} is a "bool" question, so criteria must be {{ true: string, false: string }}'
                )
        else:
            raise fail(f'{at}.type must be "choice", "score", or "bool", got {json.dumps(kind)}')
    return context


def check_images_context(context: Any) -> Any:
    """校验脚本给的图像上下文（上游 ``checkImagesContext``）。返回原对象。"""

    def fail(problem: str) -> ValueError:
        return ValueError(
            f'models.generateImages() {problem}. Expected context: {{ input: [{{ type: "text", '
            f'text: <prompt> }}, ...optional {{ type: "image", data: <base64>, mimeType }} references] }}. '
            f'See "Generate images" in {CODEMODE_DOCS_PATH}.'
        )

    if not isinstance(context, dict):
        raise fail(
            f"expects a context object as its second argument, got {describe_value(context)}"
        )
    inputs = context.get("input")
    if not isinstance(inputs, list) or not inputs:
        raise fail(
            f"context.input must be a non-empty array of blocks, got {describe_value(inputs)}"
        )
    for index, block in enumerate(inputs):
        if isinstance(block, dict):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                continue
            if (
                block.get("type") == "image"
                and isinstance(block.get("data"), str)
                and isinstance(block.get("mimeType"), str)
            ):
                continue
        raise fail(
            f"context.input[{index}] must be a text or image block, got {describe_value(block)}"
        )
    return context


def create_limiter(limit: int) -> Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]:
    """最多同时跑 ``limit`` 个调用，**按调用顺序**（上游 ``createLimiter``）。"""

    class _Limiter:
        def __init__(self) -> None:
            self._active = 0
            self._waiting: list[asyncio.Future[None]] = []

        async def run(self, job: Callable[[], Awaitable[Any]]) -> Any:
            if self._active >= limit:
                waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self._waiting.append(waiter)
                await waiter
            self._active += 1
            try:
                return await job()
            finally:
                self._active -= 1
                if self._waiting:
                    self._waiting.pop(0).set_result(None)

    limiter = _Limiter()
    return limiter.run


def is_store_entry_data(data: Any) -> bool:
    """判断 ``data`` 是不是 :class:`CodemodeStoreEntryData`（上游 ``isStoreEntryData``）。"""
    if not isinstance(data, dict):
        return False
    deleted = data.get("delete")
    return (
        isinstance(data.get("set"), dict)
        and isinstance(deleted, list)
        and all(isinstance(key, str) for key in deleted)
    )


def read_codemode_store(branch: Iterable[Any]) -> dict[str, Any]:
    """``load()`` 的值：分支上的 ``codemode-store`` 条目，**从根开始依次套用**。

    每条先执行 ``delete`` 再套用 ``set``（上游顺序），因此同一分支上后写的覆盖先写的。
    """
    store: dict[str, Any] = {}
    for entry in branch:
        if (
            entry.get("type") != "custom"
            or entry.get("customType") != CODEMODE_STORE_ENTRY_TYPE
            or not is_store_entry_data(entry.get("data"))
        ):
            continue
        data: CodemodeStoreEntryData = entry["data"]
        for key in data["delete"]:
            store.pop(key, None)
        store.update(data["set"])
    return store


def value_text(value: Any) -> str:
    """同脚本里的 ``text()``：字符串原样，其它值压成紧凑 JSON（上游 ``valueText``）。"""
    if isinstance(value, str):
        return value
    try:
        rendered = json.dumps(value, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)
    return rendered if rendered is not None else str(value)


# ---- ``CodemodeOutputItem`` / 错误的读取辅助（形状见 pi_codemode.types） ----


def _field(item: Any, name: str, default: Any = None) -> Any:
    """读一个输出项的字段；``CodemodeOutputItem`` 是 dataclass，但 dict 也接受。"""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _item_type(item: Any) -> str | None:
    value = _field(item, "type")
    return value if isinstance(value, str) else None


def _is_console(item: Any) -> bool:
    return bool(_field(item, "console", False))


def _as_image(item: Any) -> ImageContent:
    return ImageContent(
        data=_field(item, "data", "") or "", mime_type=_field(item, "mime_type", "") or ""
    )


def _error_fields(error: Any) -> tuple[str, str, str | None, str | None]:
    """``(kind, message, name, stack)``；``CodemodeError`` 是 dataclass，dict 也接受。"""
    return (
        str(_field(error, "kind", "")),
        str(_field(error, "message", "") or ""),
        _field(error, "name"),
        _field(error, "stack"),
    )


def format_output(output: Sequence[Any]) -> list[TextContent | ImageContent]:
    """把脚本输出排成模型能分辨的样子（上游 ``formatOutput``）。

    有**多于一个**文本项（``text()`` 或返回值）时，每项以 ``==> text N/M <==`` 开头；
    ``console.*`` 的行跟在其它输出之后，合成一个 ``<console_output>`` 块。
    """
    total = sum(1 for item in output if _item_type(item) == "text" and not _is_console(item))
    items: list[TextContent | ImageContent] = []
    console_lines: list[str] = []
    index = 0
    for item in output:
        kind = _item_type(item)
        if kind == "image":
            items.append(_as_image(item))
        elif _is_console(item):
            console_lines.append(_field(item, "text", ""))
        else:
            index += 1
            body = _field(item, "text", "")
            text = f"==> text {index}/{total} <==\n{body}" if total > 1 else body
            items.append(TextContent(text=text))
    if console_lines:
        items.append(
            TextContent(text=f"<console_output>\n{chr(10).join(console_lines)}\n</console_output>")
        )
    return items


def join_adjacent_text(
    items: Sequence[TextContent | ImageContent],
) -> list[TextContent | ImageContent]:
    """把相邻文本项合成一项，每部分各起一行。

    分隔判断看**最后一段非空内容**而不是拼接后的整串 —— 读一个增长中字符串的结尾会让
    每次拼接都复制一遍，那是 O(n²)（上游 #10678，本端同样只记 ``last_part``）。
    """
    joined: list[TextContent | ImageContent] = []
    parts: list[str] = []
    last_part = ""

    def flush() -> None:
        nonlocal parts, last_part
        if parts:
            joined.append(TextContent(text="".join(parts)))
        parts = []
        last_part = ""

    for item in items:
        if getattr(item, "type", None) != "text":
            flush()
            joined.append(item)
            continue
        text = getattr(item, "text", "")
        if last_part != "" and not last_part.endswith("\n"):
            parts.append("\n")
            last_part = "\n"
        parts.append(text)
        if text != "":
            last_part = text
    flush()
    return joined


def format_call_summary(calls: Sequence[Any]) -> str:
    """失败前已经发生的工具调用（它们**不会**被撤销）。"""
    if not calls:
        return "No tool calls were made."
    rendered = ", ".join(f"{call.name} ({call.status})" for call in calls)
    return f"Tool calls made before the failure (they are not undone): {rendered}"


def format_error(result: Any, calls: Sequence[Any]) -> str:
    """脚本失败的头部文本 + 已发生的调用（上游 ``formatError``）。"""
    error = result["error"] if isinstance(result, dict) else result.error
    kind, message, name, stack = _error_fields(error)
    if kind == "script":
        head = stack or f"{name or 'Error'}: {message}"
    elif kind == "timeout":
        head = f"Script timed out: {message}"
    elif kind == "aborted":
        head = f"Script aborted: {message}"
    else:
        head = f"Script sandbox failed: {message}"
    return f"{head}\n\n{format_call_summary(calls)}"


#: ``image()`` 接受的图像类型的扩展名（上游 ``IMAGE_EXTENSIONS``）。
IMAGE_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


async def spill_output(text: str) -> dict[str, str]:
    """把完整文本输出写到临时文件（像 bash 处理被截断的输出）。

    返回 ``{"path": …}`` 或 ``{"error": …}`` —— 与上游的联合类型同形。
    """
    try:
        return {"path": await save_output_file(text, ".txt", prefix="pi-codemode-")}
    except OSError as error:
        return {"error": str(error) or error.__class__.__name__}


async def save_images(
    items: Sequence[TextContent | ImageContent],
) -> list[TextContent | ImageContent]:
    """把每张图存到临时文件，并在它**前面**放一个带路径的文本项。

    模型看得到图，但没有别的办法拿到它的字节（脚本不能写文件，``write`` 只收文本）。
    **同样的图出现多次只存一次**。写失败（磁盘满、临时目录不可写）不能丢掉一个
    工具调用已经跑过的脚本的结果，所以失败信息进入那个标签。
    """
    labels: dict[str, asyncio.Task[str]] = {}

    async def label(data: str, mime_type: str) -> str:
        raw = base64.b64decode(data)
        kind = f"{mime_type}, {format_size(len(raw))}"
        extension = IMAGE_EXTENSIONS.get(mime_type)
        if extension is None:
            raise ValueError(f"No file extension for image type {mime_type}")
        try:
            path = await save_output_file(raw, extension, prefix="pi-codemode-")
        except OSError as error:
            reason = str(error) or error.__class__.__name__
            return f"[Image ({kind}) could not be saved: {reason}]"
        return f"[Image saved to {path} ({kind})]"

    async def one(item: TextContent | ImageContent) -> list[TextContent | ImageContent]:
        if getattr(item, "type", None) != "image":
            return [item]
        data = getattr(item, "data", "") or ""
        pending = labels.get(data)
        if pending is None:
            pending = asyncio.ensure_future(label(data, getattr(item, "mime_type", "") or ""))
            labels[data] = pending
        return [TextContent(text=await pending), item]

    results = await asyncio.gather(*(one(item) for item in items))
    return [entry for group in results for entry in group]


async def truncate_output(
    items: Sequence[TextContent | ImageContent],
    max_tokens: int,
) -> dict[str, Any]:
    """套用 token 预算（上游 ``truncateOutput``）。

    文本合计超预算时，文本项合成**一项**（保留开头与结尾），图片跟在它后面；
    完整文本写到临时文件。返回 ``{"items": [...], "full_output_path"?: str}``。
    """
    texts = [getattr(item, "text", "") for item in items if getattr(item, "type", None) == "text"]
    combined = "\n".join(texts)
    budget = max_tokens * CHARS_PER_TOKEN
    if not texts or len(combined) <= budget:
        return {"items": list(items)}
    head_chars = budget // 2
    tail_chars = budget - head_chars
    removed = len(combined) - head_chars - tail_chars
    head = combined[:head_chars]
    tail = combined[-tail_chars:] if tail_chars > 0 else ""
    truncated_tokens = math.ceil(removed / CHARS_PER_TOKEN)
    text = (
        f"Warning: truncated output (original token count: "
        f"{math.ceil(len(combined) / CHARS_PER_TOKEN)})\n"
        f"Total output lines: {len(combined.split(chr(10)))}\n\n"
        f"{head}…{truncated_tokens} tokens truncated…{tail}"
    )
    spilled = await spill_output(combined)
    if "path" in spilled:
        text += f"\n\n[Full output: {spilled['path']} (read with offset/limit)]"
    else:
        text += f"\n\n[Could not save the full output: {spilled['error']}]"
    result: dict[str, Any] = {
        "items": [
            TextContent(text=text),
            *(i for i in items if getattr(i, "type", None) == "image"),
        ]
    }
    if "path" in spilled:
        result["full_output_path"] = spilled["path"]
    return result


def to_script_value(tool: Any, result: Any) -> Any:
    """脚本拿到的嵌套调用返回值（上游 ``toScriptValue``）。

    声明了 ``output_schema`` 的工具解析成它的 ``structured_content``（**带该字段的错误结果也算**，
    例如 MCP 的 ``isError``）；其它工具解析成文本内容。其它失败抛异常，带工具的错误文本。

    **偏离**：上游第二参是 ``AgentToolCallOutcome``（``{ result, isError }``）；本仓
    ``AgentToolResult`` **自带 ``is_error``**，没有外层包装类型，故这里直接收结果对象。
    """
    if (
        getattr(tool, "output_schema", None)
        and getattr(result, "structured_content", None) is not None
    ):
        return result.structured_content
    text = text_of(result)
    if getattr(result, "is_error", False):
        raise ValueError(text or f'Tool "{tool.name}" failed')
    return text


__all__ = [
    "ARGS_PREVIEW_CHARS",
    "CHARS_PER_TOKEN",
    "CLASSIFIER_CONTEXT_SHAPE",
    "CODEMODE_MEMORY_LIMIT_BYTES",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "ERROR_PREVIEW_CHARS",
    "IMAGE_EXTENSIONS",
    "MAX_CONCURRENT_MODEL_CALLS",
    "MODEL_TYPES",
    "check_classifier_context",
    "check_images_context",
    "create_limiter",
    "describe_value",
    "format_call_summary",
    "format_error",
    "format_output",
    "is_store_entry_data",
    "join_adjacent_text",
    "preview_args",
    "read_codemode_store",
    "save_images",
    "spill_output",
    "text_of",
    "to_model_info",
    "to_model_type",
    "to_provider",
    "to_script_value",
    "truncate_output",
    "truncate_text",
    "value_text",
    "with_article",
]


# ============================================================
# ④-2b-2：脚本全局
# ============================================================


def is_namespace_name(namespace: ToolNamespace, query: str) -> bool:
    """``query`` 是否指这个 namespace（上游 ``isNamespaceName``）。

    名字或其别名，**按原样或按同一个脚本标识符**匹配
    （``dev-radius`` 与 ``dev_radius`` 都是 ``dev_radius``）。
    """
    query_id = to_codemode_identifier(query)
    return any(
        name == query or to_codemode_identifier(name) == query_id
        for name in (namespace.name, *(namespace.aliases or ()))
    )


def create_discovery_globals(
    tools: Sequence[ToolDefinition],
    samples: Mapping[str, str],
    options: CodemodeToolOptions | None = None,
) -> list[CodemodeTool]:
    """``searchTools()`` / ``describeTool()`` / ``describeNamespace()``。

    脚本自己的工具之上的排序检索与查找（上游 ``createDiscoveryGlobals``）。
    """
    opts = options or CodemodeToolOptions()
    ranker = Bm25Ranker()

    def entry(name: str) -> dict[str, str]:
        return {"name": to_codemode_identifier(name), "description": samples.get(name, "")}

    def _namespace_of(tool: ToolDefinition) -> ToolNamespace | None:
        getter = opts.get_tool_namespace
        return getter(tool.name) if getter is not None else None

    def search_tools(args: Any) -> Any:
        query, search_options = (
            (list(args) + [None, None])[:2] if isinstance(args, list) else (args, None)
        )
        if not isinstance(query, str):
            raise ValueError("searchTools() expects a query string")
        options_map = search_options if isinstance(search_options, dict) else {}
        limit = options_map.get("limit", DEFAULT_TOOL_SEARCH_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("searchTools() limit must be a positive integer")
        namespace = options_map.get("namespace")
        if namespace is not None and not isinstance(namespace, str):
            raise ValueError("searchTools() namespace must be a string")
        documents = []
        for tool in tools:
            tool_namespace = _namespace_of(tool)
            if namespace and (
                tool_namespace is None or not is_namespace_name(tool_namespace, namespace)
            ):
                continue
            documents.append(create_tool_search_document(tool, tool_namespace))  # type: ignore[arg-type]
        return [entry(match.name) for match in ranker.rank(query, documents, limit)]

    def describe_tool(args: Any) -> Any:
        name = (list(args) + [None])[0] if isinstance(args, list) else args
        if not isinstance(name, str):
            raise ValueError("describeTool() expects a tool name")
        found = next(
            (
                tool
                for tool in tools
                if tool.name == name or to_codemode_identifier(tool.name) == name
            ),
            None,
        )
        return samples.get(found.name) if found is not None else None

    def describe_namespace(args: Any) -> Any:
        name = (list(args) + [None])[0] if isinstance(args, list) else args
        if not isinstance(name, str):
            raise ValueError("describeNamespace() expects a namespace name")
        namespace: ToolNamespace | None = None
        names: list[str] = []
        for tool in tools:
            tool_namespace = _namespace_of(tool)
            if tool_namespace is None or not is_namespace_name(tool_namespace, name):
                continue
            if namespace is None:
                namespace = tool_namespace
            names.append(to_codemode_identifier(tool.name))
        if namespace is None:
            return None
        described: dict[str, Any] = {"name": namespace.name}
        if namespace.description:
            described["description"] = namespace.description
        if namespace.instructions:
            described["instructions"] = namespace.instructions
        described["tools"] = names
        return described

    return [
        CodemodeTool(name="searchTools", spread=True, execute=search_tools, description=""),
        CodemodeTool(name="describeTool", spread=True, execute=describe_tool, description=""),
        CodemodeTool(
            name="describeNamespace", spread=True, execute=describe_namespace, description=""
        ),
    ]


def create_model_globals(
    models: Any,
    tool_call_id: str,
    calls: list[CodemodeNestedCall],
    publish: Callable[[], None],
    add_usage: Callable[[Any], None],
    add_generated_images: Callable[[int], None],
) -> list[CodemodeTool]:
    """脚本的 ``models.*``：``docs/codemode.md`` 里记录的模型目录方法（上游 ``createModelGlobals``）。

    分类与图像调用会作为**嵌套调用行**出现，因此渲染器能看到它们，其用量进 ``add_usage``。
    行里只显示模型，**从不显示 prompt 或图像数据**。

    **偏离（本仓形状）**：`ModelRegistry.get_models_of_type` 在本仓是 **async**（上游同步），
    故 ``models.getModelsOfType`` 的实现也是 async 并 ``await`` 它。
    """
    limit = create_limiter(MAX_CONCURRENT_MODEL_CALLS)
    call_count = 0

    async def run_model_call(
        name: str,
        model_type: str,
        args: Any,
        check_context: Callable[[Any], Any],
        run: Callable[[Any, Any], Awaitable[Any]],
    ) -> Any:
        """只按 provider + id 解析模型、校验上下文，然后作为嵌套调用行执行。

        脚本自带的 ``baseUrl`` / ``headers`` **绝不能拿到凭据**。
        """
        list_hint = f'List the {model_type} models you can use with models.getAvailableOfType("{model_type}").'
        values = list(args) + [None, None] if isinstance(args, list) else [args, None]
        model, context = values[0], values[1]
        given = model if isinstance(model, dict) else None
        if (
            given is None
            or not isinstance(given.get("provider"), str)
            or not isinstance(given.get("id"), str)
        ):
            # 未传时以 None 到达：展开的实参跨沙箱是一个 JSON 数组。
            undefined_hint = (
                " models.getModelOfType() returns undefined for an unknown provider or id."
                if model is None
                else ""
            )
            raise ValueError(
                f"{name}() expects {with_article(model_type)} model as its first argument, "
                f"got {describe_value(model)}.{undefined_hint} {list_hint}"
            )
        provider = given["provider"]
        model_id = given["id"]
        ref = f"{provider}/{model_id}"
        resolved = models.get_model_of_type(model_type, provider, model_id)
        if resolved is None:
            actual_type = next(
                (
                    other
                    for other in MODEL_TYPES
                    if other != model_type
                    and models.get_model_of_type(other, provider, model_id) is not None
                ),
                None,
            )
            if actual_type is not None:
                raise ValueError(
                    f'"{ref}" is {with_article(actual_type)} model, not {with_article(model_type)} model. '
                    f"{list_hint}"
                )
            raise ValueError(f'Unknown {model_type} model "{ref}". {list_hint}')
        checked = check_context(context)

        nonlocal call_count
        call_count += 1
        record = CodemodeNestedCall(
            id=f"{tool_call_id}/{name}/{call_count}",
            name=name,
            args=f"{_field(resolved, 'provider', '')}/{_field(resolved, 'id', '')}",
            status="running",
        )
        calls.append(record)
        publish()
        started = time.perf_counter()
        result = await limit(lambda: run(resolved, checked))
        record.duration_ms = (time.perf_counter() - started) * 1000
        # 三态：stop → ok / aborted → cancelled / 其余（error）→ error。
        # 取消与错误在 UI 上是**不同提示**，不合并。
        stop_reason = _field(result, "stop_reason", None)
        record.status = (
            "ok" if stop_reason == "stop" else "cancelled" if stop_reason == "aborted" else "error"
        )
        error_message = _field(result, "error_message", None)
        if error_message:
            record.error = truncate_text(str(error_message), ERROR_PREVIEW_CHARS)
        usage = _field(result, "usage", None)
        if usage is not None:
            record.cost = _field(_field(usage, "cost", None), "total", None)
            add_usage(usage)
        publish()
        return result

    async def get_models_of_type(args: Any) -> Any:
        model_type, provider = _two(args)
        # 本仓该方法是 async（上游同步）—— 必须 await。
        found = await models.get_models_of_type(to_model_type(model_type), to_provider(provider))
        return [to_model_info(dict(model)) for model in found]

    async def get_available_of_type(args: Any, signal: Any = None) -> Any:
        model_type, provider = _two(args)
        options = None if signal is None else {"signal": signal}
        available = await models.get_available_of_type(
            to_model_type(model_type), to_provider(provider), options
        )
        return [to_model_info(dict(model)) for model in available]

    def get_model_of_type(args: Any) -> Any:
        values = list(args) + [None, None, None] if isinstance(args, list) else [args, None, None]
        model_type, provider, model_id = values[0], values[1], values[2]
        if not isinstance(provider, str) or not isinstance(model_id, str):
            got = ", ".join(describe_value(value) for value in values[:3])
            raise ValueError(
                f"models.getModelOfType(type, provider, id) expects three strings, got ({got}). "
                "The provider and the id are separate arguments, for example "
                'models.getModelOfType("classifier", "typesafe", "jev-latest").'
            )
        model = models.get_model_of_type(to_model_type(model_type), provider, model_id)
        return None if model is None else to_model_info(dict(model))

    async def classify(args: Any, signal: Any = None) -> Any:
        return await run_model_call(
            "models.classify",
            "classifier",
            args,
            check_classifier_context,
            lambda resolved, context: models.classify(resolved, context, _signal_options(signal)),
        )

    async def generate_images(args: Any, signal: Any = None) -> Any:
        async def run(resolved: Any, context: Any) -> Any:
            result = await models.generate_images(resolved, context, _signal_options(signal))
            output = _field(result, "output", None) or []
            add_generated_images(
                sum(1 for block in output if _field(block, "type", None) == "image")
            )
            return result

        return await run_model_call(
            "models.generateImages", "image", args, check_images_context, run
        )

    return [
        CodemodeTool(
            name="models.getModelsOfType", spread=True, description="", execute=get_models_of_type
        ),
        CodemodeTool(
            name="models.getAvailableOfType",
            spread=True,
            description="",
            execute=get_available_of_type,
        ),
        CodemodeTool(
            name="models.getModelOfType", spread=True, description="", execute=get_model_of_type
        ),
        CodemodeTool(name="models.classify", spread=True, description="", execute=classify),
        CodemodeTool(
            name="models.generateImages", spread=True, description="", execute=generate_images
        ),
    ]


def _two(args: Any) -> tuple[Any, Any]:
    """展开实参列表的前两位（``spread`` 全局收到的是列表）。"""
    if isinstance(args, list):
        padded = list(args) + [None, None]
        return padded[0], padded[1]
    return args, None


def _signal_options(signal: Any) -> Any:
    return None if signal is None else {"signal": signal}


#: 模块级：跨调用、跨会话共享的 codemode worker 名额（上游 ``activeCodemodeWorkers``，``execute.ts:59``）。
ACTIVE_CODEMODE_WORKERS = 0
#: 同时在跑（含正在退出）的 worker 上限（上游 ``MAX_CONCURRENT_CODEMODE_WORKERS``）。
MAX_CONCURRENT_CODEMODE_WORKERS = 2


class CodemodeWorkerLimitError(RuntimeError):
    """worker 名额已满（上游在这里 ``throw``，**不排队**）。"""


def _acquire_worker() -> Callable[[], None]:
    """占用一个 worker 名额，返回释放函数（上游 ``acquireWorker``）。

    **不等价，窗口更宽**：上游的 ``acquireWorker`` 由沙箱**在实际起 worker 时**调用，
    配额窗口 = worker 存活期；本端 ``CodemodeSandbox`` 没有该回调，只能在**调用沙箱前**占用，
    因此窗口 = **装配 + 执行期**（含建引擎 / 编译 wasm 的那段）。

    名额满时**抛错，不排队**（上游消息：*Retry after they exit.*）—— 所以不需要队列，
    也意味着「超时不是从入队那刻算」。
    """
    global ACTIVE_CODEMODE_WORKERS
    if ACTIVE_CODEMODE_WORKERS >= MAX_CONCURRENT_CODEMODE_WORKERS:
        raise CodemodeWorkerLimitError(
            f"Codemode worker limit reached ({MAX_CONCURRENT_CODEMODE_WORKERS} workers are still "
            "running or shutting down). Retry after they exit."
        )
    ACTIVE_CODEMODE_WORKERS += 1
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        global ACTIVE_CODEMODE_WORKERS
        ACTIVE_CODEMODE_WORKERS -= 1

    return release


async def execute_codemode(
    tool_call_id: str,
    tool_input: Mapping[str, Any],
    signal: Any = None,
    on_update: Callable[[Any], None] | None = None,
    ctx: Any = None,
    options: CodemodeToolOptions | None = None,
) -> Any:
    """跑一个 codemode 脚本（上游 ``executeCodemode``，``execute.ts:440``）。

    **同步沙箱 + ``to_thread``**：本仓 ``CodemodeSandbox.execute`` 是**同步阻塞**调用
    （上游 ``await sandbox.execute(...)``）。它在 ``run_in_executor`` 里跑，
    **中断只能靠 ``abort_event``** —— ``to_thread`` / executor 的任务被取消时，
    **线程会继续跑到底**。

    **名额在「线程真正结束」时才归还**（``future.add_done_callback``），
    而不是在 ``await`` 的 ``finally`` 里：外层被取消时 ``finally`` 会立刻执行，
    而线程还在跑 —— 那样实际并发会短暂**超过**名额。
    """
    started_at = time.perf_counter()
    parsed = parse_codemode_source(_field(tool_input, "code", "") or "")
    source_options = _field(parsed, "options", None)
    code = _field(parsed, "code", "")

    calls: list[CodemodeNestedCall] = []
    model_usage: list[Any] = []
    generated_images = [0]

    def snapshot() -> CodemodeToolDetails:
        return CodemodeToolDetails(calls=[replace(call) for call in calls])

    def publish() -> None:
        if on_update is not None:
            on_update({"content": [], "details": snapshot()})

    # 首个嵌套调用之前就让这一行显示为「运行中」，好让计时器起跑。
    publish()

    callable_tools = get_codemode_callable_tools(_field(ctx, "tools", None) or []) if ctx else []
    guidelines = (
        options.get_tool_guidelines()
        if options is not None and options.get_tool_guidelines
        else None
    )
    samples = {
        tool.name: render_tool_sample(
            to_codemode_declaration(tool, (guidelines or {}).get(tool.name, ()))
        )
        for tool in callable_tools
    }

    async def run_nested(tool: ToolDefinition, args: Any, call_signal: Any) -> Any:
        record = CodemodeNestedCall(
            id=f"{tool_call_id}/?", name=tool.name, args=preview_args(args), status="running"
        )
        calls.append(record)
        publish()
        call_started = time.perf_counter()
        if ctx is None:
            raise ValueError("Tool calls need a session")
        execute_tool = _field(ctx, "execute_tool", None)
        outcome = await execute_tool(tool.name, args, {"signal": call_signal})
        tool_call = _field(outcome, "tool_call", None)
        if tool_call is not None:
            record.id = str(_field(tool_call, "id", record.id))
        record.duration_ms = (time.perf_counter() - call_started) * 1000
        result = _field(outcome, "result", None)
        is_error = bool(_field(outcome, "is_error", False))
        if is_error:
            aborted = (
                call_signal is not None
                and getattr(call_signal, "is_set", None) is not None
                and call_signal.is_set()
            )
            record.status = "cancelled" if aborted else "error"
            record.error = truncate_text(
                text_of(result) or f'Tool "{tool.name}" failed', ERROR_PREVIEW_CHARS
            )
        else:
            record.status = "ok"
        publish()
        return to_script_value(tool, outcome)

    sandbox_tools = [
        CodemodeTool(
            name=tool.name,
            description=samples.get(tool.name, ""),
            execute=_make_nested_executor(tool, run_nested),
        )
        for tool in callable_tools
    ]
    globals_: list[CodemodeTool] = []
    if callable_tools:
        globals_.extend(create_discovery_globals(callable_tools, samples, options))
    if options is not None and options.models and ctx is not None:
        registry = _field(ctx, "model_registry", None)
        if registry is not None:
            globals_.extend(
                create_model_globals(
                    registry,
                    tool_call_id,
                    calls,
                    publish,
                    lambda usage: model_usage.append(usage),
                    lambda count: generated_images.__setitem__(0, generated_images[0] + count),
                )
            )

    timeout_ms = _field(source_options, "timeout_ms", None)
    sandbox = CodemodeSandbox(
        tools=sandbox_tools,
        globals=globals_,
        timeout_ms=math.inf if timeout_ms is None else float(timeout_ms),
        memory_limit_bytes=CODEMODE_MEMORY_LIMIT_BYTES,
        engine=load_quickjs_wasm(),
    )

    branch = _field(_field(ctx, "session_manager", None), "get_branch", None) if ctx else None
    store = read_codemode_store(branch() if branch is not None else [])

    release = _acquire_worker()
    abort_event = threading.Event()
    bridge: asyncio.Task[None] | None = None
    if signal is not None:
        bridge = asyncio.ensure_future(_bridge_abort(signal, abort_event))

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None, functools.partial(sandbox.execute, code, store=store, abort_event=abort_event)
    )

    def _finish(_done: Any) -> None:
        # 名额与沙箱都拴在**线程真正结束**上：外层被取消时 await 会立刻退出，线程却还在跑。
        if bridge is not None:
            bridge.cancel()
        with contextlib.suppress(Exception):
            sandbox.close()
        release()

    future.add_done_callback(_finish)
    result = await asyncio.shield(future)

    # 结束时仍标 running 的调用，是被脚本结束 / 超时 / 中止截断的。
    for call in calls:
        if call.status == "running":
            call.status = "cancelled"

    script_output: list[Any] = list(_field(result, "output", None) or [])
    if _field(result, "ok", False):
        writes = _field(result, "store_writes", None)
        written = list(_field(writes, "set", None) or {})
        deleted = list(_field(writes, "delete", None) or [])
        if written or deleted:
            append_entry = options.append_entry if options is not None else None
            if append_entry is not None:
                append_entry(
                    CODEMODE_STORE_ENTRY_TYPE,
                    {"set": dict(_field(writes, "set", {}) or {}), "delete": deleted},
                )
        value = _field(result, "value", None)
        if value is not None:
            script_output.append({"type": "text", "text": value_text(value)})

    items = format_output(script_output)
    if not _field(result, "ok", False):
        items.append(TextContent(text=f"Script error:\n{format_error(result, calls)}"))
    if generated_images[0] > 0 and not any(item.type == "image" for item in items):
        count = generated_images[0]
        items.append(
            TextContent(
                text=f"Note: models.generateImages() returned {count} image{'s' if count != 1 else ''} "
                "that the script did not show. Show each image block of result.output with image(block)."
            )
        )

    truncated = await truncate_output(
        join_adjacent_text(items),
        _field(source_options, "max_output_tokens", None) or DEFAULT_MAX_OUTPUT_TOKENS,
    )
    output = join_adjacent_text(await save_images(truncated["items"]))
    wall_time = f"{time.perf_counter() - started_at:.1f}"
    header = f"{'Script completed' if _field(result, 'ok', False) else 'Script failed'}\nWall time {wall_time} seconds\nOutput:\n"
    details = snapshot()
    if truncated.get("full_output_path"):
        details.full_output_path = truncated["full_output_path"]
    payload: dict[str, Any] = {"content": [TextContent(text=header), *output], "details": details}
    if model_usage:
        combined = model_usage[0]
        for extra in model_usage[1:]:
            combined = combine_usage(combined, extra)
        payload["usage"] = combined
    if not _field(result, "ok", False):
        payload["is_error"] = True
    return payload


async def _bridge_abort(signal: Any, abort_event: threading.Event) -> None:
    """把本仓的 ``asyncio.Event`` 桥接到沙箱要的 ``threading.Event``。

    **时序是关键**：晚一拍就会静默丢失（事件设了、脚本没停）。这里在 ``await signal.wait()``
    返回后**立刻**置位，且该任务与线程同生共死（``_finish`` 里取消）。
    """
    waiter = getattr(signal, "wait", None)
    if waiter is None:
        return
    await waiter()
    abort_event.set()


def _make_nested_executor(tool: ToolDefinition, run_nested: Callable[..., Awaitable[Any]]) -> Any:
    """把 ``CodemodeTool.execute`` 的入参形状（``spread=False`` → 单个实参）接到 ``run_nested``。"""

    async def execute(args: Any, options: Any = None) -> Any:
        call_signal = _field(options, "signal", None) if options is not None else None
        return await run_nested(tool, args, call_signal)

    return execute

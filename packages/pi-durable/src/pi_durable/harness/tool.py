"""工具任务的**叶子层** —— 移植 ``harness/tool.ts``(911) 的 **Slice A**（约 390 行）。

这是按**可测性**切的，不是按代码量切的：``ToolTask`` 三个 phase 都收
``runtime: TaskRuntime``，而它的**实现在 `scheduler.ts`**（关键路径上靠后好几步）。
所以本片只收**不碰 ``TaskRuntime``** 的部分——它顺着文件自己的依赖方向（核心 → 叶子），
**两片之间没有环，也就不需要任何延迟导入**。

**Slice B**（``ToolTask`` 本体 + ``run`` / ``settle`` / ``publish_progress`` /
``final_result`` / ``start_nested_call``）在 `scheduler.py` 到位后做。

⚠️ 两条校验路径在此**分岔**，别混：

* 工具 ``parameters`` → ``pi_ai.validate_tool_arguments``（24 关键字，**6 个被忽略**，见 PORTING 的能力边界登记）
* **``structured_output_schema`` → 本模块的 ``jsonschema``**（全关键字；替换上游的 TypeBox ``Compile``）
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import re
import time
from typing import Any, cast

import jsonschema  # type: ignore[import-untyped]

from pi_ai import TextContent, ToolCall, ToolResultMessage
from pi_chord.context import await_with_context
from pi_chord.json import copy_json
from pi_chord.output import PROGRESS_BYTES_PER_SECOND, OutputLimits, bound_output
from pi_durable.documents import define_doc_family
from pi_durable.entries import ToolResultEntry
from pi_durable.errors import error_message
from pi_durable.harness.json import assign_json
from pi_durable.harness.live import (
    LiveDoc,
    NestedToolSummary,
    clear_progress,
    finish_slot,
    remove_nested_slots,
    tool_slot,
)
from pi_durable.harness.types import (
    NestedToolExecutionResult,
    ToolDiagnostic,
    ToolExecutionResult,
    ToolRegistration,
)
from pi_durable.harness.usage import record_usage
from pi_durable.tasks import define_task
from pi_durable.truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, utf8_byte_length

__all__ = [
    "COMPLETED",
    "MADE_NESTED_CALLS",
    "MAX_SUMMARY_ERROR_CHARS",
    "NestedCallDoc",
    "NestedCallState",
    "ToolTaskCheckpoint",
    "ToolTaskInput",
    "ToolTaskResult",
    "append_tool_result",
    "bound_content",
    "check_key",
    "diagnostic_json",
    "fallback_result",
    "from_slot",
    "harness_error",
    "json_equal",
    "nested_result",
    "output_value",
    "prepare",
    "render_diagnostics",
    "ToolTask",
    "run",
    "start_nested_call",
    "settle",
    "resolve_tool",
    "read_call",
    "publish_progress",
    "final_result",
    "structured_output_error",
    "summary_of",
    "tool_diagnostic",
    "truncated",
    "validate",
]

#: 「本任务给某个工具任务建过嵌套调用」的备忘键名（``tool.ts:87``）。
#:
#: 每次尝试**首次准入之前**提交：只有那时 ``pi.live`` 才可能持有它下面的槽位，
#: 而结算是靠那个来移除槽位的。两者之间崩溃只多花一次扫描。
MADE_NESTED_CALLS = "pi.tool.madeNestedCalls"

#: 一条 ``pi.tool`` 任务的输入（``tool.ts:58-68``）。
ToolTaskInput = dict[str, Any]

#: 一条 ``pi.tool`` 任务的检查点（``tool.ts:70-73``）。
ToolTaskCheckpoint = dict[str, Any]

#: 一条 ``pi.tool`` 任务的结果（``tool.ts:79-81``）。
ToolTaskResult = dict[str, Any]

#: 一次嵌套调用的状态（``tool.ts:90-95``）。
NestedCallState = dict[str, Any]

#: 一个工具调用的嵌套调用，按**调用者任务**分族的文档（``tool.ts:97-108``）。
#:
#: 每个嵌套调用一份文档，所以任何一次提交都不会重写别的。
#: 这样一次重跑能找到它已经做过的调用，调用者也能读到每个结果。**随调用者一起退役。**
NestedCallDoc = define_doc_family(
    {
        "kind": "pi.tool.nested-call",
        "version": 1,
        "scope": "task",
        "family": True,
        "initial": lambda seed=None: {},
    }
)


def diagnostic_json(diagnostic: Any) -> dict[str, Any]:
    """把一条说明转成**严格 JSON** 的 dict（``ToolDiagnostic`` 是 dataclass）。

    ⚠️ 上游没有这一步：那边 diagnostics 本来就是普通对象，``copyJson`` 直接能吃。
    本端**必须**显式转——否则 :func:`pi_chord.json.copy_json` 会抛
    ``Value contains a non-JSON ToolDiagnostic``（R24 同族）。
    """
    if isinstance(diagnostic, dict):
        return dict(diagnostic)
    out: dict[str, Any] = {
        "severity": diagnostic.severity,
        "message": diagnostic.message,
    }
    if diagnostic.code is not None:
        out["code"] = diagnostic.code
    return out


def tool_diagnostic(code: str, message: str) -> ToolDiagnostic:
    """一条 ``error`` 级的说明（``tool.ts:794-796``）。"""
    return ToolDiagnostic(severity="error", message=message, code=code)


def harness_error(code: str, message: str) -> ToolExecutionResult[Any]:
    """Harness **自己**写的一条错误结果：没有内容，只有一条带 ``code`` 的 ``error`` 说明
    （``tool.ts:790-792``）。"""
    return ToolExecutionResult(
        output=(), is_error=True, diagnostics=(tool_diagnostic(code, message),)
    )


def invalid(message: str) -> ToolExecutionResult[Any]:
    """参数无效（``tool.ts:256-258``）。"""
    return harness_error("invalid_arguments", message)


def truncated(dropped: dict[str, Any], retain: str | None = None) -> ToolDiagnostic:
    """Harness 的截断说明（``tool.ts:798-810``）。

    ``retain`` 在**从槽位重建**时是未知的——那时它没有 ``retain`` 可参照。
    """
    kept = "" if retain is None else f" to its {'beginning' if retain == 'head' else 'end'}"
    return ToolDiagnostic(
        severity="warn",
        code="truncated",
        message=(
            f"Output truncated{kept}: {dropped['dropped_lines']} lines, "
            f"{dropped['dropped_bytes']} bytes dropped"
        ),
    )


#: 一次嵌套调用的摘要里保留的错误文本上限（``tool.ts:812``）。
MAX_SUMMARY_ERROR_CHARS = 500


def error_text_of(result: NestedToolExecutionResult, output: list[Any]) -> str:
    """一条嵌套结果的错误消息；没有就取它输出的文本项，用换行连接（``tool.ts:826-830``）。"""
    errors = [d for d in result.get("diagnostics") or [] if _severity(d) == "error"]
    if errors:
        return "\n".join(_message(d) for d in errors)
    return "\n".join(item.text for item in output if _is_text(item))


def summary_of(result: NestedToolExecutionResult, output: list[Any]) -> NestedToolSummary:
    """一次嵌套调用是怎么结束的，给它的槽位用（``tool.ts:814-824``）。

    ⚠️ 三个可选字段**不存在**与**是空串**是两回事：``error == ""`` 表示「没有错误文本」，
    此时**不写那个键**（R23：在**构造处**省略，不指望 no-op 开关）。
    """
    error = error_text_of(result, output)[:MAX_SUMMARY_ERROR_CHARS] if result.get("isError") else ""
    summary: NestedToolSummary = {"isError": bool(result.get("isError"))}
    if result.get("durationMs") is not None:
        summary["durationMs"] = result["durationMs"]
    if result.get("usage") is not None:
        summary["usage"] = result["usage"]
    if error != "":
        summary["error"] = error
    return summary


def output_value(output: list[Any]) -> Any:
    """工具**没有声明 schema** 时，程序从它的输出拿到什么（``tool.ts:744-750``）：

    空 → ``""``；多个 → 内容列表；单个文本 → 那个字符串；单个图片 → 那个图片本身。
    """
    if len(output) == 0:
        return ""
    if len(output) > 1:
        return output
    only = output[0]
    return only.text if _is_text(only) else only


def render_diagnostics(diagnostics: list[ToolDiagnostic]) -> str:
    """把说明渲染成模型看得见的那段 ``<harness>`` 文本（``tool.ts:862-868``）。"""
    body = "\n".join(f"[{d.severity}] {d.message}" for d in diagnostics)
    return f"<harness>\n{body}\n</harness>"


def bound_content(content: list[Any], limits: OutputLimits) -> dict[str, Any]:
    """给结果内容里的文本**定界**（``tool.ts:870-888``）。

    拼接后的文本超过限制时，文本项被换成**一个**定界后的项，
    位置取**第一个**（``head``）或**最后一个**（``tail``）文本项的原位；其它内容保持不动。
    """
    texts = [item for item in content if _is_text(item)]
    bounded = bound_output("".join(item.text for item in texts), limits)
    if bounded.dropped_bytes == 0:
        return {"content": content, "dropped_bytes": 0, "dropped_lines": 0}
    keep = texts[0] if limits.retain == "head" else texts[-1]
    result: list[Any] = []
    for item in content:
        if not _is_text(item):
            result.append(item)
        elif item is keep:
            result.append(item.model_copy(update={"text": bounded.text}))
    return {
        "content": result,
        "dropped_bytes": bounded.dropped_bytes,
        "dropped_lines": bounded.dropped_lines,
    }


#: 显式嵌套调用键的**非法**形状（``tool.ts:890-897``）。
#:
#: 键是调用 ID 的**路径段**，所以不同嵌套调用的 ID 永不碰撞，且**永不是正整数**
#: （默认键用的就是正整数）。``__proto__`` 保持保留——它当年是 JSON 对象的索引键。
_NUMERIC_KEY = re.compile(r"^[1-9][0-9]*$")


def check_key(key: str) -> None:
    """校验一个显式的嵌套调用键（``tool.ts:890-897``）。"""
    if key == "" or "/" in key or key == "__proto__" or _NUMERIC_KEY.match(key):
        raise ValueError(
            f"Nested call key {key!r} must be non-empty, without '/', "
            'not "__proto__", and not a positive integer'
        )


def json_equal(a: Any, b: Any) -> bool:
    """结构化 JSON 相等；**对象键的顺序无关**（``tool.ts:899-911``）。

    ⚠️ Python 的 ``==`` 对 ``True == 1`` 成立、``1 == 1.0`` 也成立，
    **而 JSON 里它们是不同的值**。所以这里**先比类型**再比值——
    否则「``1`` 与 ``true`` 相等」会让「同一把键、不同参数」的嵌套调用被误判成同一个。
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if a is None or b is None:
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def nested_result(
    task_id: int, result: ToolExecutionResult[Any], duration_ms: float | None
) -> NestedToolExecutionResult:
    """一次嵌套调用的结果，**存下来并返回**的那个形状（``tool.ts:656-677``）。

    严格 JSON；**不含 ``output``**（只有模型读它）与 **``control``**（只有模型发起的调用用）。
    """
    nested: dict[str, Any] = {
        "taskId": task_id,
        "structuredOutput": result.structured_output,
        "isError": bool(result.is_error),
        "details": result.details,
        # ⚠️ `ToolDiagnostic` 是 **dataclass**，而 `copyJson` 要**严格 JSON**
        # （R24 同族：chord 的 JSON 工具**不会**替你展开任何对象）。
        # 上游那边 diagnostics 本来就是普通对象，所以没这一步。
        "diagnostics": [diagnostic_json(d) for d in (result.diagnostics or ())],
        "usage": result.usage,
        "durationMs": duration_ms,
    }
    # 上游是 `copyJson(..., {omitUndefinedProperties: true})`。
    # ⚠️ 那是**每个键在构造处省略**（R23）——`None` 在本端既可能是 `undefined` 也可能是 `null`，
    # 所以这里显式滤掉值为 `None` 的键，而不是指望一个 no-op 开关。
    return cast(
        NestedToolExecutionResult,
        copy_json({k: v for k, v in nested.items() if v is not None}),
    )


def fallback_result(task_id: int, name: str, outcome: dict[str, Any]) -> NestedToolExecutionResult:
    """一条**没有存储结果**的嵌套调用（被调度器出错或孤立）拿到的回退结果（``tool.ts:752-765``）。"""
    if outcome["status"] == "faulted":
        message = f"Tool {name} failed: {outcome['error']['message']}"
    elif outcome["status"] == "orphaned":
        message = f"Tool {name} could not resume: {outcome['reason']}"
    else:
        message = f"Tool {name} ended without a result"
    error = harness_error(outcome["status"], message)
    return cast(
        NestedToolExecutionResult,
        {
            "taskId": task_id,
            "isError": True,
            "diagnostics": [diagnostic_json(d) for d in (error.diagnostics or ())],
        },
    )


#: 一个工具任务**怎么结束**；结果条目**两种情形都会**被追加（``tool.ts:767-772``）。
#:
#: ``failed``（执行抛出或被中断）为**这次调用拥有的对话**记录取消意图；
#: 而带 ``is_error`` 的**结果**仍然算 ``completed``。
COMPLETED: dict[str, Any] = {"status": "completed"}


def from_slot(slot: dict[str, Any] | None, code: str, message: str) -> ToolExecutionResult[Any]:
    """从槽位的**持久部分输出**、details 与说明重建一条错误结果（``tool.ts:772-788``）。

    这是「中断 / 丢弃」时唯一的**持久**证据来源——它在 ``pi.live`` 里，不在任务结果里。
    """
    diagnostics = list((slot or {}).get("diagnostics") or [])
    dropped_bytes = (slot or {}).get("droppedBytes") or 0
    if dropped_bytes > 0:
        diagnostics.append(
            truncated(
                {
                    "dropped_bytes": dropped_bytes,
                    "dropped_lines": (slot or {}).get("droppedLines") or 0,
                }
            )
        )
    diagnostics.append(tool_diagnostic(code, message))
    text = (slot or {}).get("output")
    return ToolExecutionResult(
        output=[] if not text else [TextContent(type="text", text=text)],
        is_error=True,
        details=(slot or {}).get("details"),
        diagnostics=tuple(diagnostics),
    )


#: 参数，或者它们为什么无效（``tool.ts:235-236``）。
Checked = dict[str, Any]


def prepare(tool: ToolRegistration[Any], args: dict[str, Any]) -> Checked:
    """参数，按工具自己的 ``prepare_arguments`` 修复之后（``tool.ts:238-246``）。

    修复**抛出**就把它们判为无效——这是工具作者能控制的第一个钩子。
    """
    prepare_arguments = tool.prepare_arguments
    if prepare_arguments is None:
        return {"args": args}
    try:
        return {"args": prepare_arguments(args)}
    except BaseException as error:  # noqa: BLE001 - 上游同样捕获一切
        return {"error": error_message(error)}


def validate(tool: ToolRegistration[Any], call: ToolCall, args: dict[str, Any]) -> Checked:
    """参数按**实现方 schema** 校验并强制转换之后（``tool.ts:248-254``）。

    ⚠️ 走的是 ``pi_ai.validate_tool_arguments``（工具 ``parameters`` 那条路，
    **不是** ``structured_output_schema`` 那条）。
    """
    from pi_ai.validation import validate_tool_arguments

    try:
        # 上游是 `{...call, arguments: args}`：一份**新**对象，不改调用方那份。
        # ⚠️ `call` **两种形状都来**：模型发起的是**从存储读回的 JSON dict**（`read_call`），
        # 而 Slice A 的这条分支按 **pydantic `ToolCall`** 写（它有 `model_copy`）。
        # 端到端跑到这里才炸：`'dict' object has no attribute 'model_copy'`（**R26**）。
        candidate = call if not isinstance(call, dict) else ToolCall(**call)
        merged = candidate.model_copy(update={"arguments": args})
        return {"args": validate_tool_arguments(cast(Any, tool), merged)}
    except BaseException as error:  # noqa: BLE001 - 上游同样捕获一切
        return {"error": error_message(error)}


#: ``structured_output_schema`` 的校验器缓存（``tool.ts:557``）。
#:
#: 上游用 ``WeakMap``（按 schema **对象身份**缓存）。本包按**内容**缓存——
#: 因为 JSON Schema 从存储读回来是 dict，身份不稳定，而内容才是它真正等价的东西。
_STRUCTURED_VALIDATORS: dict[str, Any] = {}


def _validator_for(schema: dict[str, Any]) -> Any:
    key = repr(sorted(schema.items(), key=lambda kv: kv[0]))
    validator = _STRUCTURED_VALIDATORS.get(key)
    if validator is None:
        validator = jsonschema.validators.validator_for(schema)(schema)
        _STRUCTURED_VALIDATORS[key] = validator
    return validator


def structured_output_error(
    tool: ToolRegistration[Any], result: ToolExecutionResult[Any]
) -> str | None:
    """一条结果的 ``structured_output`` 为什么**违反**工具的契约；守住契约时是 ``None``
    （``tool.ts:560-587``）。

    ⚠️ **这里用 ``jsonschema``**（全关键字支持），替换上游 TypeBox 的 ``Compile``。
    与 ``parameters`` 那条路（``validate_tool_arguments``）**是两条路**。
    """
    schema = tool.structured_output_schema
    value = result.structured_output
    if schema is None:
        return (
            None
            if value is None
            else f"Tool {tool.name} returned structuredOutput but declares no structuredOutputSchema"
        )
    if value is None:
        return None if result.is_error is True else f"Tool {tool.name} returned no structuredOutput"

    validator = _validator_for(dict(schema))
    if validator.is_valid(value):
        return None
    first = next(iter(validator.iter_errors(value)), None)
    path = _instance_path(first) or "root"
    detail = str(first.message) if first is not None else "invalid"
    return (
        f"Tool {tool.name} returned structuredOutput that does not match its schema: "
        f"{path}: {detail}"
    )


def _instance_path(error: Any) -> str:
    """把 jsonschema 的 ``absolute_path`` 转成上游那种 ``a.b.c``（``tool.ts:585-586``）。

    上游写的是 ``instancePath.replace(/^\\//, "").replace(/\\//g, ".")``——
    也就是把 ``/a/b`` 变成 ``a.b``。这里**先构造同样的 ``/a/b``** 再套同一个变换，
    保证边界情形（根、数组下标）逐字一致。
    """
    if error is None:
        return ""
    pointer = "/" + "/".join(str(part) for part in error.absolute_path)
    if pointer == "/":
        return ""
    return pointer[1:].replace("/", ".")


async def append_tool_result(
    tx: Any,
    conversation_id: int,
    call: ToolCall,
    result: ToolExecutionResult[Any],
    meta: dict[str, Any],
) -> Any:
    """追加一条 ``pi.tool-result`` 条目（``tool.ts:836-860``）。

    内容**以渲染后的说明结尾**，所以存下来的消息**正是模型看到的那条**；
    ``data`` 保留结构化那份列表。结果的用量在**同一次提交里**计入 ``pi.usage``。
    """
    timestamp = meta["timestamp"]
    duration_ms = meta.get("durationMs")
    diagnostics = list(result.diagnostics or ())
    content: list[Any] = list(result.output or ())
    if diagnostics:
        content.append(TextContent(type="text", text=render_diagnostics(diagnostics)))
    fields: dict[str, Any] = {
        "role": "toolResult",
        # ⚠️ `call` 是**从存储读回的 JSON dict**（不是对象）——用 `_field` 两种都吃（**R26**）。
        # 我第一版按属性读，端到端跑到这里才炸：`'dict' object has no attribute 'id'`。
        "toolCallId": _field(call, "id"),
        "toolName": _field(call, "name"),
        "content": content,
        "isError": bool(result.is_error),
        "timestamp": timestamp,
    }
    if result.details is not None:
        fields["details"] = result.details
    if result.usage is not None:
        fields["usage"] = result.usage
    if duration_ms is not None:
        fields["durationMs"] = duration_ms
    # ⚠️ **严格 JSON**：存储要序列化，而 pydantic 对象不是 JSON（R24 同族）。
    # **内存后端不会报**（它只 deepcopy），只有 sqlite 的 `json.dumps` 会抛——
    # 这两个后端的参数化测试正是唯一能抓到它的地方。
    #
    # ⚠️ `exclude_none=True` 对应上游那些**条件展开**（``...(x === undefined ? {} : {x})``）：
    # pydantic 的 `model_dump()` 会把未设的可选字段写成 `None`，而上游是**整个键省掉**。
    # 本消息里所有可选字段的 `None` 都表示「未设」，所以二者等价。
    message = ToolResultMessage.model_construct(**fields).model_dump(exclude_none=True)
    if result.usage is not None:
        await record_usage(tx, conversation_id, "tools", _field(call, "name"), result.usage)
    # ⚠️ `data` 会被序列化进存储，所以 diagnostics 必须是**严格 JSON**（R24 同族）。
    # ⚠️ 本包 `append_entry` 的参数顺序是 ``(conversation_id, value, token)``——
    # **不是**上游的 ``(token, conversationId, value)``；令牌是**第三可选参数**（偏离 #48）。
    return await tx.append_entry(
        conversation_id,
        {
            "model": [message],
            "data": {"diagnostics": [diagnostic_json(d) for d in diagnostics]},
        },
        ToolResultEntry,
    )


def replace_result(result: ToolExecutionResult[Any], **changes: Any) -> ToolExecutionResult[Any]:
    """``{...result, ...changes}`` 的对应物（``ToolExecutionResult`` 是冻结 dataclass）。"""
    return dataclasses.replace(result, **changes)


def without_structured_output(result: ToolExecutionResult[Any]) -> ToolExecutionResult[Any]:
    """``const { structuredOutput: _dropped, ...rest } = final`` 的对应物（``tool.ts:527``）。"""
    return dataclasses.replace(result, structured_output=None)


def structured_diagnostics(result: ToolExecutionResult[Any]) -> list[ToolDiagnostic]:
    """一条结果自己的说明（上游 ``result.diagnostics ?? []``）。"""
    return list(result.diagnostics or ())


# ── 内部小工具 ────────────────────────────────────────────────────────────────────────


def _json_text(value: Any) -> str:
    """``JSON.stringify(value ?? null)``——**JS 语义**（``R30``：`true`/`null`，不是 `True`/`None`）。"""
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _field(message: Any, name: str, default: Any = None) -> Any:
    """取一条消息/内容的字段，**同时**接受 JSON dict 与 pydantic 模型（**R26**）。

    条目里的消息是**从存储读回的 JSON**，而刚构造的是 pydantic 对象——两者同一条流水线。
    """
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _is_text(item: Any) -> bool:
    return getattr(item, "type", None) == "text"


def _severity(diagnostic: Any) -> Any:
    return diagnostic["severity"] if isinstance(diagnostic, dict) else diagnostic.severity


def _message(diagnostic: Any) -> str:
    raw = diagnostic["message"] if isinstance(diagnostic, dict) else diagnostic.message
    return str(raw)


# --------------------------------------------------------------------------------------
# Slice B（第一批）：不依赖 `ToolTask` 的四个助手（``tool.ts:209-234, 441-556``）
# --------------------------------------------------------------------------------------


async def resolve_tool(runtime: Any, input: dict[str, Any], name: str, context: Any) -> Any:
    """被调用的工具（``tool.ts:209-218``）。

    ⚠️ **两个来源不同**：

    * **模型发起**的调用 → 从 ``agent.tools``（**提供给模型**的那些）里找；
    * **嵌套**调用 → 从 ``agent.callable``（工具可以调用的那些）里找。

    混用会让一个「没提供给模型」的工具经由嵌套路径**被模型间接触发**——
    而它本该是 ``tools`` 那个集合的专属。
    """
    agent = await runtime.agent(context)
    pool = agent.tools if input["kind"] == "model" else agent.callable
    for tool in pool:
        if tool.name == name:
            return tool
    return None


async def read_call(runtime: Any, input: dict[str, Any], context: Any) -> Any:
    """这次调用（``tool.ts:220-233``）。

    ⚠️ **嵌套调用的 call 在 input 里**（没有条目持有它），而**模型发起**的要到助手条目里找。
    嵌套的那条会被钩子看到，**并带上它的父**——所以这里给它补上 ``parent``。
    """
    if input["kind"] == "nested":
        return {
            **input["call"],
            "parent": {"taskId": input["parent"], "callId": input["parentCallId"]},
        }
    entry = await runtime.entry(input["assistant"], context)
    messages = (entry or {}).get("model") or []
    message = messages[0] if messages else None
    if message is not None and _field(message, "role") == "assistant":
        for content in _field(message, "content") or []:
            if _field(content, "type") == "toolCall" and _field(content, "id") == input["callId"]:
                return content
    raise RuntimeError(f"Entry {input['assistant']} has no tool call {input['callId']}")


async def final_result(
    runtime: Any,
    input: dict[str, Any],
    call: Any,
    tool: Any,
    result: ToolExecutionResult[Any],
    reported: Any,
    context: Any,
) -> ToolExecutionResult[Any]:
    """**结算后的结果**（``tool.ts:497-556``）。

    顺序即语义：工具的结果 → ``afterTool`` 钩子 → 截断说明 → 给内容定界 →
    **校验** ``structuredOutput`` → 最后是 **Harness 自己的说明**。

    ⚠️ **``structuredOutput`` 违约的处置按调用种类不同**（这一条最容易写错）：

    * **嵌套调用** → 变成一个**错误结果**（``is_error=True``），并把违约记成一条说明
      ——因为**程序**读的就是那个值，给了错的值还不如明确报错；
    * **模型发起** → **只丢掉那个值**，调用照旧成立——**模型根本看不到 ``structuredOutput``**，
      所以对它来说没有违约；**违约报告给宿主**。
    """
    harness: list[ToolDiagnostic] = []
    retained = None if result.output is not None else reported.output.snapshot()
    output: list[Any] = (
        list(result.output or ())
        if retained is None
        else ([] if retained.text == "" else [TextContent(type="text", text=retained.text)])
    )
    final = replace_result(
        result,
        output=output,
        details=reported.details if result.details is None else result.details,
        diagnostics=tuple([*reported.diagnostics, *(result.diagnostics or ())]),
    )

    async def run_after_hook(hook: Any) -> None:
        nonlocal final
        decision = hook(call, final, runtime, context)
        if hasattr(decision, "__await__"):
            decision = await decision
        final = decision if decision is not None else final

    await runtime.hooks.each("afterTool", run_after_hook)
    # ⚠️ 保留输出的**截断说明只在 ``afterTool`` 没换掉那段输出时才加**。
    if retained is not None and retained.dropped_bytes > 0 and final.output == output:
        harness.append(
            truncated(
                {"dropped_bytes": retained.dropped_bytes, "dropped_lines": retained.dropped_lines},
                reported.limits["retain"],
            )
        )
    bounded = bound_content(list(final.output or ()), reported.limits)
    if bounded["dropped_bytes"] > 0:
        harness.append(
            truncated(
                {
                    "dropped_bytes": bounded["dropped_bytes"],
                    "dropped_lines": bounded["dropped_lines"],
                },
                reported.limits["retain"],
            )
        )
    broken = structured_output_error(tool, final)
    if broken is not None:
        rest = without_structured_output(final)
        if input["kind"] == "nested":
            final = replace_result(rest, is_error=True)
            harness.append(tool_diagnostic("invalid_structured_output", broken))
        else:
            # ⚠️ **模型从不看 structuredOutput**，所以它的调用照旧成立；**宿主**得知这个工具坏了。
            final = rest
            runtime.report(RuntimeError(broken))
    # 程序拿到**工具自己的值**：没有 schema 时就是它的输出。模型只读 ``output``，所以模型发起的调用两者都跳过。
    if input["kind"] == "nested" and tool.structured_output_schema is None:
        final = replace_result(final, structured_output=output_value(bounded["content"]))
    # ⚠️ 一条**有 schema 的错误结果**若没带 structuredOutput，它的文本只在 ``output`` 里，
    # 而**程序从不读 output**——所以把它变成一条 error 说明（除非已经有一条说了哪里不对）。
    if (
        input["kind"] == "nested"
        and tool.structured_output_schema is not None
        and final.is_error is True
        and final.structured_output is None
        and not any(d.severity == "error" for d in [*(final.diagnostics or ()), *harness])
    ):
        text = "\n".join(
            item.text for item in bounded["content"] if getattr(item, "type", None) == "text"
        )
        if text != "":
            harness.append(tool_diagnostic("tool_error", text))
    return replace_result(
        final, output=bounded["content"], diagnostics=tuple([*(final.diagnostics or ()), *harness])
    )


def publish_progress(runtime: Any, reported: Any, enabled: bool, context: Any) -> Any:
    """把工具报的东西**节流地**提交进它的槽位（``tool.ts:441-496``）。

    ⚠️ 三道关键设计（少一道就会让每次提交都写整段输出）：

    1. **``output`` 整体赋成一个字符串字段**——chord 会把它 diff 成「追加」或
       「裁剪 + 追加」（滑动尾窗）；**替换整个槽位对象会把整段窗口每次都记下来**；
    2. **``details`` 逐叶 diff**（``assign_json``），不是整体替换；
    3. **``diagnostics`` 只追加新的那些**（按上次写的条数切片）。

    返回的 ``bytes`` 是本次提交的**新增字节数**——chord 的 ``Progress`` 用它决定是否值得再等。
    """
    from pi_chord.output import Progress

    written: dict[str, Any] = {"text": "", "details": None, "diagnostics": 0}

    async def write() -> int:
        if not enabled:
            return 0
        # ⚠️ **同步捕获一切**：工具在提交飞行期间还会继续报。
        snapshot = reported.output.snapshot()
        current: dict[str, Any] = {
            "text": str(snapshot.text),
            "details": reported.details,
            "diagnostics": len(reported.diagnostics),
        }
        added = reported.diagnostics[written["diagnostics"] : current["diagnostics"]]
        details_changed = current["details"] != written["details"]
        # ⚠️ 写成什么，由 chord 对**字符串**的 diff 决定：一次追加、一次裁剪+追加，
        # 或者（当有界重叠搜索找不到共同部分时）整段窗口。
        nbytes = 0
        previous_text: str = written["text"]
        if snapshot.text != previous_text:
            if snapshot.text.startswith(previous_text):
                shared = len(previous_text)
            else:
                from pi_chord.delta import overlap

                shared = overlap(previous_text, str(snapshot.text), 65_536)
            nbytes += utf8_byte_length(snapshot.text[shared:])
        if details_changed:
            nbytes += utf8_byte_length(_json_text(current["details"]))
        if added:
            nbytes += utf8_byte_length(_json_text([diagnostic_json(d) for d in added]))

        async def change(tx: Any, _current: Any = None) -> None:
            live = await tx.doc(LiveDoc, runtime.conversation_id)
            slot = tool_slot(live, runtime.task_id)
            if slot is None:
                return None
            # REMINDER：`output` **赋成一个字符串字段**。chord 会把它 diff 成追加、
            # 或（滑动尾窗时）裁剪 + 追加；**替换整个槽位对象会把整段窗口每次都记下来**。
            if (slot.get("output") or "") != snapshot.text:
                slot["output"] = snapshot.text
            if snapshot.dropped_bytes > 0:
                slot["droppedBytes"] = snapshot.dropped_bytes
            if snapshot.dropped_lines > 0:
                slot["droppedLines"] = snapshot.dropped_lines
            # ⚠️ **逐叶 diff ``details``**、**只追加新的 ``diagnostics``**——每次提交只写变化的部分。
            if details_changed and current["details"] is not None:
                assign_json(slot, "details", current["details"])
            if added:
                if slot.get("diagnostics") is None:
                    slot["diagnostics"] = []
                for diagnostic in added:
                    slot["diagnostics"].append(diagnostic_json(diagnostic))
            return None

        await runtime.commit(change, context)
        written.update(current)
        return nbytes

    def on_error(error: BaseException) -> None:
        # ⚠️ 中止标记或关闭**之后**的拒绝是预期的；已提交的状态仍然一致。
        if not runtime.signal.aborted:
            runtime.report(error)

    return Progress(write, on_error, runtime.settings["progress"]["outputIntervalMs"])


# --------------------------------------------------------------------------------------
# Slice B（第二批）：闭环的四件 —— settle / start_nested_call / run / ToolTask
# --------------------------------------------------------------------------------------


async def settle(
    runtime: Any,
    input: dict[str, Any],
    call: Any,
    ending: dict[str, Any],
    build: Any,
    context: Any,
    duration_ms: float | None = None,
) -> None:
    """提交工具的**终局状态**，并把它的槽位标记 done（``tool.ts:589-655``）。

    ⚠️ **先中止它留下的每一个嵌套调用、再等它们**——
    这样它们会在**自己下面的槽位离开 ``pi.live`` 之前**报进各自的槽位。

    ⚠️ 两种终局形状**不同**：

    * **模型发起** → 追加一条结果条目，以**条目 ID** 结束；
    * **嵌套** → 把结果写进**调用者的 ``NestedCallDoc``**、记下用量，以**一个小回执**结束
      （回执小、结果随调用者一起退役）。
    """
    nested_ids = [
        record["id"]
        for record in await runtime.owned_tasks(context)
        if record["kind"] == ToolTask.definition["name"]
        and (record.get("input") or {}).get("kind") == "nested"
    ]
    # ⚠️ 在**结算这次提交之前**读它：`ended` 之后不会再有准入，而在途的已经被等过了。
    made_nested = await runtime.memo(MADE_NESTED_CALLS, context) is True
    # ⚠️ **先把它们全部标记，再等任何一个**——其中一个可能一直跑到兄弟被取消为止。
    for id in nested_ids:
        await runtime.abort_owned(id, context)

    async def change(tx: Any, _current: Any = None) -> Any:
        # ⚠️ 收 `(tx, current)` 两个参数——**JS 容忍回调少写参数，Python 不容忍**。
        # 返回类型是 `Any`：它交出的是**任务的下一个状态**（`{"status": "terminal", ...}`），
        # 而不是 None——`runtime.commit` 会把它写下去。
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        slot = tool_slot(live, runtime.task_id)
        result = build(slot)
        if input["kind"] == "nested":
            nested = nested_result(runtime.task_id, result, duration_ms)
            if nested.get("usage") is not None:
                await record_usage(
                    tx, runtime.conversation_id, "tools", call["name"], nested["usage"]
                )
            # ⚠️ 调用者在它拥有的嵌套调用之前**永远不会终局**，所以它的文档是开着的；
            # 里面出现另一个任务是 **bug，不是竞态**。
            state = await tx.doc(NestedCallDoc, input["parent"], input["key"], None)
            if state.get("taskId") != runtime.task_id:
                raise RuntimeError(f"Nested call {call['id']} is not in its caller's index")
            state["result"] = nested
            if slot is not None and "parentTaskId" in slot:
                finish_slot(slot)
                slot["summary"] = summary_of(nested, list(result.output or ()))
            settled: dict[str, Any] = {"kind": "nested"}
        else:
            entry = await append_tool_result(
                tx,
                runtime.conversation_id,
                call,
                result,
                {"timestamp": runtime.now(), "durationMs": duration_ms},
            )
            if slot is not None and "parentTaskId" not in slot:
                finish_slot(slot)
                slot["entry"] = entry["id"]
            # ⚠️ 工具可以自由构造 control 对象；把值为 `None` 的键**在构造处省掉**（R23）。
            settled = {"kind": "model", "entryId": entry["id"]}
            if result.control is not None and ending["status"] == "completed":
                settled["control"] = {
                    k: v for k, v in _as_dict(result.control).items() if v is not None
                }
        # ⚠️ 一个**任何一次尝试里都没做过嵌套调用**的调用，下面没有槽位；
        # 跳过这次扫描能让许多叶子保持线性。**在读之前提交**：`ended` 之后不再准入。
        if made_nested:
            remove_nested_slots(live, runtime.task_id)
        if ending["status"] == "aborted":
            return {"status": "terminal", "outcome": {"status": "aborted", "result": settled}}
        if ending["status"] == "failed":
            return {
                "status": "terminal",
                "outcome": {
                    "status": "failed",
                    "error": {"message": ending["message"]},
                    "result": settled,
                },
            }
        return {"status": "terminal", "outcome": {"status": "completed", "result": settled}}

    await runtime.commit(change, context)


def _as_dict(value: Any) -> dict[str, Any]:
    """一个 control/映射值的 dict 视图。"""
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


async def start_nested_call(
    runtime: Any,
    parent_call_id: str,
    name: str,
    args: dict[str, Any],
    options: dict[str, Any],
    context: Any,
) -> int:
    """在**一次提交里**创建嵌套调用 ``key``：它的工具任务、它的槽位、它的 ``NestedCallDoc``
    （``tool.ts:679-743``）。

    ⚠️ **已经做过的 key**：若它指的是**同一个工具、同样的参数**，返回**那次调用已有的任务**；
    否则**报错**——同一个 key 复用在另一个工具上会让调用者读到**别人的结果**。
    """
    key = options["key"]
    call = {
        "type": "toolCall",
        "id": f"{parent_call_id}/{key}",
        "name": name,
        "arguments": copy_json(args),
    }
    created: dict[str, Any] = {}

    async def change(tx: Any, _current: Any = None) -> None:
        # 打开的，或者为还没做过的 key 建一个空的。
        state = await tx.doc(NestedCallDoc, runtime.task_id, key, None)
        existing = state.get("taskId")
        if existing is not None:
            record = await tx.task(existing)
            input_ = None if record is None else record.get("input")
            same = (
                input_ is not None
                and input_.get("kind") == "nested"
                and (input_.get("call") or {}).get("name") == name
                and json_equal((input_["call"] or {}).get("arguments"), call["arguments"])
            )
            if not same:
                raise RuntimeError(
                    f"Nested call {call['id']} was already made with another tool or other arguments"
                )
            created["id"] = existing
            return
        input_ = {
            "kind": "nested",
            "parent": runtime.task_id,
            "parentCallId": parent_call_id,
            "key": key,
            "call": call,
        }
        if options.get("progress") is False:
            input_["progress"] = False
        created["id"] = await tx.create_task(
            ToolTask,
            input_,
            {
                "ownership": {"kind": "task", "taskId": runtime.task_id},
                "abandonOnRestart": options["abandonOnRestart"],
            },
        )
        state["taskId"] = created["id"]
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        if live.get("nestedTools") is None:
            live["nestedTools"] = []
        live["nestedTools"].append(
            {
                "callId": call["id"],
                "parentCallId": parent_call_id,
                "parentTaskId": runtime.task_id,
                "name": name,
                "taskId": created["id"],
                "arguments": call["arguments"],
                "status": "pending",
            }
        )

    await runtime.commit(change, context)
    return int(created["id"])


async def run(
    runtime: Any,
    input: dict[str, Any],
    call: Any,
    tool: ToolRegistration[Any],
    args: dict[str, Any],
    context: Any,
) -> None:
    """用解析出来的实现**执行**，然后结算它的结果（``tool.ts:269-440``）。

    ⚠️ 三处关键设计：

    1. **``admissions`` 要等**：本次调用起过的嵌套调用准入，清理时**等它们**
       ——不然清理看不到每一个嵌套调用（§1 会漏掉最后几个）；
    2. **``ended`` 闸门**：``assert_live`` 让**结算之后**的任何 ``api`` 调用**立刻抛**
       ——工具在结果已经写下之后还在报进度，那些进度**不该再进槽位**；
    3. **工具抛出时**：结果变成一条 ``tool_error`` 说明，而 ``ending`` 变成 **``failed``**
       ——那会**给这次调用拥有的东西记下取消意图**，于是它们被中止。
       （**异常的环境构建也一样**——``env`` 在 ``try`` 里。）
    """
    # ⚠️ `ToolOutputLimits` 是 **dataclass**，不是 dict——按**属性**读（我自己的规则：
    # "先读被调方/被读方的那一面"，这里是"被读方"）。
    declared = tool.output_limits
    limits: dict[str, Any] = {
        "maxBytes": None if declared is None else declared.max_bytes,
        "maxLines": None if declared is None else declared.max_lines,
        "retain": "head" if declared is None else (declared.retain or "head"),
    }
    from pi_chord.output import OutputBuffer, OutputLimits

    chord_limits = OutputLimits(
        max_bytes=limits["maxBytes"] or DEFAULT_MAX_BYTES,
        max_lines=limits["maxLines"] or DEFAULT_MAX_LINES,
        retain=limits["retain"],
    )
    reported: dict[str, Any] = {
        "output": OutputBuffer(chord_limits),
        "limits": chord_limits,
        "diagnostics": [],
        "details": None,
    }
    streams = input["kind"] == "model" or input.get("progress") is not False
    progress = publish_progress(runtime, _ReportedView(reported), streams, context)
    admissions: list[Any] = []
    # ⚠️ 一个**能重跑**的调用者会在重启后重新依附到它的嵌套调用与子任务；别的调用者**永不恢复**，
    # 所以它们被抛弃（`TaskOptions.abandonOnRestart`）。
    resumes = tool.replay == "safe"
    sequence = 0
    made_nested_memo: Any = None
    ended = False

    def assert_live() -> None:
        if ended:
            raise RuntimeError(f"Tool call {call['id']} has settled")

    async def output(chunk: Any, skipped: Any = None) -> None:
        assert_live()
        if reported["output"].push(chunk, skipped):
            progress.mark()

    def retained_output() -> dict[str, Any]:
        assert_live()
        retained = reported["output"].snapshot()
        return {"text": retained.text, "truncated": retained.dropped_bytes > 0}

    def diagnostic(value: Any) -> None:
        assert_live()
        reported["diagnostics"].append(diagnostic_json(value))
        progress.mark()

    async def details(value: Any, details_context: Any) -> None:
        assert_live()
        signal = getattr(details_context, "abort_signal", None)
        if signal is not None:
            signal.throw_if_aborted()
        reported["details"] = copy_json(value)
        committed = progress.mark_and_wait()
        # ⚠️ 取消这次等待**留下**那次更新；提交自己的结果照常被观察。
        committed.add_done_callback(lambda _f: None)
        if signal is not None:
            await await_with_context(committed, details_context)

    async def api_commit(change: Any, commit_context: Any) -> Any:
        captured: dict[str, Any] = {}

        async def on_line(tx: Any, _current: Any = None) -> None:
            captured["result"] = await change(tx)
            return None

        await runtime.commit(on_line, commit_context)
        return captured.get("result")

    async def create_task(
        task: Any, task_input: Any, options: dict[str, Any], task_context: Any
    ) -> Any:
        child = dict(options)
        if options.get("ownership", {}).get("kind") == "task" and not resumes:
            child = {"abandonOnRestart": True, **options}
        captured: dict[str, Any] = {}

        async def on_line(tx: Any, _current: Any = None) -> None:
            captured["id"] = await tx.create_task(task, task_input, child)
            return None

        await runtime.commit(on_line, task_context)
        return captured["id"]

    async def execute_tool(
        name: str,
        tool_args: dict[str, Any],
        call_context: Any,
        options: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal made_nested_memo, sequence
        options = options or {}
        assert_live()
        sequence += 1
        key = options.get("key") or str(sequence)
        if options.get("key") is not None:
            check_key(options["key"])
        # ⚠️ **备忘先，每次尝试一次**；失败的那个由下一次调用再试。
        if made_nested_memo is None:
            made_nested_memo = asyncio.ensure_future(
                runtime.memo(MADE_NESTED_CALLS, call_context, True)
            )
            made_nested_memo.add_done_callback(lambda _f: None)
        # ⚠️ **现在就登记**，好让清理等它。
        admission = asyncio.ensure_future(
            _admit_after(
                made_nested_memo,
                runtime,
                call["id"],
                name,
                tool_args,
                {"key": key, "progress": options.get("progress"), "abandonOnRestart": not resumes},
                call_context,
            )
        )
        admissions.append(admission)
        id = await admission
        settled = await runtime.wait_for_task(id, call_context)
        state = await runtime.snapshot(NestedCallDoc, runtime.task_id, key, call_context)
        stored = None if state is None else state.get("result")
        # ⚠️ **一份工具可以改的副本**；被调度器出错或孤立的嵌套调用什么都没存。
        if stored is None:
            return fallback_result(id, name, settled["state"]["outcome"])
        return copy_json(stored)

    api: dict[str, Any] = {
        "taskId": runtime.task_id,
        "conversationId": runtime.conversation_id,
        "callId": call["id"],
        "registry": runtime.registry,
        "agent": runtime.agent,
        "models": runtime.models,
        "output": output,
        "outputWindow": (
            {
                "maxBytes": limits["maxBytes"],
                "maxLines": limits["maxLines"],
                "minIntervalMs": runtime.settings["progress"]["outputIntervalMs"],
                "bytesPerSecond": PROGRESS_BYTES_PER_SECOND,
            }
            if limits["retain"] == "tail"
            else None
        ),
        "retainedOutput": retained_output,
        "diagnostic": diagnostic,
        "details": details,
        "commit": api_commit,
        "memo": runtime.memo,
        "createTask": create_task,
        "getTask": runtime.get_task,
        "waitForTask": runtime.wait_for_task,
        "conversation": runtime.conversation,
        "snapshot": runtime.snapshot,
        "snapshotAsOf": runtime.snapshot_as_of,
        "watchDoc": runtime.watch_doc,
        "executeTool": execute_tool,
    }

    result: ToolExecutionResult[Any] = ToolExecutionResult[Any]()
    ending: dict[str, Any] = COMPLETED
    duration_ms: float | None = None
    try:
        env = await runtime.env(context)
        started = time.monotonic()
        try:
            # ⚠️ `api` 是个 dict（本端把 `ToolExecutionApi` 的字段收在字典里传给工具）；
            # 用 cast 过掉那个名义类型。
            called: Any = tool.execute(args, cast(Any, {**api, "env": env}), context)
            if inspect.isawaitable(called):
                called = await called
            result = cast("ToolExecutionResult[Any]", called)
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
    except BaseException as error:  # noqa: BLE001
        if runtime.signal.aborted:
            ended = True
            for waiter in progress.stop():
                _reject_future(waiter, error)
            raise
        result = ToolExecutionResult[Any](
            is_error=True, diagnostics=(tool_diagnostic("tool_error", error_message(error)),)
        )
        # ⚠️ 抛出（来自 `execute()` **或**环境构建）让任务以 `failed` 结束——那**取消这次调用拥有的东西**；
        # 它不再监督它们了。错误文本已经在结果里。
        ending = {"status": "failed", "message": f"Tool {call['name']} threw"}
    ended = True
    # ⚠️ `ended` 之后不会再有准入；让在途的那些提交完，好让结算能中止并列出它们的调用。
    if admissions:
        await asyncio.gather(*admissions, return_exceptions=True)
    reported["output"].end()
    pending = progress.stop()
    try:
        settled_result = await final_result(
            runtime, input, call, tool, result, _ReportedView(reported), context
        )
        await settle(
            runtime, input, call, ending, lambda _slot: settled_result, context, duration_ms
        )
    except BaseException as error:  # noqa: BLE001
        for waiter in pending:
            _reject_future(waiter, error)
        raise
    for waiter in pending:
        _resolve_future(waiter)


async def _admit_after(
    memo: Any,
    runtime: Any,
    parent_call_id: str,
    name: str,
    args: Any,
    options: dict[str, Any],
    context: Any,
) -> Any:
    """等那次备忘提交完，再准入嵌套调用（``tool.ts:376-390`` 的那段 ``.then``）。"""
    try:
        await memo
    except BaseException:
        raise
    return await start_nested_call(runtime, parent_call_id, name, args, options, context)


class _ReportedView:
    """把 `run` 的 `reported` dict 暴露成 `final_result`/`publish_progress` 要的属性面。"""

    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    @property
    def output(self) -> Any:
        return self._d["output"]

    @property
    def limits(self) -> Any:
        return self._d["limits"]

    @property
    def diagnostics(self) -> list[Any]:
        return cast("list[Any]", self._d["diagnostics"])

    @property
    def details(self) -> Any:
        return self._d["details"]


def _reject_future(future: Any, error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


def _resolve_future(future: Any) -> None:
    if not future.done():
        future.set_result(None)


#: 内建的工具任务（``tool.ts:119-208``）。
#:
#: 它在一个 ``call`` 处理器里**解析、校验、跑 ``beforeTool``、记录意图、执行、跑 ``afterTool``、
#: 并结算结果**——**没有任何东西把「解析」与「结算」分开**。
#: ``execute`` **只由恢复**到达，并且施加重放规则。
#:
#: ⚠️ **版本 2**：版本 1 只有模型发起的调用，所以 ``migrate`` 给老输入补上 ``kind: "model"``。
async def _tool_call_phase(task: Any, runtime: Any, context: Any) -> None:
    """``call`` 阶段（``tool.ts:127-176``）。

    顺序即语义，**每一步失败都走 ``settle``**（而不是抛）——因为**结果要留下来**：
    工具不可用、参数无效、被钩子挡下，三者都是**这次调用的结果**，不是基础设施故障。
    """
    input_ = task["input"]
    call = await read_call(runtime, input_, context)
    tool = await resolve_tool(runtime, input_, call["name"], context)
    if tool is None:
        error = harness_error("tool_unavailable", f"Tool {call['name']} is not available")
        await settle(runtime, input_, call, COMPLETED, lambda _slot: error, context)
        return
    prepared = prepare(tool, dict(call.get("arguments") or {}))
    checked = prepared if "error" in prepared else validate(tool, call, prepared["args"])
    if "error" in checked:
        message = checked["error"]
        await settle(runtime, input_, call, COMPLETED, lambda _slot: invalid(message), context)
        return
    args = checked["args"]
    block: str | None = None

    async def before_tool(hook: Any) -> None:
        nonlocal args, block
        # ⚠️ **第一个 block 生效、其余不再跑**——这是 `HookRunner.each` 的语义
        # （普通抛出报告后继续；这里用 early-return 让后续钩子看到已经决定的 block）。
        if block is not None:
            return
        try:
            decision = hook({**call, "arguments": args}, runtime, context)
            if inspect.isawaitable(decision):
                decision = await decision
            if decision is not None and decision.get("block") is not None:
                block = decision["block"]
            elif decision is not None and decision.get("arguments") is not None:
                args = decision["arguments"]
        except BaseException as error:  # noqa: BLE001
            if runtime.signal.aborted:
                raise
            block = error_message(error)

    await runtime.hooks.each("beforeTool", before_tool)
    if block is not None:
        blocked = harness_error("blocked", f"Tool call blocked: {block}")
        await settle(runtime, input_, call, COMPLETED, lambda _slot: blocked, context)
        return
    validated = validate(tool, call, args)
    if "error" in validated:
        message = validated["error"]
        await settle(runtime, input_, call, COMPLETED, lambda _slot: invalid(message), context)
        return
    # ⚠️ **严格 JSON**：修复可能把可选属性设成 `undefined`，而槽位与检查点**装不下**它。
    final = cast(dict[str, Any], copy_json(validated["args"]))

    async def change(tx: Any, _current: Any = None) -> Any:
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        slot = tool_slot(live, runtime.task_id)
        if slot is not None:
            slot["status"] = "running"
        # ⚠️ 嵌套槽位显示**这次调用实际运行的参数**（修复、钩子、强制转换都可能改过它）。
        # 与 input 比，而不是读草稿——读草稿会**跟踪每一个叶子**。
        changed = input_["kind"] == "nested" and not json_equal(
            (input_.get("call") or {}).get("arguments"), final
        )
        if changed and slot is not None and "parentTaskId" in slot:
            slot["arguments"] = final
        intent = {"phase": "execute", "arguments": final, "replay": tool.replay or "unsafe"}
        return {"status": "running", "checkpoint": intent}

    await runtime.commit(change, context)
    await run(runtime, input_, call, tool, final, context)


async def _tool_execute_phase(task: Any, runtime: Any, context: Any) -> None:
    """``execute`` 阶段——**恢复之后**（``tool.ts:177-197``）。

    ⚠️ **只有存储的与当前的重放策略都说 ``safe`` 才重跑**：
    两者任一为 ``unsafe`` 时重跑会让一次**可能已经部分执行**的调用**再执行一遍**。
    """
    checkpoint = task["state"]["checkpoint"]
    args = checkpoint["arguments"]
    replay = checkpoint["replay"]
    input_ = task["input"]
    call = await read_call(runtime, input_, context)
    tool = await resolve_tool(runtime, input_, call["name"], context)
    if replay == "safe" and tool is not None and tool.replay == "safe":
        # ⚠️ **重跑从头报告**——把被打断的那次尝试发布过的东西清掉。
        async def change(tx: Any, _current: Any = None) -> None:
            live = await tx.doc(LiveDoc, runtime.conversation_id)
            slot = tool_slot(live, runtime.task_id)
            if slot is not None:
                clear_progress(slot)
            return None

        await runtime.commit(change, context)
        await run(runtime, input_, call, tool, args, context)
        return
    message = f"Tool {call['name']} was interrupted and may have partially run"
    # ⚠️ `failed` **记下取消意图**，于是这次调用拥有的、**没人监督**的对话会被中止。
    ending = {"status": "failed", "message": message}
    await settle(
        runtime, input_, call, ending, lambda slot: from_slot(slot, "interrupted", message), context
    )


async def _tool_abort_phase(task: Any, runtime: Any, context: Any) -> None:
    """``abort`` 阶段（``tool.ts:198-207``）。

    ⚠️ **三种文案**，由「为什么被中止」决定：

    * 普通的 **``aborted``**；
    * 重启时**已经进入 ``execute``** → **``interrupted``**（**它可能已经部分跑过**）；
    * 重启时**还在 ``call``** → **``abandoned``**（**它没开始**）。

    三种的差别是**给用户的信息量**：说「被中止」而不说「可能已经跑过」，
    会让人以为重试是安全的。
    """
    call = await read_call(runtime, task["input"], context)
    if task.get("abortReason") != "restart":
        code, message = "aborted", f"Tool {call['name']} was aborted"
    elif task["state"]["checkpoint"]["phase"] == "execute":
        code = "interrupted"
        message = f"Tool {call['name']} was interrupted by a restart and may have partially run"
    else:
        code = "abandoned"
        message = f"Tool {call['name']} was not started: its caller ended with a restart"
    ending = {"status": "aborted"}
    await settle(
        runtime, task["input"], call, ending, lambda slot: from_slot(slot, code, message), context
    )


ToolTask = define_task(
    {
        "name": "pi.tool",
        "version": 2,
        "initial": lambda input_=None: {"phase": "call"},
        # 版本 1 只有模型发起的调用。
        "migrate": lambda input_, checkpoint, version=1: {
            "input": {"kind": "model", **(input_ or {})},
            "checkpoint": checkpoint,
        },
        "phases": {
            "call": _tool_call_phase,
            "execute": _tool_execute_phase,
        },
        "abort": _tool_abort_phase,
    }
)

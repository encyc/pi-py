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

import dataclasses
import re
from typing import Any, cast

import jsonschema  # type: ignore[import-untyped]

from pi_ai import TextContent, ToolCall, ToolResultMessage
from pi_chord.json import copy_json
from pi_chord.output import OutputLimits, bound_output
from pi_durable.documents import define_doc_family
from pi_durable.entries import ToolResultEntry
from pi_durable.errors import error_message
from pi_durable.harness.live import NestedToolSummary
from pi_durable.harness.types import (
    NestedToolExecutionResult,
    ToolDiagnostic,
    ToolExecutionResult,
    ToolRegistration,
)
from pi_durable.harness.usage import record_usage

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
        merged = call.model_copy(update={"arguments": args})
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
        "toolCallId": call.id,
        "toolName": call.name,
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
        await record_usage(tx, conversation_id, "tools", call.name, result.usage)
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


def _is_text(item: Any) -> bool:
    return getattr(item, "type", None) == "text"


def _severity(diagnostic: Any) -> Any:
    return diagnostic["severity"] if isinstance(diagnostic, dict) else diagnostic.severity


def _message(diagnostic: Any) -> str:
    raw = diagnostic["message"] if isinstance(diagnostic, dict) else diagnostic.message
    return str(raw)

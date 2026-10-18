"""压缩任务的**叶子层** —— 移植 ``harness/compaction.ts``(464) 的 **Slice A**（约 210 行）。

按**可测性**切（与 `tool.py` 同一取舍）：``CompactionTask`` 的三个 phase 都收
``runtime: TaskRuntime``，而它的**实现在 `scheduler.py``**（关键路径上靠后）。
本片只收**不碰 ``TaskRuntime``** 的部分。

**Slice B**（``CompactionTask`` 本体 + ``place`` / ``place_summary`` / ``complete`` /
``fail_no_model`` / ``create_compaction``）在 `scheduler.py` 到位后做——
其中 ``create_compaction`` 还要 ``submissions.admit_submission``（SCC 的**回边**，届时延迟导入）。

⚠️ 本模块的消息来自 ``view.contributions``，那是**从存储读回的 JSON**（普通 dict）——
按 **R26** 用 :func:`_field` 双形状访问。
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import TypeAdapter

from pi_ai import Message, Usage, is_retryable_assistant_error, retry_delay_ms
from pi_ai.estimate import calculate_context_tokens, estimate_message_tokens
from pi_durable.entries import CompactionEntry
from pi_durable.harness.context import order_tool_results
from pi_durable.harness.live import (
    LiveDoc,
    add_compaction_status,
    compaction_status,
    fail_compaction_status,
    remove_compaction_status,
)
from pi_durable.harness.provider import ensure_provider_session_id
from pi_durable.harness.submissions import admit_submission
from pi_durable.harness.types import ContextView
from pi_durable.harness.usage import record_usage
from pi_durable.tasks import define_task

__all__ = [
    "complete",
    "create_compaction",
    "SUMMARY_PREFIX",
    "SUMMARY_SUFFIX",
    "SUMMARIZATION_PROMPT",
    "SUMMARIZATION_SYSTEM_PROMPT",
    "TOOL_RESULT_MAX_CHARS",
    "CompactionCheckpoint",
    "CompactionInput",
    "SummaryRequest",
    "content_text",
    "estimate_context",
    "is_candidate",
    "select_cut",
    "serialize_conversation",
    "summarized_messages",
    "summary_failure",
    "summary_prompt",
    "summary_text",
    "truncate",
]

#: 一段序列化摘要源里**工具结果文本**的上限（``compaction.ts:62``）。
TOOL_RESULT_MAX_CHARS = 2000

#: 摘要条目的前缀 / 后缀（``compaction.ts:64-66``）。
SUMMARY_PREFIX = "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
SUMMARY_SUFFIX = "\n</summary>"

#: 摘要器的系统提示（``compaction.ts:68-70``）。
SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI assistant, then produce a structured summary following "
    "the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the "
    "conversation. ONLY output the structured summary."
)

#: 摘要器的用户提示（``compaction.ts:72-108``）。
SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work. If the conversation starts with an earlier summary, preserve its information and fold the newer messages into it.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

#: 一次压缩的输入（``compaction.ts:38``）。
CompactionInput = dict[str, Any]

#: **钉住**的摘要请求（``compaction.ts:41-51``）。
SummaryRequest = dict[str, Any]

#: 一次压缩的检查点（``compaction.ts:53-56``）。
CompactionCheckpoint = dict[str, Any]

#: 把存储里的 JSON 消息还原成 pydantic 模型的适配器（见 :func:`_as_message`）。
_MESSAGE_ADAPTER: TypeAdapter[Message] = TypeAdapter(Message)

#: 同上，针对 ``usage``（它也是从存储读回的 JSON dict）。
_USAGE_ADAPTER: TypeAdapter[Usage] = TypeAdapter(Usage)


def _context_tokens(usage: Any) -> int:
    """``calculateContextTokens(usage)``——**先**把 JSON dict 还原成 ``Usage``。

    ⚠️ 与 :func:`_as_message` 同一个理由：``pi_ai`` 的这两个函数要读**属性**。
    """
    if usage is None:
        return 0
    return calculate_context_tokens(
        usage if isinstance(usage, Usage) else _USAGE_ADAPTER.validate_python(usage)
    )


def _as_message(message: Any) -> Any:
    """把一个 **JSON dict** 消息转成 pydantic 模型。

    ⚠️ **R26 同族，而且这次是反向的**：``pi_ai.estimate`` 的两个函数收的是
    **pydantic 消息对象**（它们要读 ``usage`` 的属性），而 ``view.contributions`` 里的消息
    **是从存储读回的 JSON dict**。不转就会报
    ``TypeError: unsupported operand type(s) for *: 'dict' and 'int'``。

    （上一步 ``context.py`` 那边是「我们收 dict」；这里是「下游要对象」——
    **同一个 R26 的两个方向**。跨包传值时，必须服从**对方**的形状。）
    """
    if not isinstance(message, dict):
        return message
    return _MESSAGE_ADAPTER.validate_python(message)


def _field(message: Any, name: str, default: Any = None) -> Any:
    """取一条消息的字段，**同时**接受 JSON dict 与 pydantic 模型（**R26**）。

    ``view.contributions`` 里的消息是**从存储读回的 JSON**，而活上下文里可能是 pydantic 对象——
    两者会混在同一条流水线上。
    """
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def is_candidate(contributions: list[list[Any]], index: int) -> bool:
    """第 ``index`` 个条目能不能当**切点**（``compaction.ts:282-303``）。

    规则：第一个消息是助手 → 可以；不是用户 → 不可以；
    是用户时，要看**它后面、下一条助手之前**有没有「前面那条助手的调用的结果」——
    有的话那些结果属于**切点之前**，所以这个条目不能切。
    """
    block = contributions[index]
    first = block[0] if block else None
    if _field(first, "role") == "assistant":
        return True
    if _field(first, "role") != "user":
        return False
    # 「这条条目之前那条助手」的调用 ID 集合。
    calls: set[str] = set()
    for before in range(index - 1, -1, -1):
        assistant = None
        for message in contributions[before]:
            if _field(message, "role") == "assistant":
                assistant = message
        if assistant is None:
            continue
        calls = {
            _field(content, "id")
            for content in (_field(assistant, "content") or [])
            if _field(content, "type") == "toolCall"
        }
        break
    if not calls:
        return True
    for after in range(index, len(contributions)):
        for position, message in enumerate(contributions[after]):
            role = _field(message, "role")
            if role == "assistant" and (after > index or position > 0):
                return True
            if role == "toolResult" and _field(message, "tool_call_id") in calls:
                return False
    return True


def select_cut(view: ContextView, keep_recent_tokens: float) -> int | None:
    """选一个**旧的**前缀切掉（``compaction.ts:262-280``）。

    从尾往前累计 token，直到留下的**不少于** ``keep_recent_tokens``，
    然后取**不早于**那个位置的最靠前的候选；一个都没有就取最后一个候选。
    最后要求切点**之前**至少有一个非空条目——否则没有东西可压。
    """
    contributions = view["contributions"]
    start = 0 if view["head"] is None else 1
    candidates = [
        index for index in range(start, len(contributions)) if is_candidate(contributions, index)
    ]
    kept = 0
    cut: int | None = None
    for index in range(len(contributions) - 1, start - 1, -1):
        for message in contributions[index]:
            kept += estimate_message_tokens(_as_message(message))
        if kept < keep_recent_tokens:
            continue
        cut = next((c for c in candidates if c >= index), candidates[-1] if candidates else None)
        break
    if cut is None:
        return None
    for index in range(start, cut):
        if contributions[index]:
            return cut
    return None


def summarized_messages(view: ContextView, cut: int) -> list[Any]:
    """``cut`` 之前那些条目的模型消息：**head 标记在最前**，按模型上下文的顺序
    （``compaction.ts:305-307``）。"""
    flat = [message for block in view["contributions"][:cut] for message in block]
    return order_tool_results(flat)


def estimate_context(view: ContextView, extra: list[Any]) -> float:
    """一次「``view`` 后面再跟 ``extra``」的请求有多大（spec §8.3）（``compaction.ts:314-330``）。

    用**head 标记之后最新那条助手**的用量（它那次请求**包含**了标记），
    加上它之后那些消息的估算；一条都没有时，就估算每一条。

    ⚠️ 这是**唯一**能给出真实 token 数的办法：提供方只对**已经发出过**的请求报用量。
    """
    measured: Any = None
    after = view["head"]["id"] if view["head"] is not None else float("-inf")
    index = len(view["entries"]) - 1
    while index >= 0 and measured is None:
        if view["entries"][index]["id"] > after:
            for message in view["contributions"][index]:
                if (
                    _field(message, "role") == "assistant"
                    and _context_tokens(_field(message, "usage")) > 0
                ):
                    measured = message
        index -= 1
    from_index = 0 if measured is None else _last_index_of(view["messages"], measured) + 1
    tokens = 0.0 if measured is None else float(_context_tokens(_field(measured, "usage")))
    for message in view["messages"][from_index:]:
        tokens += estimate_message_tokens(_as_message(message))
    for message in extra:
        # `extra` 是本包自己的参数，所以**两种形状都收**（对自己宽、对下游严）。
        tokens += estimate_message_tokens(_as_message(message))
    return tokens


def _last_index_of(messages: list[Any], target: Any) -> int:
    """``lastIndexOf`` 的对应物——用**身份**比，因为 JSON 往返会产生相等的副本。"""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index] is target or messages[index] == target:
            return index
    return -1


def summary_text(message: Any) -> str | None:
    """一次干净的 ``stop``、有文本、**没有**工具调用的摘要；别的都不是摘要
    （``compaction.ts:332-340``）。"""
    if _field(message, "stop_reason") != "stop":
        return None
    content = _field(message, "content") or []
    if any(_field(item, "type") == "toolCall" for item in content):
        return None
    text = "\n".join(
        _field(item, "text") or "" for item in content if _field(item, "type") == "text"
    )
    text = text.strip()
    return None if text == "" else text


def summary_failure(message: Any) -> str:
    """助手消息为什么**没有**产出摘要（``compaction.ts:342-350``）。"""
    stop = _field(message, "stop_reason")
    if stop in ("error", "aborted"):
        detail = _field(message, "error_message") or stop
        return f"Summarization failed: {detail}"
    if stop == "length":
        return "Summarization hit the token limit; the summary is incomplete"
    content = _field(message, "content") or []
    if any(_field(item, "type") == "toolCall" for item in content):
        return "Summarization attempted to call a tool"
    return "Summarization produced no text"


def summary_prompt(messages: list[Any], instructions: str | None) -> str:
    """摘要器的用户消息：序列化后的对话 + 提示 + 任何附加指示（``compaction.ts:352-356``）。"""
    focus = "" if instructions is None else f"\n\nAdditional focus: {instructions}"
    return (
        f"<conversation>\n{serialize_conversation(messages)}\n</conversation>\n\n"
        f"{SUMMARIZATION_PROMPT}{focus}"
    )


def serialize_conversation(messages: list[Any]) -> str:
    """把消息摊成**纯文本**，好让摘要器**读**它而不是**接着它说**（``compaction.ts:363-388``）。

    **系统消息省略**。**思考也省略**：提供方返回的是真实推理的摘要，
    而重放的思考常常不属于活上下文（与请求不匹配的块会被丢掉），
    带上它会让请求比它要压缩的上下文还大。可见文本、工具调用与工具结果记录了发生过什么。
    """
    parts: list[str] = []
    for message in messages:
        role = _field(message, "role")
        content = _field(message, "content") or []
        if role == "user":
            text = content_text(content)
            if text:
                parts.append(f"[User]: {text}")
        elif role == "assistant":
            texts = [
                _field(item, "text") or "" for item in content if _field(item, "type") == "text"
            ]
            calls = [
                f"{_field(item, 'name')}({_render_arguments(_field(item, 'arguments'))})"
                for item in content
                if _field(item, "type") == "toolCall"
            ]
            if texts:
                joined = "\n".join(texts)
                parts.append(f"[Assistant]: {joined}")
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif role == "toolResult":
            text = content_text(content)
            if text:
                parts.append(f"[Tool result]: {truncate(text, TOOL_RESULT_MAX_CHARS)}")
    return "\n\n".join(parts)


def _render_arguments(arguments: Any) -> str:
    """``Object.entries(args).map(([k,v]) => `${k}=${JSON.stringify(v)}`).join(", ")``
    （``compaction.ts:375-377``）。

    ⚠️ **``JSON.stringify`` 的语义**，不是 Python 的 ``str()``：
    ``True`` 要写成 ``true``、``None`` 写成 ``null``。这与
    :mod:`pi_ai` 里那条 ``String(value)`` 的坑同族（**凡是从 JS 平移来的转换，先问两边语义一样吗**）。
    """
    if not isinstance(arguments, dict):
        return _json_text(arguments)
    return ", ".join(f"{key}={_json_text(value)}" for key, value in arguments.items())


def _json_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def content_text(content: Any) -> str:
    """内容块里的文本（``compaction.ts:390-395``）。"""
    if isinstance(content, str):
        return content
    return "\n".join(
        _field(block, "text") or ""
        for block in content
        if _field(block, "type") == "text" and _field(block, "text") is not None
    )


def truncate(text: str, max_chars: int) -> str:
    """按**字符数**截断，并写明丢了多少（``compaction.ts:397-401``）。"""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[... {len(text) - max_chars} more characters truncated]"


# --------------------------------------------------------------------------------------
# Slice B（工厂）：`create_compaction` —— **两处共用**（`classify` 的溢出分支 +
# `GenerationTask.prepare` 的阈值分支），所以它**必须先于那两块**可用。
# --------------------------------------------------------------------------------------


async def create_compaction(
    tx: Any, conversation_id: int, input_: dict[str, Any], owner: int | None = None
) -> int:
    """建一个压缩任务并**把它的状态挂进 `pi.live`**（``compaction.ts:241-254``）。

    ⚠️ **两个字段由 `owner` 决定，而且它们各自决定一件事**：

    | `owner` | `ownership` | `background` |
    |---|---|---|
    | 给了（生成任务拥有） | `{kind: "task", taskId: owner}` | **False** |
    | 没给（对话拥有） | `{kind: "conversation"}` | ``reason != "manual"`` |

    * **`ownership`**：有主时，**拥有者不会在压缩跑完前终局**，且拥有者被中止时它跟着被中止；
    * **`background`**：没主且**不是手动**时才置真 —— **手动压缩是要等的**（用户点了按钮），
      而阈值/溢出压缩**挂在对话上、后台跑**。

    ⚠️ **`blocking` 与 `fallback`**：`blocking = owner is not None` ——
    有主的那次是**阻塞式**的（生成在等它），没主的是后台的。

    ⚠️ `CompactionTask` 定义在**本文件末尾**（Slice B 本体）。**与 `create_generation` 同一情形**：
    mypy 的错误码是 `name-defined`（这个名字现在还不存在），故挂一句静态说明；
    **运行期只有真被调用时才解析**，所以此时调用它会 `NameError` —— **这是预期的**，
    而**双向验证在 `CompactionTask` 落地后做**。
    """
    ownership: dict[str, Any] = (
        {"kind": "conversation"} if owner is None else {"kind": "task", "taskId": owner}
    )
    background = owner is None and input_["reason"] != "manual"
    task_id = int(
        await tx.create_task(
            # ⚠️ **这处 `type: ignore[name-defined]` 已删** —— `CompactionTask` 现在就在本文件末尾。
            # （mypy 在它落地的那一刻报了 `unused-ignore`，是"会过期的 ignore"第 4 次兑现。）
            CompactionTask,
            input_,
            {
                "ownership": ownership,
                "conversationId": conversation_id,
                "background": background,
            },
        )
    )
    live = await tx.doc(LiveDoc, conversation_id)
    add_compaction_status(
        live,
        {
            "taskId": task_id,
            "reason": input_["reason"],
            "blocking": owner is not None,
            "attempt": 1,
        },
    )
    return task_id


# --------------------------------------------------------------------------------------
# Slice B：`CompactionTask` 本体 + `place` / `place_summary` / `complete` / `fail_no_model`
# --------------------------------------------------------------------------------------


async def place(runtime: Any, first_kept: int, summary: str, context: Any) -> None:
    """把摘要**放进**会话并完成（``compaction.ts:403-410``）。

    ⚠️ 它只是 `place_summary` 的提交外壳 —— **真正的分支在 `place_summary` 里**
    （阻塞式 vs 对话拥有），而那个分支由 `current.owner` 决定。
    """

    async def change(tx: Any, current: Any) -> Any:
        # ⚠️ 上游在 `place` 里**先取** `live` 再传进 `place_summary`；
        # 本端照做（而不是在 `place_summary` 里猜）——**别用"哨兵参数"绕这一步**。
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        return await _place_summary(tx, runtime, current, live, first_kept, summary)

    await runtime.commit(change, context)


async def _place_summary(
    tx: Any,
    runtime: Any,
    current: Any,
    live: Any,
    first_kept: int,
    summary: str,
) -> Any:
    """放摘要条目并完成（spec §8.7）（``compaction.ts:412-449``）。

    ⚠️ **两条路，由 `current.owner` 决定**：

    * **阻塞式**（有主：一次生成在等它）→ **直接追加**条目，结果给 `entry_id`；
    * **对话拥有**（没主）→ **作为一次写提交准入**（`admitSubmission`）——
      空闲时立刻放置，否则在下一个边界，或者结算成 `stale`。

    > ⚠️ **别的任何东西都不能往忙碌的会话里追加** —— 所以每一个非阻塞的摘要都走准入。

    ⚠️ 而且**两条路都先 `remove_compaction_status` 并清掉 `live.compactionFailure`** ——
    状态先撤、结果后写；反过来的话，一个在两者之间读到 `live` 的人会看到
    「摘要已放好，而状态还说它在跑」。
    """
    remove_compaction_status(live, runtime.task_id)
    __del_key(live, "compactionFailure")
    text = f"{SUMMARY_PREFIX}{summary}{SUMMARY_SUFFIX}"
    entry = {
        "kind": CompactionEntry.kind,
        "head": first_kept,
        "model": [
            {
                "role": "user",
                "content": [{"type": "text", "text": text}],
                "timestamp": runtime.now(),
            }
        ],
        "data": {"reason": input_reason(current)},
    }
    if _field(current, "owner") is None:
        submission_id = await admit_submission(
            tx,
            runtime.conversation_id,
            {"type": "write", "requestId": f"compaction:{runtime.task_id}", "entry": entry},
            runtime.now(),
            runtime.settings,
        )
        result: dict[str, Any] = {"submissionId": submission_id}
    else:
        result = {"entryId": (await tx.append_entry(runtime.conversation_id, entry))["id"]}
    return {"status": "terminal", "outcome": {"status": "completed", "result": result}}


def input_reason(current: Any) -> Any:
    """`current.input.reason` —— 两种形状都吃（**R26**）。"""
    input_ = _field(current, "input") or {}
    return _field(input_, "reason")


async def complete(runtime: Any, context: Any) -> None:
    """**撤掉状态、不给摘要**地完成（``compaction.ts:450-456``）。

    ⚠️ 它用于**两种**情况：`select` 找不到切点，或钩子**拒绝**了这次压缩。
    **两者都算"成功完成"**（`result: {}`）—— 因为「没什么可压」不是失败。
    """

    async def change(tx: Any, _current: Any = None) -> Any:
        remove_compaction_status(await tx.doc(LiveDoc, runtime.conversation_id), runtime.task_id)
        return {"status": "terminal", "outcome": {"status": "completed", "result": {}}}

    await runtime.commit(change, context)


async def fail_no_model_compaction(runtime: Any, ref: Any, context: Any) -> None:
    """没有可用模型时的失败（``compaction.ts:457-464``）。

    ⚠️ 与生成的同名函数**只差一处**：它把失败**记进压缩状态**（`failCompactionStatus`），
    因为压缩是**挂在 `pi.live` 上的**、而生成不是。
    """
    message = (
        "No model is configured"
        if ref is None
        else f"Model {_field(ref, 'provider')}/{_field(ref, 'modelId')} is not available"
    )

    async def change(tx: Any, _current: Any = None) -> Any:
        fail_compaction_status(
            await tx.doc(LiveDoc, runtime.conversation_id), runtime.task_id, message
        )
        return {
            "status": "terminal",
            "outcome": {
                "status": "failed",
                "error": {"message": message, "detail": {"reason": "no_model"}},
            },
        }

    await runtime.commit(change, context)


# --------------------------------------------------------------------------------------
# `CompactionTask` 本体（``compaction.ts:110-240``）
# --------------------------------------------------------------------------------------


async def _select_phase(task: Any, runtime: Any, context: Any) -> None:
    """`select`：挑切点、问钩子、然后转 `summarize`（``compaction.ts:116-166``）。

    ⚠️ **三种结局**，顺序即优先级：

    1. **没有模型** → 失败；
    2. **`select_cut` 找不到切点** → :func:`complete`（**"没什么可压"不是失败**）；
    3. **钩子 `decline`** → 也是 :func:`complete`；
    4. **钩子给了 `summary`** → 直接 :func:`place`（**跳过模型调用**）；
    5. 否则 → 转 `summarize`。

    ⚠️ **`maxTokens` 的算法**：`min(floor(0.8 * reserveTokens), maxTokens > 0 ? maxTokens : ∞)`
    —— **第二个 `min` 是防"模型没有上限"的**（`maxTokens <= 0` 时**不设上限**，
    而不是取 0；取 0 会让摘要**一个字都写不出来**）。
    """
    conversation_id = runtime.conversation_id
    agent = await runtime.agent(context)
    settings = runtime.settings
    ref = _field(agent, "model")
    model = (
        None
        if ref is None
        else runtime.models.get_model(_field(ref, "provider"), _field(ref, "model_id"))
    )
    if ref is None or model is None:
        return await fail_no_model_compaction(runtime, ref, context)
    policy = settings["compaction"]
    view = await runtime.context(conversation_id, context)
    cut = select_cut(view, policy["keepRecentTokens"])
    if cut is None:
        return await complete(runtime, context)
    first_kept = view["entries"][cut]["id"]
    input_ = task["input"]
    reason = input_["reason"]
    instructions = input_.get("instructions")
    compaction: dict[str, Any] = {
        "reason": reason,
        "entries": view["entries"][:cut],
        "messages": summarized_messages(view, cut),
        "firstKept": first_kept,
    }
    if instructions is not None:
        compaction["instructions"] = instructions
    decision: dict[str, Any] | None = None

    async def before_compact(hook: Any) -> None:
        nonlocal decision
        if decision is None:
            returned = hook(compaction, runtime, context)
            if inspect.isawaitable(returned):
                returned = await returned
            decision = returned

    await runtime.hooks.each("beforeCompact", before_compact)
    if decision is not None and decision.get("decline"):
        return await complete(runtime, context)
    if decision is not None:
        return await place(runtime, first_kept, decision["summary"], context)

    tail = first_kept
    for entry in view["entries"]:
        if entry["id"] > tail:
            tail = entry["id"]
    max_tokens = min(
        int(0.8 * policy["reserveTokens"]),
        0 if False else (model.max_tokens if model.max_tokens > 0 else float("inf")),
    )
    request = {
        "attempt": 1,
        "model": ref,
        "thinkingLevel": _field(agent, "thinking_level"),
        "streamOptions": settings["stream"],
        "maxTokens": max_tokens,
        "tail": tail,
        "firstKept": first_kept,
    }

    async def change(tx: Any, _current: Any = None) -> Any:
        return {"status": "running", "checkpoint": {"phase": "summarize", **request}}

    await runtime.commit(change, context)


async def _summarize_phase(task: Any, runtime: Any, context: Any) -> None:
    """`summarize`：请模型写摘要，然后放置它、重试、或失败（``compaction.ts:167-218``）。

    ⚠️ **第二次按 `firstKept` 找切点**：上下文在 `tail` 处是**不可变的**，
    所以这就是 `select` 当初挑的那个范围 —— 用 `findIndex(entry.id === firstKept)`
    **重新定位**，而不是把切点存进检查点（**切片位置是派生值，不是状态**）。
    """
    checkpoint = task["state"]["checkpoint"]
    request = {k: v for k, v in checkpoint.items() if k != "phase"}
    ref = request["model"]
    model = runtime.models.get_model(_field(ref, "provider"), _field(ref, "model_id"))
    if model is None:
        return await fail_no_model_compaction(runtime, ref, context)
    view = await runtime.context(runtime.conversation_id, context, {"at": request["tail"]})
    cut = next(
        (
            index
            for index, entry in enumerate(view["entries"])
            if entry["id"] == request["firstKept"]
        ),
        0,
    )
    now = runtime.now()
    messages: list[Any] = [
        {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT, "timestamp": now},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": summary_prompt(
                        summarized_messages(view, cut), (task["input"] or {}).get("instructions")
                    ),
                }
            ],
            "timestamp": now,
        },
    ]
    forwarded = {k: v for k, v in (request["streamOptions"] or {}).items() if k != "deferred"}
    options: dict[str, Any] = {
        **forwarded,
        "cacheRetention": "none",
        "maxTokens": request["maxTokens"],
        "signal": runtime.signal,
        "sessionId": await ensure_provider_session_id(runtime, context),
    }
    if request["thinkingLevel"] != "off":
        options["reasoning"] = request["thinkingLevel"]
    message = await runtime.models.complete_simple(model, {"messages": messages}, options)
    # ⚠️ 中止标记或关闭：**由中止调用或重开的那次任务**处理已提交状态。
    runtime.signal.throw_if_aborted()
    summary = summary_text(message)
    settings = runtime.settings
    policy = settings["retry"]
    attempt = request["attempt"]
    retry = (
        message.get("stop_reason") == "error"
        and is_retryable_assistant_error(message)
        and policy["enabled"]
        and attempt <= policy["maxRetries"]
    )
    until = runtime.now() + retry_delay_ms(_retry_policy(settings), attempt) if retry else 0

    async def change(tx: Any, current: Any) -> Any:
        await record_usage(
            tx,
            runtime.conversation_id,
            "models",
            f"{message.get('provider')}/{message.get('model')}",
            message.get("usage"),
        )
        live = await tx.doc(LiveDoc, runtime.conversation_id)
        if summary is not None:
            return await _place_summary(tx, runtime, current, live, request["firstKept"], summary)
        if retry:
            status = compaction_status(live, runtime.task_id)
            if status is not None:
                status["retry"] = {"at": until, "error": message.get("error_message") or ""}
            return {
                "status": "running",
                "checkpoint": {"phase": "retry", **request, "until": until},
            }
        text = summary_failure(message)
        fail_compaction_status(live, runtime.task_id, text)
        return {
            "status": "terminal",
            "outcome": {
                "status": "failed",
                "error": {"message": text, "detail": {"reason": "model_error"}},
            },
        }

    await runtime.commit(change, context)


async def _compaction_retry_phase(task: Any, runtime: Any, context: Any) -> None:
    """`retry`：等到 `until`、把尝试数 +1、回到 `summarize`（``compaction.ts:219-233``）。

    ⚠️ **`status.attempt` 也要 +1，并删掉 `status.retry`** ——
    否则 `pi.live` 上会**永远挂着一个过期的重试时刻**，而 UI 会一直显示"正在重试"。
    """
    checkpoint = task["state"]["checkpoint"]
    request = {k: v for k, v in checkpoint.items() if k not in ("phase", "until")}
    await runtime.sleep(checkpoint["until"], context)
    attempt = request["attempt"] + 1

    async def change(tx: Any, _current: Any = None) -> Any:
        status = compaction_status(await tx.doc(LiveDoc, runtime.conversation_id), runtime.task_id)
        if status is not None:
            status["attempt"] = attempt
            __del_key(status, "retry")
        return {
            "status": "running",
            "checkpoint": {"phase": "summarize", **request, "attempt": attempt},
        }

    await runtime.commit(change, context)


async def _compaction_abort_phase(_task: Any, runtime: Any, context: Any) -> None:
    """`abort`（``compaction.ts:234-240``）。

    ⚠️ **只做一件事：撤掉 `pi.live` 上的压缩状态** ——
    因为**半压缩的摘要根本不该存在**（摘要是一次性的、要么放要么不放），
    所以这里**没有 `ToolTask` 那种"可能已经跑了一半"的三态**。
    """

    async def change(tx: Any, _current: Any = None) -> Any:
        remove_compaction_status(await tx.doc(LiveDoc, runtime.conversation_id), runtime.task_id)
        return {"status": "terminal", "outcome": {"status": "aborted"}}

    await runtime.commit(change, context)


CompactionTask = define_task(
    {
        "name": "pi.compaction",
        "version": 1,
        "initial": lambda input_=None: {"phase": "select"},
        "phases": {
            "select": _select_phase,
            "summarize": _summarize_phase,
            "retry": _compaction_retry_phase,
        },
        "abort": _compaction_abort_phase,
    }
)


def __del_key(target: Any, key: str) -> None:
    """删掉一个 ``Draft`` / dict 上的键（**存在才删**）。

    ⚠️ **`Draft.pop` 不是 dict 的 pop** —— 它的签名是 ``pop(index=-1)``，
    是**列表**语义！传 ``pop("k", None)`` 会报
    ``TypeError: Draft.pop() takes from 1 to 2 positional arguments but 3 were given``。

    正确写法是 ``if key in target: del target[key]``（`Draft` 两个 dunder 都有）。
    ⚠️ 这是〈本端把上游的字面量/对象做成了 dataclass〉那一族的**反面**：
    这里不是"上游是对象、本端是 dataclass"，而是**本端的 `Draft` 长得像 dict、实际不是**。
    """
    if key in target:
        del target[key]


def _retry_policy(settings: dict[str, Any]) -> Any:
    """把 settings 里的**会话重试策略**转成 `pi_ai.RetryPolicy`。

    ⚠️ **两处不匹配，都要处理**：

    1. **形状**：settings 给的是 **TypedDict（dict）**，而 `pi_ai.retry_delay_ms` 收的是
       **dataclass 对象**（它读 `policy.base_delay_ms`）—— 传 dict 会报
       `'dict' object has no attribute 'base_delay_ms'`（**R26**，端到端才发现）；
    2. **命名**：settings 是 **camelCase**（`maxRetries` / `baseDelayMs` / `maxAgentDelayMs`），
       而 `RetryPolicy` 是 **snake_case**。

    ⚠️ `max_agent_delay_ms` **必须传** —— 它有默认值 60 秒，而**漏传会让退避被静默封顶在 60 秒**
    （那与"用户没设"是两回事）。
    """
    from pi_ai import RetryPolicy

    policy = settings.get("retry") or {}
    return RetryPolicy(
        enabled=bool(policy.get("enabled")),
        max_retries=int(policy.get("maxRetries") or 0),
        base_delay_ms=int(policy.get("baseDelayMs") or 1000),
        max_agent_delay_ms=int(policy.get("maxAgentDelayMs") or 60_000),
    )

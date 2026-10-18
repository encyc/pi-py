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

from typing import Any

from pydantic import TypeAdapter

from pi_ai import Message, Usage
from pi_ai.estimate import calculate_context_tokens, estimate_message_tokens
from pi_durable.harness.context import order_tool_results
from pi_durable.harness.types import ContextView

__all__ = [
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

"""一个对话的已提交上下文 —— 移植 ``harness/context.ts``（333 行）。

**上下文** = 下一次提供方请求要用的模型消息。它由三样东西决定：

1. **边界**（:class:`ContextBounds`）：最新 head 标记 + tail，两个 O(1) 读就够；
2. **活动条目**：head 标记本身，后面跟着从它的 ``head`` 到 tail 的**非 head** 条目；
3. **派生**：逐条应用编辑、剔除被排除的 stop reason、把工具结果按调用序排到各自的助手消息后面、
   必要时把 system 消息提到最前。

**会话线与读的分离**是这块的核心设计：边界在**会话线**上捕获（:meth:`SessionImpl.read_on_line`），
之后 tail 及以下的条目**永不再变**，所以派生可以**离开写线**做。
:func:`read_context_from` 利用这一点做**增量**读——同一个 head 标记下只扫描 tail 之后的新条目。

⚠️ 本模块**第一次把 ``session/`` 与 ``harness/`` 接在一起**（以类型方式导入 ``SessionImpl``）。
这是**设计使然**：上下文派生本来就要跨「会话线读边界」与「离线的历史派生」两侧，
不是耦合意外——**不要把它当架构问题去「解耦」**。
"""

from __future__ import annotations

from typing import Any, cast

from pi_ai import Message
from pi_chord.context import Context
from pi_durable.entries import SystemEntry
from pi_durable.harness.types import ContextView
from pi_durable.session.session import SessionImpl
from pi_durable.types import (
    ContextEdit,
    ConversationId,
    Cursor,
    EntryId,
    EntryQuery,
    EntryRecord,
    Storage,
)

__all__ = [
    "EXCLUDED_STOP_REASONS",
    "MISSING_RESULT_TEXT",
    "ContextBounds",
    "ContextRange",
    "active_entries",
    "capture_context_bounds",
    "derive_context",
    "freeze_json",
    "order_tool_results",
    "read_context",
    "read_context_from",
]

#: 内部扫描的分页大小（``context.ts:9``）。
SCAN_PAGE_SIZE = 256

#: 这些 stop reason 的助手消息**不进上下文**（``context.ts:10``）。
#:
#: ⚠️ 上游收了 ``"deferred"``，而 pi_ai 的 ``AssistantMessage.stop_reason`` 字面量里**没有**它。
#: 照上游收着：多余的成员无害（它永远不会命中），而漏掉一个会静默改变上下文。
EXCLUDED_STOP_REASONS: frozenset[str] = frozenset({"aborted", "error", "deferred"})

#: 合成出来的「工具结果缺失」文本（``context.ts:11``）。
MISSING_RESULT_TEXT = "Tool result unavailable: history ends before this call completed."


def _field(message: Any, name: str, default: Any = None) -> Any:
    """取一条消息的字段，**同时**接受 JSON 记录（dict）与 pydantic 模型。

    ⚠️ **这是 Python 侧的必需品，JS 没有对应物**：条目是从存储里读回来的 **JSON**，
    所以 ``entry["model"]`` 里是**普通 dict**（``{"role": "user", "content": [...]}``），
    而不是 pydantic 的 ``Message`` 对象。上游 TS 里 JSON 对象**就是** ``Message``，
    所以它直接写 ``message.role``。

    两种形状的**字段名都是 snake_case**（``model_dump()`` 的输出与 pydantic 属性一致），
    所以代价只是「用 dict 取还是属性取」。
    """
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


class ContextBounds:
    """固定住一段已提交上下文范围的 head 标记与最新可见条目（``context.ts:14-17``）。"""

    __slots__ = ("head", "tail")

    def __init__(self, head: Any, tail: int) -> None:
        self.head = head
        self.tail = tail

    def __repr__(self) -> str:
        return f"ContextBounds(head={None if self.head is None else self.head.get('id')}, tail={self.tail})"


class ContextRange:
    """一次上下文读：扫过的可见条目，以及从它们派生出的视图（``context.ts:48-64``）。"""

    __slots__ = ("baseline", "bounds", "edited", "entries", "open", "settled", "view")

    def __init__(
        self,
        bounds: ContextBounds,
        entries: list[EntryRecord],
        view: ContextView,
        edited: set[int],
        settled: list[Message],
        open_: list[Message],
        baseline: Any,
    ) -> None:
        self.bounds = bounds
        self.entries = entries
        self.view = view
        #: ``entries`` 里那些编辑的**目标**。
        self.edited = edited
        #: 最后一条助手消息**之前**那部分贡献消息算出的 ``view.messages``。
        self.settled = settled
        #: 从最后一条助手消息起的贡献消息，**工具结果排序之前**。
        self.open = open_
        #: 基线的 system 消息；见 :func:`find_baseline`。
        self.baseline = baseline


def freeze_json(value: Any) -> Any:
    """上游在这里深冻结一个 JSON 值（``context.ts:235-243``）。

    ⚠️ **偏离**：Python 没有 ``Object.freeze`` 的对等物。本包按 PORTING.md 偏离 #51
    （``MemoryStorage`` 的 ``freeze``）的同一取舍处理——**只保证解除关联，不冻结**。
    这**不是桩**：它是有意的语义偏离，理由是 Python 的惯例是「不改变不是自己造的值」，
    而强行用 ``MappingProxyType`` / ``tuple`` 会把条目换成非 dict 类型，
    破坏 ``copy_json`` 与所有按 TypedDict 消费的下游。

    ``ContextRange`` 会活过本次读，所以它持有的条目按约定**不得被改动**。
    """
    return value


async def capture_context_bounds(
    storage: Storage,
    conversation_id: ConversationId,
    context: Context,
    at: EntryId | None = None,
) -> ContextBounds | None:
    """用**两个 O(1) 读**捕获当前上下文的边界，或「在可见条目 ``at`` 处切掉」那份上下文的边界
    （``context.ts:24-41``）。

    在**会话线**上跑它；tail 及以下的条目不可变，所以 :func:`derive_context` 可以离线扫它们。
    """
    if at is None:
        page = await storage.scan_entries({"conversationId": conversation_id}, 1, None, context)
        if not page["items"]:
            return None
        tail: int = page["items"][0]["id"]
    else:
        if await storage.entry_in_conversation(conversation_id, at, context) is None:
            raise RuntimeError(f"Entry {at} is not visible from conversation {conversation_id}")
        tail = at
    head = await storage.find_latest_head_marker(conversation_id, tail, context)
    return ContextBounds(head=head, tail=tail)


async def read_context_from(
    session: SessionImpl,
    storage: Storage,
    conversation_id: ConversationId,
    context: Context,
    at: EntryId | None,
    previous: ContextRange | None,
) -> tuple[ContextView, ContextRange | None]:
    """:func:`read_context` 的**增量**版本：复用 ``previous``（同一对话的早先范围）
    （``context.ts:73-110``）。

    同一个 head 标记下只扫描它 tail **之后**的条目。范围会活过这次读，
    所以返回的视图持有**自己的数组**。
    """
    bounds = await session.read_on_line(
        lambda: capture_context_bounds(storage, conversation_id, context, at)
    )
    if bounds is None:
        return empty_view(), None
    freeze_json(bounds.head)

    if previous is None or _head_id(previous.bounds) != _head_id(bounds):
        entries = await _scan_range(storage, conversation_id, _range_query(bounds), context)
        return _finish(derive_range(bounds, cast(list[EntryRecord], freeze_json(entries))))
    if bounds.tail == previous.bounds.tail:
        return _finish(previous)
    if bounds.tail < previous.bounds.tail:
        kept = [entry for entry in previous.entries if entry["id"] <= bounds.tail]
        return _finish(derive_range(bounds, kept))

    added = await _scan_range(
        storage,
        conversation_id,
        {"minEntryId": previous.bounds.tail + 1, "maxEntryId": bounds.tail},
        context,
    )
    return _finish(extend_range(previous, bounds, cast(list[EntryRecord], freeze_json(added))))


def _finish(range_: ContextRange) -> tuple[ContextView, ContextRange]:
    """返回**副本数组**的视图与范围本身（``context.ts:101-109``）。"""
    view = range_.view
    return (
        {
            "head": view["head"],
            "entries": list(view["entries"]),
            "contributions": [list(c) for c in view["contributions"]],
            "messages": list(view["messages"]),
        },
        range_,
    )


async def read_context(
    session: SessionImpl,
    storage: Storage,
    conversation_id: ConversationId,
    context: Context,
    at: EntryId | None = None,
) -> ContextView:
    """一个对话的已提交上下文：边界在会话线上捕获，条目离线派生（``context.ts:112-121``）。"""
    bounds = await session.read_on_line(
        lambda: capture_context_bounds(storage, conversation_id, context, at)
    )
    return await derive_context(storage, conversation_id, bounds, context)


async def derive_context(
    storage: Storage,
    conversation_id: ConversationId,
    bounds: ContextBounds | None,
    context: Context,
) -> ContextView:
    """在已捕获的边界内派生一个对话的活动 transcript 与模型上下文（``context.ts:129-137``）。

    ``H`` = 最新可见 head 标记；范围从 ``H.head``（或 transcript 起点）到 tail。
    每个目标上**范围内最新的编辑**生效。上下文条目是 ``H`` 后面跟着范围内的非 head 条目。
    """
    if bounds is None:
        return empty_view()
    entries = await _scan_range(storage, conversation_id, _range_query(bounds), context)
    return derive_range(bounds, entries).view


def empty_view() -> ContextView:
    """没有任何条目的视图（``context.ts:139-141``）。"""
    return {"head": None, "entries": [], "contributions": [], "messages": []}


def derive_range(bounds: ContextBounds, entries: list[EntryRecord]) -> ContextRange:
    """从 ``bounds`` 内扫到的条目派生上下文视图（``context.ts:144-159``）。"""
    edits: dict[int, ContextEdit] = {}
    # 范围内**每一个**条目的编辑都算数，包括被 select_active() 丢掉的更早的 head 标记。
    for entry in entries:
        for edit in entry.get("edits") or []:
            edits[edit["target"]] = edit
    active = select_active(bounds.head, entries)
    contributions = [contribute(entry, edits.get(entry["id"])) for entry in active]
    settled, open_ = settle([], [message for group in contributions for message in group])
    baseline = find_baseline(bounds.head, active)
    messages = lead_with_system([*settled, *order_tool_results(open_)], baseline)
    view: ContextView = {
        "head": bounds.head,
        "entries": active,
        "contributions": contributions,
        "messages": messages,
    }
    return ContextRange(bounds, entries, view, set(edits.keys()), settled, open_, baseline)


def extend_range(
    previous: ContextRange, bounds: ContextBounds, added: list[EntryRecord]
) -> ContextRange:
    """把 ``previous`` 用 tail 之后新扫到的 ``added`` 扩展（``context.ts:161-183``）。

    只派生**新条目的贡献**与 open 消息。**一个新编辑可以改到更早的条目**，
    所以那种情况下退回整段重算。
    """
    entries = [*previous.entries, *added]
    if any(
        entry.get("edits") is not None
        or entry.get("head") is not None
        or entry["id"] in previous.edited
        for entry in added
    ):
        return derive_range(bounds, entries)
    contributions = [contribute(entry, None) for entry in added]
    settled, open_ = settle(
        previous.settled, [*previous.open, *[m for group in contributions for m in group]]
    )
    baseline = (
        previous.baseline
        if previous.baseline is not _NO_BASELINE
        else find_baseline(bounds.head, added)
    )
    view: ContextView = {
        "head": bounds.head,
        "entries": [*previous.view["entries"], *added],
        "contributions": [*previous.view["contributions"], *contributions],
        "messages": lead_with_system([*settled, *order_tool_results(open_)], baseline),
    }
    return ContextRange(bounds, entries, view, previous.edited, settled, open_, baseline)


def find_baseline(head: Any, entries: list[EntryRecord]) -> Any:
    """``entries``（旧→新）里的基线（``context.ts:185-193``）。

    它是 head 标记之后**第一条** ``pi.system`` 条目的 system 消息——那条是 generation
    在「所有 section 与工具都已生效」时写下的。没有 head 标记时，是**没有** system 消息的
    transcript 的那条。

    基线之后总会有（且会省略保留的 ``pi.system`` 条目）；没有标记时，
    只要**之前没见过** system 消息就有——除非更早的条目带了 system 消息，
    哪怕之后有编辑把它省掉。**瞄准基线的编辑**会贡献别的消息或什么都不贡献，
    所以不会由它来打头；**后面的条目也不会取代它**。
    """
    for entry in entries:
        if head is not None and entry["id"] <= head["id"]:
            continue
        if SystemEntry.is_(entry):
            messages = entry.get("model") or []
            for message in messages:
                if _field(message, "role") == "system":
                    return message
            return None
        if head is None and carries_system_message(entry):
            return None
    return _NO_BASELINE


#: 「还没有定论」的基线哨兵：``undefined`` 的对应物（更晚的条目可能才是基线）。
_NO_BASELINE = object()


def carries_system_message(entry: EntryRecord) -> bool:
    """一个条目自己的消息、或它编辑的替换消息里，是否有 system 消息（``context.ts:195-203``）。"""
    own = cast(list[Message], entry.get("model") or [])
    if any(_field(message, "role") == "system" for message in own):
        return True
    for edit in cast(list[ContextEdit], entry.get("edits") or []):
        if edit.get("action") == "replace":
            replaced = cast(list[Message], edit.get("messages") or [])
            if any(_field(message, "role") == "system" for message in replaced):
                return True
    return False


def lead_with_system(messages: list[Message], baseline: Any) -> list[Message]:
    """**只在**「第一条 system 消息前面全是用户消息」或「它就是基线」时，把它挪到最前
    （``context.ts:212-221``）。

    一次运行的输入会在 generation 渲染 system 提示**之前**提交，所以 transcript 以输入打头、
    基线紧随其后；压缩之后的范围也以保留的历史打头（含助手消息）。
    **提供方只把打头的 system 消息当作初始提示与工具集**——没有它，之后一次工具变更会重写请求的
    工具表并让整个提示缓存失效。**只挪第一条** system 消息，这样按序重放 system 消息
    得到同样的 section 与工具。

    其它 system 消息（助手消息之前的）**留在原地**：挪动它会改变它对模型的含义。
    """
    index = next(
        (i for i, message in enumerate(messages) if _field(message, "role") == "system"), None
    )
    if index is None or index == 0:
        return messages
    system = messages[index]
    # ⚠️ 不能只靠 `is not`：JSON 往返之后，`baseline` 与 `messages` 里那条是**两份相等的 dict**。
    if (
        system is not baseline
        and system != baseline
        and any(_field(message, "role") != "user" for message in messages[:index])
    ):
        return messages
    return [system, *messages[:index], *messages[index + 1 :]]


def contribute(entry: EntryRecord, edit: ContextEdit | None) -> list[Message]:
    """一个活动条目在**应用编辑、剔除被排除的 stop reason 之后**、**工具结果排序之前**的消息
    （``context.ts:223-230``）。"""
    if edit is not None and edit.get("action") == "omit":
        return []
    contributed = (
        cast(list[Message], edit.get("messages") or [])
        if edit is not None and edit.get("action") == "replace"
        else cast(list[Message], entry.get("model") or [])
    )
    return [
        message
        for message in contributed
        if _field(message, "role") != "assistant"
        or _field(message, "stop_reason") not in EXCLUDED_STOP_REASONS
    ]


def settle(settled: list[Message], open_: list[Message]) -> tuple[list[Message], list[Message]]:
    """把 ``open`` 里**最后一条助手消息之前**的有序消息并进 ``settled``（``context.ts:247-255``）。

    之所以成立：``order_tool_results()`` 作用在一段序列上，等于作用在它的每一部分上再拼接——
    只要每个后起的部分都以助手消息打头。
    """
    last: int | None = None
    for index, candidate in enumerate(open_):
        if _field(candidate, "role") == "assistant":
            last = index
    if last is None or last <= 0:
        return settled, open_
    return [*settled, *order_tool_results(open_[:last])], open_[last:]


async def active_entries(
    storage: Storage,
    conversation_id: ConversationId,
    bounds: ContextBounds | None,
    context: Context,
) -> list[EntryRecord]:
    """已捕获边界内的**原始**活动条目，不派生模型上下文（``context.ts:257-266``）。"""
    if bounds is None:
        return []
    entries = await _scan_range(storage, conversation_id, _range_query(bounds), context)
    return select_active(bounds.head, entries)


def _range_query(bounds: ContextBounds) -> dict[str, Any]:
    """``bounds`` 的可见范围：从 head 标记的 ``head``（或 transcript 起点）到 tail
    （``context.ts:268-272``）。"""
    head = bounds.head
    if head is None:
        return {"maxEntryId": bounds.tail}
    return {"minEntryId": head["head"], "maxEntryId": bounds.tail}


async def _scan_range(
    storage: Storage,
    conversation_id: ConversationId,
    range_: dict[str, Any],
    context: Context,
) -> list[EntryRecord]:
    """``range_`` 内的可见条目，**旧→新**（``context.ts:274-289``）。"""
    entries: list[EntryRecord] = []
    cursor: Cursor | None = None
    while True:
        query: dict[str, Any] = {"conversationId": conversation_id}
        query.update(range_)
        page = await storage.scan_entries(cast(EntryQuery, query), SCAN_PAGE_SIZE, cursor, context)
        entries.extend(cast(list[EntryRecord], page["items"]))
        cursor = page.get("next")
        if cursor is None:
            break
    entries.reverse()
    return entries


def select_active(head: Any, range_: list[EntryRecord]) -> list[EntryRecord]:
    """head 标记后面跟着范围内**非 head** 条目；没有标记时就是整个范围（``context.ts:291-296``）。"""
    if head is None:
        return range_
    return [head, *(entry for entry in range_ if entry.get("head") is None)]


def order_tool_results(messages: list[Message]) -> list[Message]:
    """把每个助手的工具结果**按调用序**紧跟在它后面（``context.ts:299-321``）。

    结果取自「下一条助手消息之前」的消息；**缺失的结果会合成一条**，
    **没匹配上的结果被丢弃**。
    """
    ordered: list[Message] = []
    for index, message in enumerate(messages):
        role = _field(message, "role")
        if role == "toolResult":
            continue
        ordered.append(message)
        if role != "assistant":
            continue
        calls = [
            content
            for content in (_field(message, "content") or [])
            if _field(content, "type") == "toolCall"
        ]
        if not calls:
            continue
        results: dict[str, int] = {}
        for next_index in range(index + 1, len(messages)):
            candidate = messages[next_index]
            candidate_role = _field(candidate, "role")
            if candidate_role == "assistant":
                break
            call_id = _field(candidate, "tool_call_id")
            if candidate_role == "toolResult" and call_id not in results:
                results[call_id] = next_index
        for call in calls:
            found = results.get(_field(call, "id"))
            ordered.append(
                missing_result(call, _field(message, "timestamp"))
                if found is None
                else messages[found]
            )
    return ordered


def missing_result(call: Any, timestamp: Any) -> Any:
    """合成一条「结果缺失」的工具结果（``context.ts:323-333``）。

    ⚠️ 返回的是 **dict**，不是 pydantic 的 ``ToolResultMessage``：条目里的消息来自 JSON
    （见 :func:`_field`），合成的那条必须与它们**形状一致**，否则下游会拿到混着两种形状的列表。
    上游 TS 里没这个区分——普通对象就是 ``ToolResultMessage``。
    """
    return {
        "role": "toolResult",
        "tool_call_id": _field(call, "id"),
        "tool_name": _field(call, "name"),
        "content": [{"type": "text", "text": MISSING_RESULT_TEXT}],
        "is_error": True,
        "details": {"reason": "missing_result"},
        "timestamp": timestamp,
    }


def _head_id(bounds: ContextBounds) -> Any:
    return None if bounds.head is None else bounds.head["id"]

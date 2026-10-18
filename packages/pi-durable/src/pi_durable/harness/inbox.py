"""一个对话的排队提交 —— 移植 ``harness/inbox.ts``（168 行）。

边界（boundary）是「把排队中的输入放到 transcript 上」的那一瞬。它按 spec §6 选择：

* **每一个** write（被动的条目写入）；
* steer：第一个或全部；
* follow-up：**只在 final 边界**，第一个或全部。

一个被选中的 reset（``head: "self"`` 的 write）会把 ``postTools`` 边界**升级成 final**。

两处顺序要分清（容易想当然）：

* **选择**按 mode：先挑 steer（第一个或全部），final 边界再挑 follow-up；
* **放置**按**收件箱位置**——挑出来的下标会被 ``sort``，所以先排队的那一项先落地，
  哪怕它是 follow-up。**不是**「steer 一律排在 follow-up 前面」。

另一个顺序是确定的：**write 先放、用户项后放**——这样在 reset 之前排队的用户项
会在**新上下文**里跑。write 的 head 指向活动范围之前（含本次提交里更早开启的范围）时算**过期**，
结算成 ``unanswered`` / ``stale`` 而**不**放置。
"""

from __future__ import annotations

from typing import Any, TypedDict, cast

from pi_chord.json import JsonRepresentation, copy_json
from pi_durable.documents import define_doc
from pi_durable.entries import UserEntry


def _plain(value: Any) -> Any:
    """把一个可能来自 chord ``Draft`` 的嵌套值取成**普通 JSON**。

    ⚠️ **R24**：chord 的 ``Draft`` 会**递归地**把每一个嵌套容器都包成 Draft，
    而 ``copy_json`` **拒绝** Draft（``Value contains a non-JSON Draft``）——
    它要的是严格 JSON。所以从草稿里读出嵌套值再交给别处时，**必须先 ``to_json()``**。

    JS 没有这一步：上游的 ``copyJson`` 顺着 Proxy 自然展开。
    """
    to_json = getattr(value, "to_json", None)
    return to_json() if callable(to_json) else value


__all__ = [
    "Boundary",
    "BoundaryResult",
    "InboxDoc",
    "InboxItem",
    "InboxState",
    "QueueModes",
    "append_input",
    "apply_boundary",
    "has_queued_reset",
    "is_display_only",
    "is_stale",
    "pick_input_entry",
    "prepare_boundary",
    "remove_inbox_item",
    "withdraw_queued_inputs",
]


class _SteerItem(TypedDict, total=False):
    """一个排队中的用户输入（``inbox.ts:7-13``）。"""

    id: int
    mode: str  # "steer" | "followUp"
    content: JsonRepresentation
    #: 不用 ``pi.user`` 落成条目时，用这个（``InputEntry`` 的 JSON 形式）。
    entry: JsonRepresentation


class _WriteItem(TypedDict):
    """一个排队中的被动条目写入（``inbox.ts:14-15``）。``entry`` 是 ``EntryDraft``，按普通 JSON 存。"""

    id: int
    mode: str  # "write"
    entry: dict[str, Any]


#: 一个排队中的提交（``inbox.ts:6-16``）。
InboxItem = dict[str, Any]


class InboxState(TypedDict):
    """一个对话的排队提交，按 ID 序（``inbox.ts:19``）。"""

    items: list[InboxItem]


#: 一个对话等待边界的排队提交（``inbox.ts:21-30``）。
#:
#: ``fork: "initial"``：分叉**从空队列开始**——排队中的输入属于**那个**对话，
#: 不该被复制到子会话去再放一遍。
#:
#: ``checkpointWhen: (value) => value.items.length === 0``：**队列空的时候才写 base**。
#: 队列是高频变更的小结构，空了就存成 base，比无限累积增量更省。
InboxDoc = define_doc(
    {
        "kind": "pi.inbox",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda seed=None: {"items": []},
        "checkpointWhen": lambda value, ops, info: len(value["items"]) == 0,
    }
)


class QueueModes(TypedDict, total=False):
    """边界读的排队模式（``inbox.ts:33`` 的 ``Pick<Settings, ...>``）。

    上游从 ``Settings`` 里挑两字段；Python 侧用这个**窄形状**，
    免得为一个 ``Pick`` 去先定完整的 ``Settings``（与 D-1 的窄提取同一取舍）。
    """

    steeringMode: str  # QueueMode
    followUpMode: str  # QueueMode


class Boundary:
    """边界在提交的第一次表写入**之前**读到的东西，以及它目前见过的最新 head
    （``inbox.ts:35-44``）。"""

    __slots__ = ("conversation_id", "follow_up_mode", "head", "inbox", "steering_mode")

    def __init__(
        self,
        conversation_id: int,
        inbox: Any,
        steering_mode: str,
        follow_up_mode: str,
        head: int | None,
    ) -> None:
        self.conversation_id = conversation_id
        self.inbox = inbox
        self.steering_mode = steering_mode
        self.follow_up_mode = follow_up_mode
        #: 活动范围的起点，也就是最新 head 标记的 ``head``；会被本次提交里写入的 head 推进。
        self.head = head


class BoundaryResult(TypedDict):
    """被选中的用户项（按 ID 序），以及是否放置过一次 ``head: "self"`` 写入（一次重置）
    （``inbox.ts:46-47``）。"""

    users: list[int]
    reset: bool


async def prepare_boundary(tx: Any, conversation_id: int, modes: QueueModes) -> Boundary:
    """读边界需要的东西（``inbox.ts:53-67``）。

    ⚠️ **表读取必须发生在本次提交的第一次表写入之前**，所以调用方要在自己那次提交的
    **开头**就准备好边界。
    """
    marker = await tx.latest_head_marker(conversation_id)
    head = marker["head"] if marker is not None else None
    inbox = await tx.doc(InboxDoc, conversation_id)
    return Boundary(
        conversation_id=conversation_id,
        inbox=inbox,
        steering_mode=modes.get("steeringMode", "all"),
        follow_up_mode=modes.get("followUpMode", "all"),
        head=head,
    )


async def apply_boundary(tx: Any, boundary: Boundary, at: str, now: float) -> BoundaryResult:
    """放置边界选中的排队项（``inbox.ts:79-119``）。``at`` 是 ``"postTools"`` 或 ``"final"``。"""
    items: list[InboxItem] = boundary.inbox["items"]
    reset = has_queued_reset(items)
    final = at == "final" or reset

    def pick(mode: str, queue_mode: str) -> list[int]:
        indexes = [index for index, item in enumerate(items) if item["mode"] == mode]
        return indexes if queue_mode == "all" else indexes[:1]

    writes = [index for index, item in enumerate(items) if item["mode"] == "write"]
    users = [*pick("steer", boundary.steering_mode)]
    if final:
        users.extend(pick("followUp", boundary.follow_up_mode))
    users.sort()

    # ``append_entry()`` 会**复制**草稿的值；这些项在之后才被移除。
    for index in writes:
        item = items[index]
        draft: dict[str, Any] = _plain(item["entry"])
        if is_stale(boundary, draft):
            tx.settle_submission(item["id"], {"status": "unanswered", "reason": "stale"})
            continue
        entry = await tx.append_entry(boundary.conversation_id, draft)
        if draft.get("head") is not None:
            boundary.head = entry["id"] if draft["head"] == "self" else draft["head"]
        tx.place_submission(item["id"], entry["id"])

    placed: list[int] = []
    for index in users:
        item = items[index]
        entry = await append_input(
            tx,
            boundary.conversation_id,
            _plain(item["content"]),
            _plain(item["entry"]) if item.get("entry") is not None else None,
            now,
        )
        tx.place_submission(item["id"], entry["id"])
        placed.append(item["id"])

    # 从**尾部**开始按位置移除，前面的下标才不会被移动。
    for index in sorted([*writes, *users], reverse=True):
        del items[index]
    return {"users": placed, "reset": reset}


async def append_input(
    tx: Any,
    conversation_id: int,
    content: Any,
    entry: dict[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    """追加一个输入要落成的条目（``inbox.ts:122-131``）。

    没有 ``entry`` 时是 ``pi.user``；否则用 ``entry`` 的 kind 与 data，
    ``model`` 里永远是这条输入自己的用户消息。
    """
    message = {"role": "user", "content": content, "timestamp": now}
    if entry is None:
        return cast(
            dict[str, Any], await tx.append_entry(conversation_id, {"model": [message]}, UserEntry)
        )
    return cast(
        dict[str, Any],
        await tx.append_entry(conversation_id, {**pick_input_entry(entry), "model": [message]}),
    )


def pick_input_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """一个输入的条目的 kind 与 data，**只**这两样（``inbox.ts:134-137``）。

    ⚠️ 上游用 ``omitUndefinedProperties`` 去掉未定义的 ``data``；按 **R23**，
    这里在**构造处**就不写那个键（``{kind}`` vs ``{kind, data}``），
    而不是指望开关替我删——那样会把合法的 JSON ``null`` 一起删掉。
    """
    picked = (
        {"kind": entry["kind"]}
        if entry.get("data") is None
        else {"kind": entry["kind"], "data": entry["data"]}
    )
    return cast(dict[str, Any], copy_json(picked))


def is_display_only(entry: dict[str, Any]) -> bool:
    """这个写入是否**只影响展示**（``inbox.ts:143-145``）。

    没有 ``model`` / ``head`` / ``edits``（**存在**就算，哪怕是空的）就什么都不改模型看得见的东西，
    也就不需要边界。
    """
    return entry.get("model") is None and entry.get("head") is None and entry.get("edits") is None


def has_queued_reset(items: list[InboxItem]) -> bool:
    """是否有一次重置（``head: "self"`` 的写入）排着队（``inbox.ts:148-150``）。

    在它落地之前追加的条目，会在它落地时**离开活动范围**。
    """
    return any(
        item["mode"] == "write" and _plain(item["entry"]).get("head") == "self" for item in items
    )


def is_stale(boundary: Boundary, entry: dict[str, Any]) -> bool:
    """这个 head 写入是否指向活动范围**之前**的条目（``inbox.ts:153-155``）——

    放它会把已经切掉的历史带回来。
    """
    head = entry.get("head")
    return (
        isinstance(head, int)
        and not isinstance(head, bool)
        and boundary.head is not None
        and head < boundary.head
    )


async def remove_inbox_item(tx: Any, conversation_id: int, id: int) -> None:
    """移走一个被撤回的提交的项；**调用方负责结算那个提交**（``inbox.ts:158-162``）。"""
    items: list[InboxItem] = (await tx.doc(InboxDoc, conversation_id))["items"]
    for index, item in enumerate(items):
        if item["id"] == id:
            del items[index]
            return


async def withdraw_queued_inputs(tx: Any, conversation_id: int) -> None:
    """撤回一个对话里**每一个**排队中的输入（``inbox.ts:168-181``）——

    ``Conversation.abort()`` 与中止级联就是这么做的：每个都结算成 ``unanswered`` / ``aborted``
    并离开收件箱；**排队中的写入留着**，等以后放置。
    """
    items: list[InboxItem] = (await tx.doc(InboxDoc, conversation_id))["items"]
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if item["mode"] == "write":
            continue
        tx.settle_submission(item["id"], {"status": "unanswered", "reason": "aborted"})
        del items[index]

"""提交的准入、等待与撤回 —— 移植 ``harness/submissions.ts``(241)。**SCC 的最后一块。**

``Spec §6`` 的准入规则全在 :func:`admit_submission` 里：一个提交要么**就地**落地
（空闲时），要么**进收件箱排队**（忙碌时），要么**被拒**（``whenBusy: "reject"``）。
:class:`Submissions` 是宿主的入口：准入、查、等、撤回。

⚠️ **本片是 SCC 里唯一不碰 ``TaskRuntime`` 的一块**：``admit_submission`` 只收 ``tx``，
``Submissions`` 只收 ``SessionImpl`` + ``Storage``——所以**两者都可以完整验收**。

⚠️ **`submissions → generation` 那条边是延迟导入**：:func:`admit_submission` 在落地一个
空闲输入后要 `start_run()`，而后者属于 ``generation.py`` 的 **Slice B**（尚待 `scheduler.py`）。
见 PORTING.md〈SCC 四文件的拆分方案〉与环的最终归属。
"""

from __future__ import annotations

from typing import Any, cast

from pi_chord.json import copy_json
from pi_durable.errors import ConversationBusy
from pi_durable.harness.inbox import (
    InboxDoc,
    QueueModes,
    append_input,
    apply_boundary,
    has_queued_reset,
    is_display_only,
    is_stale,
    pick_input_entry,
    prepare_boundary,
    remove_inbox_item,
)
from pi_durable.harness.live import LiveDoc
from pi_durable.harness.util import Waiters, closed_error
from pi_durable.session.session import SessionImpl
from pi_durable.types import Storage, SubmissionRecord

__all__ = [
    "AbortResult",
    "SubmissionHandle",
    "Submissions",
    "admit_submission",
    "check_input_entry",
    "is_settled",
]

#: 撤回一个排队中提交的结果（``submissions.ts:30``）。
#:
#: * ``"aborted"`` —— 撤回成功；
#: * ``"already_placed"`` —— 已经落地了，撤回不了；
#: * ``"settled"`` —— 已经结算了。
AbortResult = str


def is_settled(record: Any) -> bool:
    """一条提交记录**是否已结算**（``submissions.ts:147-149``）。

    只有 ``done`` 与 ``unanswered`` 算结算——``queued`` / ``placed`` 都还在路上。
    """
    return record["status"] in ("done", "unanswered")


def check_input_entry(entry: dict[str, Any]) -> None:
    """拒绝一个**无类型调用方**写错的输入条目（``submissions.ts:152-158``）。

    它的 ``kind`` 必须是非空字符串、且**不以 ``pi.`` 开头**——那个前缀是 Harness 内建种类的保留区。

    ⚠️ 错误里的 ``got ${String(kind)}`` 用的是 **JS 的 ``String()`` 语义**：
    ``str(True)`` 在 Python 是 ``"True"``、JS 是 ``"true"``。这里照 JS 转，
    免得错误文本与上游对不上（后来的人搜报错会搜不到）。
    """
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind == "" or kind.startswith("pi."):
        raise TypeError(
            "An input entry's kind must be a non-empty string not starting "
            f'with "pi.", got {_js_string(kind)}'
        )


def _js_string(value: Any) -> str:
    """``String(value)`` 的 JS 语义（``submissions.ts:156``）。"""
    if value is None:
        return "undefined" if value is None else "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


async def admit_submission(
    tx: Any,
    conversation_id: int,
    draft: dict[str, Any],
    now: float,
    queue_modes: QueueModes,
) -> int:
    """在**一次提交里**准入一个提交（spec §6）（``submissions.ts:171-241``）。

    ``Conversation.submit()`` 与对话拥有的压缩共用它。分支按**优先级**排列，顺序本身就是语义：

    1. **已知的请求 ID** → 直接返回它已有的提交**且不写任何东西**（幂等）；类型不符则报错；
    2. **纯展示写入**（只有 ``kind`` 与 ``data``）→ **无论忙不忙**都立刻追加并结算 ``done``——
       它不改变模型看得见的东西。**唯一的例外**：排在**已排队的 reset** 之后时要**排队**——
       那样它会落在 reset **之后**，而不是在 reset 结束的那段上下文**之前**；
    3. **忙碌** → 进 ``pi.inbox`` 排队；``whenBusy: "reject"`` 的输入用 ``ConversationBusy`` 拒掉；
    4. **空闲但收件箱里有东西** → 排到它们**后面**，并跑一次 **final 边界**；
    5. **否则空闲** → 输入**落地**（``pi.user`` 或输入自己的种类）并**启动一次运行**；
       写入则追加条目并结算 ``done``，或当它的 head 越过活动范围之前时结算 ``stale``。
    """
    if draft["type"] == "input" and draft.get("entry") is not None:
        check_input_entry(draft["entry"])
    if draft.get("requestId") is not None:
        existing = await tx.submission_by_request(conversation_id, draft["requestId"])
        if existing is not None:
            if existing["type"] != draft["type"]:
                raise RuntimeError(
                    f"Request {draft['requestId']} already identifies a submission "
                    f"of type {existing['type']}"
                )
            return int(existing["id"])
    request_id = {} if draft.get("requestId") is None else {"requestId": draft["requestId"]}

    # ② 纯展示写入：**忙不忙都立刻**——除非它前面排着一个 reset。
    if (
        draft["type"] == "write"
        and is_display_only(draft["entry"])
        and not has_queued_reset((await tx.doc(InboxDoc, conversation_id)).get("items") or [])
    ):
        entry = await tx.append_entry(conversation_id, draft["entry"])
        write = {
            "conversationId": conversation_id,
            **request_id,
            "type": "write",
            "status": "done",
            "entry": entry["id"],
        }
        return int((await tx.create_submission(write))["id"])

    live = await tx.doc(LiveDoc, conversation_id)
    busy = live.get("run") is not None
    if busy and draft["type"] == "input" and draft.get("whenBusy") == "reject":
        raise ConversationBusy(conversation_id)
    # 边界要**读表**，所以在第一次表写入**之前**准备；忙碌时不需要它。
    boundary = None if busy else await prepare_boundary(tx, conversation_id, queue_modes)

    if boundary is None or (boundary.inbox.get("items") or []):
        created = await tx.create_submission(
            {
                "conversationId": conversation_id,
                **request_id,
                "type": draft["type"],
                "status": "queued",
            }
        )
        id = int(created["id"])
        # 宿主可能留着可选字段；草稿要**严格 JSON**。
        value = _strict_json(draft["entry"] if draft["type"] == "write" else draft["content"])
        items = (
            boundary.inbox.get("items")
            if boundary is not None
            else (await tx.doc(InboxDoc, conversation_id)).get("items")
        )
        if draft["type"] == "write":
            items.append({"id": id, "mode": "write", "entry": value})
        else:
            mode = "steer" if draft.get("whenBusy") == "steer" else "followUp"
            entry = (
                {} if draft.get("entry") is None else {"entry": pick_input_entry(draft["entry"])}
            )
            items.append({"id": id, "mode": mode, "content": value, **entry})
        if boundary is None:
            return id
        result = await apply_boundary(tx, boundary, "final", now)
        if result["users"]:
            await _start_run(tx, conversation_id, live, result["users"])
        return id

    # ④ 空闲且收件箱为空：就地落地。
    if draft["type"] == "write":
        if is_stale(boundary, draft["entry"]):
            stale = {"status": "unanswered", "reason": "stale"}
            return int(
                (
                    await tx.create_submission(
                        {
                            "conversationId": conversation_id,
                            **request_id,
                            "type": "write",
                            **stale,
                        }
                    )
                )["id"]
            )
        entry = await tx.append_entry(conversation_id, draft["entry"])
        write = {
            "conversationId": conversation_id,
            **request_id,
            "type": "write",
            "status": "done",
            "entry": entry["id"],
        }
        return int((await tx.create_submission(write))["id"])

    entry = await append_input(tx, conversation_id, draft["content"], draft.get("entry"), now)
    input_ = {
        "conversationId": conversation_id,
        **request_id,
        "type": "input",
        "status": "placed",
        "entry": entry["id"],
    }
    id = int((await tx.create_submission(input_))["id"])
    await _start_run(tx, conversation_id, live, [id])
    return id


async def _start_run(tx: Any, conversation_id: int, live: Any, users: list[int]) -> None:
    """启动一次运行。

    ⚠️ **环上的回边**（``submissions → generation``）：函数内延迟导入。
    `start_run` 属于 `generation.py` 的 **Slice B**（待 `scheduler.py`），
    所以现在**必须**延迟；**等 Slice B 落地后**它同时也能当**顶层**导入用——
    但那时它仍会被留在函数内，因为**这条边正是用来打破三环的那一条**
    （见 PORTING.md〈环的最终归属〉）。
    """
    # ⚠️ `start_run` 属 `generation.py` 的 **Slice B**（待 `scheduler.py`），所以现在挂忽略；
    # **Slice B 一到就删掉这个 ignore**。这条边本身会**留在函数内**——它是打破三环的那一条。
    from pi_durable.harness.generation import start_run  # type: ignore[attr-defined]

    await start_run(tx, conversation_id, live, users)


def _strict_json(value: Any) -> Any:
    """草稿的严格 JSON 形态（``copyJson(draft, {omitUndefinedProperties: true})``）。

    ⚠️ **偏离**：JS 的 ``undefined`` 与 ``null`` 是两个东西（前者整个键省掉、后者保留），
    而 Python 的 ``None`` 把两者合一。本包按 **R23** 的同一取舍：**顶层把值为 ``None``
    的键整个省掉**——因为对**宿主传进来的草稿**而言，「显式写了 null」没有意义，
    而那些字段（``entry`` / ``whenBusy`` / ``requestId``）的上游类型都是 ``?:`` 可选。

    ⚠️ 只处理**顶层**：嵌套结构里的 ``None`` 是数据，不能动。
    """
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if v is not None}
    return copy_json(value) if not isinstance(value, (str, int, float, bool)) else value


class SubmissionHandle:
    """一个提交的句柄（``submissions.ts:123-144``）。宿主拿它查、等、撤回。"""

    __slots__ = ("_submissions", "id")

    def __init__(self, id: int, submissions: Submissions) -> None:
        self.id = id
        self._submissions = submissions

    async def status(self, context: Any) -> SubmissionRecord:
        return await self._submissions.status(self.id, context)

    async def wait(self, context: Any) -> dict[str, Any]:
        return await self._submissions.wait(self.id, context)

    async def abort(self, context: Any) -> AbortResult:
        result = await self._submissions.abort(self.id, context)
        if result == "not_found":
            raise RuntimeError(f"Submission {self.id} does not exist")
        return result


class Submissions:
    """一个 Harness 的持久提交的**准入、等待与撤回**（``submissions.ts:33-121``）。"""

    def __init__(
        self,
        session: SessionImpl,
        storage: Storage,
        now: Any,
        queue_modes: Any,
        resume: Any,
    ) -> None:
        self._session = session
        self._storage = storage
        self._now = now
        self._queue_modes = queue_modes
        self._resume = resume
        self._waiters: Waiters[int, dict[str, Any]] = Waiters()
        self._closed = False
        session.observe_commits(lambda publication, _context: self._observe(publication))
        session.subscribe_close(self._on_close)

    def _on_close(self) -> None:
        self._closed = True
        self._waiters.reject_all(closed_error(self._session))

    async def submit(
        self, conversation_id: int, draft: dict[str, Any], context: Any
    ) -> SubmissionHandle:
        """在**一次提交里**准入一个提交（``submissions.ts:60-66``）。"""
        self._resume()
        id = await self._session.commit_with(
            lambda tx: admit_submission(
                tx, conversation_id, draft, self._now(), self._queue_modes()
            ),
            context,
        )
        return SubmissionHandle(id, self)

    async def get(self, id: int, context: Any) -> SubmissionHandle | None:
        """一个已存在提交的句柄，或 ``None``（``submissions.ts:69-72``）。"""
        record = await self._session.read_on_line(lambda: self._storage.submission(id, context))
        return None if record is None else SubmissionHandle(record["id"], self)

    async def status(self, id: int, context: Any) -> SubmissionRecord:
        """一条提交记录；不存在时报错（``submissions.ts:74-78``）。"""
        record = await self._session.read_on_line(lambda: self._storage.submission(id, context))
        if record is None:
            raise RuntimeError(f"Submission {id} does not exist")
        return record

    async def wait(self, id: int, context: Any) -> dict[str, Any]:
        """等一个提交**结算**（``submissions.ts:80-94``）。

        ⚠️ **查与登记在同一条会话线上完成**：否则一次结算发布可能正好落在两者之间，
        于是这个等待**永远**不会被唤醒。
        """
        self._resume()

        async def job() -> dict[str, Any]:
            record = await self._storage.submission(id, context)
            if record is None:
                raise RuntimeError(f"Submission {id} does not exist")
            if is_settled(record):
                return {"settled": record}
            # 关闭会**同步**拒绝已登记的等待者，而且可能在这次读期间开始。
            if self._closed:
                raise closed_error(self._session)
            return {"waiter": self._waiters.add(id, context)}

        found: dict[str, Any] = await self._session.read_on_line(job)
        if "settled" in found:
            return cast(dict[str, Any], found["settled"])
        return cast(dict[str, Any], await found["waiter"])

    async def abort(self, id: int, context: Any, conversation_id: int | None = None) -> AbortResult:
        """撤回一个**排队中**的提交并移除它的收件箱项（``submissions.ts:97-111``）。

        已落地的输入与已结算的提交**如实报告**，不假装撤回成功。
        """

        async def change(tx: Any) -> AbortResult:
            record = await tx.submission(id)
            if record is None or (
                conversation_id is not None and record["conversationId"] != conversation_id
            ):
                return "not_found"
            if record["status"] == "queued":
                tx.settle_submission(id, {"status": "unanswered", "reason": "aborted"})
                await remove_inbox_item(tx, record["conversationId"], id)
                return "aborted"
            return "already_placed" if record["status"] == "placed" else "settled"

        result: AbortResult = await self._session.commit_with(change, context)
        return result

    def _observe(self, publication: dict[str, Any]) -> None:
        """提交发布里**结算了**的提交唤醒对应的等待者（``submissions.ts:113-120``）。"""
        for change in publication["changes"]:
            if change["type"] != "submission":
                continue
            value = change["value"]
            if is_settled(value):
                self._waiters.resolve(value["id"], value)

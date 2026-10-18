"""`pi_durable` 的**端到端测试基建**（真 Session + 真调度器）。

⚠️ **为什么它不在 `tests/conftest.py`**：本仓**根目录已经有一个 `conftest.py`**，
于是测试文件里的 `from conftest import ...` 会解析到**根那个**（报 `ImportError`）。
⇒ 放到 `testing/` 下，**按包路径导入**（`from pi_durable.testing.harness_e2e import ...`），
这样它在任何 import 模式下都稳定。

⚠️ **抽它的理由**：`ToolTask` 的端到端测试连续两轮各花掉 5~6 次「跑→改」，
**全部**栽在同一批坑上；而 `GenerationTask` / `CompactionTask` 的 E2E **会再走一遍同一条路**。
⇒ **抽一次 = 省两轮 × 6 次迭代。**

每个 helper 的 docstring 都写明**它替你处理了哪些坑**，这样下一个人不会重踩。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pi_ai import TextContent
from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.harness.scheduler import TaskScheduler, TaskSchedulerOptions
from pi_durable.harness.tool import ToolTask
from pi_durable.harness.types import Agent, Extension, ToolExecutionResult, ToolRegistration
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage

#: 真工具被调用的记录。**测试要在每个用例前清空它。**
CALLS: list[dict[str, Any]] = []


def make_tool(name: str = "read", **kwargs: Any) -> ToolRegistration[Any]:
    """一个**真工具**（不是 mock）：记录调用、返回一个结构化值。

    ⚠️ `replay="safe"` 是**故意的**——它让 `ToolTask` 的 `execute` 阶段在恢复时**允许重跑**，
    而那正是那条路径的分支条件。
    """

    async def execute(args: Any, api: Any, context: Any) -> ToolExecutionResult[Any]:
        CALLS.append({"name": name, "args": args})
        return ToolExecutionResult[Any](
            output=[TextContent(type="text", text=f"{name} ran")], structured_output={"ok": True}
        )

    fields: dict[str, Any] = {
        "name": name,
        "description": name,
        "parameters": {"type": "object", "properties": {}},
        "execute": execute,
        "replay": "safe",
    }
    fields.update(kwargs)
    return ToolRegistration(**fields)


class FakeSnapshot:
    """`RegistrySnapshot` 的最小实现——**6 个方法都在**（契约是完整形状）。"""

    def __init__(self, tasks: dict[str, Any], extensions: list[Any] | None = None) -> None:
        self._tasks = tasks
        self._extensions = extensions or []

    def installed(self) -> list[Any]:
        return list(self._extensions)

    def extension(self, name: str) -> Any:
        return next((e for e in self._extensions if e.name == name), None)

    def tools(self) -> list[Any]:
        return []

    def sections(self) -> list[Any]:
        return []

    def tasks(self) -> list[Any]:
        return list(self._tasks.values())

    def task(self, name: str) -> Any:
        return self._tasks.get(name)


class FakeRegistry:
    def __init__(self, snapshot: FakeSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> Any:
        return self._snapshot

    def subscribe(self, listener: Any) -> Any:
        return lambda: None


def make_settings() -> dict[str, Any]:
    return {
        "contextRetentionMs": 600_000,
        "progress": {"partialIntervalMs": 100, "outputIntervalMs": 100},
        "steeringMode": "one-at-a-time",
        "followUpMode": "one-at-a-time",
    }


async def make_harness(backend: str, tmp_path: Path, tools: list[ToolRegistration[Any]]) -> Any:
    """真 `Session`（两个后端之一）+ 真 `TaskScheduler`（**`open` 由测试决定时机**）。

    ⚠️ **它替你处理的坑**：

    * `TaskSchedulerOptions` 有 **13 个字段**，其中 `context` 必须是**真的**
      （`with_abort_signal` 拿 `None` 会炸）；
    * `now` 要返回 **int 毫秒**——返回 float 会让 `timestamp` 变成 `1.0`，
      而那是**序列化警告**（本文件的 autouse fixture 会把它变成失败）；
    * `settle_outcome` 必须接上 `live.settle_scheduler_outcome`，
      否则「终局时做 Harness 清理」这一步是空的；
    * 调度器**默认不使能**——测试要自己 `resume()`。

    ⚠️ **`open` 故意不在这里调**：`open` 会做一次加载提交，时机对某些测试有意义
    （例如「先 `abort` 再 `open`」）。
    """
    storage = MemoryStorage() if backend == "memory" else await open_sqlite(tmp_path)
    session = SessionImpl(storage)
    agent = Agent[Any](tools=tools, callable=tools, extensions=[Extension(name="demo")])
    # ⚠️ **三个内建任务都要注册**，否则 `_resolve` 会把它们判成 `missing_task`
    # 而任务**直接被孤立**（`orphaned`），测试就测不到真正的 phase。
    from pi_durable.harness.compaction import CompactionTask
    from pi_durable.harness.generation import GenerationTask

    snapshot = FakeSnapshot(
        {"pi.tool": ToolTask, "pi.generation": GenerationTask, "pi.compaction": CompactionTask},
        [Extension(name="demo")],
    )

    async def agent_resolver(conversation_id: int, snap: Any, context: Any) -> Any:
        return agent

    async def env_resolver(conversation_id: int, context: Any) -> Any:
        return None

    from pi_durable.harness.live import settle_scheduler_outcome

    async def settle_outcome(tx: Any, record: Any, outcome: Any) -> None:
        await settle_scheduler_outcome(tx, record, outcome)

    async def withdraw_inputs(tx: Any, conversation_id: int) -> None:
        return None

    def conversation(id: int, binding: Any, context: Any) -> Any:
        return None

    options = TaskSchedulerOptions(
        session=session,
        storage=storage,
        registry=FakeRegistry(snapshot),
        models=type("M", (), {})(),
        agent=agent_resolver,
        settings=make_settings,
        env=env_resolver,
        now=lambda: 1,
        report=lambda error: None,
        settle_outcome=settle_outcome,
        withdraw_inputs=withdraw_inputs,
        conversation=conversation,
        context=CTX,
    )
    return session, TaskScheduler(options)


async def open_sqlite(tmp_path: Path) -> Any:
    from pi_durable.storage.sqlite.node import open_node_sqlite_storage

    return await open_node_sqlite_storage(str(tmp_path / "durable.db"))


async def seed_call(
    session: SessionImpl, tool_name: str = "read", call_id: str = "c1"
) -> tuple[int, int]:
    """造一个会话 + 一条带工具调用的助手条目 + 一个 `pi.tool` 任务。返回 ``(task_id, entry_id)``。

    ⚠️ **它替你处理的坑**：

    * ``create_root_conversation()`` 返回的是**记录**，不是 id ——
      直接当 id 用会报 `TypeError: unhashable type: 'dict'`；
    * ``append_entry`` 的参数顺序是本端的 ``(conversation_id, value, token)``，
      **不是**上游的 ``(token, conversationId, value)``；
    * ⚠️ **`pi.live` 的工具槽位是「准入时」建的**（`agent`/`submissions` 那条路）。
      **直接建任务不会有槽位** —— 所以**不要断言槽位里的东西**，
      要断言这条路径真的产生的：**结果条目**与**任务的终局状态**。
    """
    conversation = await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    conversation_id = conversation["id"]

    async def seed(tx: Any) -> dict[str, int]:
        entry = await tx.append_entry(
            conversation_id,
            {
                "kind": "pi.assistant",
                "model": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "id": call_id, "name": tool_name, "arguments": {}},
                        ],
                        "stop_reason": "toolUse",
                        "timestamp": 1,
                    }
                ],
            },
        )
        task_id = await tx.create_task(
            ToolTask,
            {"kind": "model", "assistant": entry["id"], "callId": call_id},
            {"ownership": {"kind": "conversation"}, "conversationId": conversation_id},
        )
        return {"task": task_id, "entry": entry["id"]}

    result = await session.commit(seed, CTX)
    return result["task"], result["entry"]


async def tool_results(session: SessionImpl, conversation_id: int = 1) -> list[Any]:
    """本对话里的 `pi.tool-result` 条目。

    ⚠️ **结算的可观察证据在条目里**，不在槽位里（见 `seed_call` 的第三条坑）。
    """
    page = await session._storage.scan_entries(  # noqa: SLF001
        {"conversationId": conversation_id}, 50, None, CTX
    )
    return [e for e in page["items"] if e["kind"] == "pi.tool-result"]


class _FakeRuntimeForNested:
    """`start_nested_call` 只要这几样——**一个最小的 runtime 替身**。

    ⚠️ **它替你处理的坑**：

    * ``commit`` 的回调是 ``(tx, current)`` **两个参数**——JS 容忍少写，Python 不容忍；
    * ``conversation_id`` 必须是**真的**会话 id（``start_nested_call`` 会往它的 ``LiveDoc`` 写）；
    * 必须先 ``seed()`` 造一个**真的父任务**，否则 ``owner`` 指向不存在的任务，
      而 ``start_nested_call`` 会报 ``Task 0 does not exist``。
    """

    def __init__(self, session: Any, task_id: int = 0, conversation_id: int = 0) -> None:
        self.session = session
        self.task_id = task_id  # 由 `seed` 填真值
        self.conversation_id = conversation_id  # ⚠️ 也必须是**真**的会话 id
        self.commits: list[Any] = []

    @classmethod
    async def seed(cls, session: Any) -> _FakeRuntimeForNested:
        """⚠️ **先造一个真的父任务**——否则 `owner` 指向不存在的任务，
        而 `start_nested_call` 会报 `Task 0 does not exist`。"""
        conversation = await session.commit(lambda tx: tx.create_root_conversation(), CTX)

        async def create(tx: Any) -> int:
            # ⚠️ `int(...)`：`create_task` 返回 `Any`（它按任务定义泛型化）。
            return int(
                await tx.create_task(
                    ToolTask,
                    {"kind": "model", "assistant": 1, "callId": "c1"},
                    {"ownership": {"kind": "conversation"}, "conversationId": conversation["id"]},
                )
            )

        return cls(session, int(await session.commit(create, CTX)), int(conversation["id"]))

    async def commit(self, change: Any, context: Any = None) -> Any:
        return await self.session.commit(lambda tx: change(tx, None), CTX)


class FakeModels:
    """一个**会按事件流吐出 partial 的假 provider**。

    ⚠️ **它用真的 `pi_ai.stream.EventStream`** —— 这里假的是 **provider**（外部边界，
    没有网络就不可能真），而**不是框架内部**。按「能用真的就别用替代物」：
    **凡是本包自己的东西都用真的；只有网络那一层才是假的。**

    ⚠️ **它替你处理的坑**：

    * `stream_simple` 的第二个参数是 **pi_ai 的 `Context`**（上游传的是 `{messages}`）——
      所以断言写成 `call["context"]["messages"]`；
    * 事件是**普通的 dict** 就够（`stream_response` 用 `_field` 两种形状都吃），
      但**推送必须发生在 `stream_simple` 被调用之后**——先 `await asyncio.sleep(0)` 让协程跑起来；
    * ``mutate_partial`` 是给「**provider 还在改它自己那份**」那条测试用的：
      它在事件推送**之后**再改一次那份 partial，用来验证 `stream_response` **拷贝的是值不是引用**。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stream: Any = None

    def stream_simple(self, model: Any, context: Any, options: Any = None) -> Any:
        from pi_ai import EventStream  # ⚠️ 从**包的公开面**取（`pi_ai.stream` 不显式导出它）

        self.calls.append({"model": model, "context": context, "options": options})
        stream: Any = EventStream()
        self.stream = stream
        return stream

    def push_partial(self, text: str) -> dict[str, Any]:
        """推一个**有内容**的 partial（`stream_response` 会为它排一次 flush）。"""
        message = {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": None,
            "timestamp": 1,
        }
        self.stream.push({"type": "delta", "partial": message})
        return message

    def push_empty_partial(self) -> None:
        """推一个**空内容**的 partial——pi-ai 开头的 ``start`` 事件就是这样。

        ⚠️ 一个 **deferred 响应永远不会越过它**，所以它**从不留下 partial**。
        """
        self.stream.push(
            {"type": "start", "partial": {"role": "assistant", "content": [], "timestamp": 1}}
        )

    def finish(self, message: Any) -> None:
        self.stream.end(message)

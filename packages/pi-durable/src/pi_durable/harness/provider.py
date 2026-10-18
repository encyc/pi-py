"""对话的提供方身份 —— 移植 ``harness/provider.ts``（39 行）。

这是批次 D 里第一个**真正接上 Session 运行时**的模块：它用
``Session.snapshot`` / ``Session.commit`` + ``Transaction.doc``，
三者都在批次 B 里交付并各有测试。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypedDict, cast

from pi_ai import uuidv7
from pi_chord.context import Context
from pi_durable.documents import define_doc

__all__ = ["ProviderDoc", "ProviderRuntime", "ProviderState", "ensure_provider_session_id"]


class ProviderState(TypedDict):
    """一个对话的、面向提供方的**稳定身份**（``provider.ts:9-11``）。"""

    sessionId: str


#: 内建的提供方状态；**每个分叉都从一个全新身份开始**，而不是复制它的父（``provider.ts:14-22``）。
#:
#: ``history: "latest"`` + ``fork: "initial"`` 是这里的关键：身份必须是**当前**值，
#: 而且分叉要**重新初始化**——沿用父会话的 session ID 会让两次会话在提供方那边被认成同一个。
#: ``checkpointWhen: () => True`` 让它每次都写 base，不做增量。
ProviderDoc = define_doc(
    {
        "kind": "pi.provider",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda seed=None: {"sessionId": uuidv7()},
        "checkpointWhen": lambda value, ops, info: True,
    }
)


class ProviderRuntime(Protocol):
    """``ensure_provider_session_id`` 需要的**窄运行时面**（``provider.ts:26-38``）。

    上游收的是完整的 ``TaskRuntime``；这里只声明它真正用到的三样。
    完整的 ``TaskRuntime`` 契约属于批次 D 的后续步骤——**窄提取而不是先猜形状**，
    与批次 D-1 对 ``harness/types.py`` 的做法一致。
    """

    @property
    def conversation_id(self) -> int:
        """这次运行所属的对话（``provider.ts:32`` 的 ``runtime.conversation_id``）。

        字段名走 snake_case：这是**本包自己的** Protocol，不是持久化记录
        （与 ``EnvConformanceOptions`` 同一条约定）。将来真正的 ``TaskRuntime`` 落地时，
        这个 Protocol 会被它替换。
        """
        ...

    def snapshot(self, token: Any, *args: Any) -> Awaitable[Any]:
        """已提交文档读（``provider.ts:31``）。"""
        ...

    def commit(self, change: Callable[[Any], Any], context: Context) -> Awaitable[Any]:
        """一次原子事务（``provider.ts:34``）。"""
        ...


async def ensure_provider_session_id(runtime: ProviderRuntime, context: Context) -> str:
    """返回**已持久化**的身份，正常路径下不写任何东西（``provider.ts:24-38``）。

    没有 ``pi.provider`` 的老对话会走一次迁移提交——那次提交里的 ``tx.doc()``
    会在提供方请求开始**之前**跑掉 ``initial()``，也就是生成身份。
    """
    existing = await runtime.snapshot(ProviderDoc, runtime.conversation_id, context)
    if existing is not None:
        return cast(str, existing["sessionId"])

    created: str | None = None

    async def change(tx: Any) -> None:
        nonlocal created
        draft = await tx.doc(ProviderDoc, runtime.conversation_id)
        created = cast(str, draft["sessionId"])
        return None

    await runtime.commit(change, context)
    if created is None:
        raise RuntimeError(f"Conversation {runtime.conversation_id} has no provider session ID")
    return created

"""🚧 **`select_cut` 为什么找不到切点？** —— 一个**观察工具**，不是猜。

⚠️ **为什么要留这个脚本**：我为同一条测试**猜了两轮夹具**（"加条目数应该就行"），
两轮都失败。**同一个夹具试错两次还没成，就停止猜测，改为"打印它实际看到的东西"。**
（次数阈值比"要记得观察"更可执行 —— 它不依赖你当时是否想起来。）

将来改 `select_cut` / `is_candidate` 时，这个脚本还能用。

用法：``uv run python packages/pi-durable/scripts/diagnose_select_cut.py``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pi_chord.context import BACKGROUND_CONTEXT as CTX
from pi_durable.entries import AssistantEntry, UserEntry
from pi_durable.harness.compaction import is_candidate, select_cut
from pi_durable.session import SessionImpl
from pi_durable.storage.memory import MemoryStorage


async def main() -> None:
    session = SessionImpl(MemoryStorage())
    conversation = await session.commit(lambda tx: tx.create_root_conversation(), CTX)
    conversation_id = conversation["id"]

    async def seed(tx: Any) -> None:
        for i in range(6):
            await tx.append_entry(
                conversation_id,
                {"model": [{"role": "user", "content": f"question {i}", "timestamp": i + 1}]},
                UserEntry,
            )
            await tx.append_entry(
                conversation_id,
                {
                    "model": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": f"answer {i} " * 200}],
                            "timestamp": i + 2,
                        }
                    ]
                },
                AssistantEntry,
            )

    await session.commit(seed, CTX)

    # ⚠️ 用**真实**的运行时读（与 `_RuntimeView.context` 同一条路）。
    from pi_durable.harness.context import read_context_from

    view, _range = await read_context_from(
        session, session._storage, conversation_id, CTX, None, None
    )  # noqa: SLF001

    print(f"entries: {len(view['entries'])}")
    print(f"contributions 块数: {len(view['contributions'])}")
    print()
    for index, block in enumerate(view["contributions"]):
        roles = [_field(m, "role") for m in block]
        kinds = [e["kind"] for e in view["entries"][index : index + 1]]
        print(
            f"块 #{index}: 条目={kinds} 角色={roles} "
            f"is_candidate={is_candidate(view['contributions'], index)}"
        )
    print()
    for keep in (1, 100, 10_000, 100_000):
        print(f"select_cut(keepRecentTokens={keep}) = {select_cut(view, keep)}")


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


if __name__ == "__main__" and "--mirror" not in __import__("sys").argv:
    asyncio.run(main())


TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"


async def mirror_the_test() -> None:
    """⚠️ **复刻那条失败的测试，但把 `classify` 内部看到的东西打出来。**

    「同一个夹具试错两次还没成，停止猜测、改为打印它实际看到的东西」——
    这是第二次观察（第一次只看了裸的 `select_cut`，没看**经过 `runtime.context(…, at=cutoff)` 之后**的视图）。
    """
    import sys

    # ⚠️ `Path(__file__)` 在异步函数里会触发 ASYNC240（它建议用 anyio.Path）——
    # 这里只是构造一个 `sys.path` 项，**同步做掉**即可：挪到函数外。
    sys.path.insert(0, str(TESTS_DIR))
    from test_harness_generation_helpers import _real_runtime  # noqa: PLC2701

    from pi_durable.harness.generation import classify
    from pi_durable.testing.harness_e2e import make_harness, make_tool

    session, scheduler = await make_harness("memory", Path("/tmp"), [make_tool()])
    runtime = await _real_runtime(session, scheduler, ["a"])
    conversation_id = runtime.conversation_id
    scheduler._settings = lambda: {  # noqa: SLF001
        "contextRetentionMs": 600_000,
        "progress": {"partialIntervalMs": 100, "outputIntervalMs": 100},
        "compaction": {
            "enabled": True,
            "reserveTokens": 1000,
            "backgroundTokens": 100,
            "keepRecentTokens": 1,
        },
        "retry": {"enabled": False, "maxRetries": 0},
        "stream": {},
        "toolExecution": "sequential",
    }

    async def seed(tx: Any) -> None:
        for i in range(6):
            await tx.append_entry(
                conversation_id,
                {"model": [{"role": "user", "content": f"question {i}", "timestamp": i + 1}]},
                UserEntry,
            )
            await tx.append_entry(
                conversation_id,
                {
                    "model": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": f"answer {i} " * 200}],
                            "timestamp": i + 2,
                        }
                    ]
                },
                AssistantEntry,
            )

    await session.commit(seed, CTX)
    page = await session._storage.scan_entries({"conversationId": conversation_id}, 50, None, CTX)  # noqa: SLF001
    # ⚠️ `scan_entries` **最新在前** —— `[-1]` 会取到最早那条（这正是那条测试的 bug）。
    last = max(e["id"] for e in page["items"])
    print(
        f"\n=== 复刻测试：conversation={conversation_id} 条目 {len(page['items'])} 条，cutoff={last} ==="
    )

    # ⚠️ **`classify` 里读到的视图**（带 cutoff）——这才是它实际看到的。
    view = await runtime.context(conversation_id, CTX, {"at": last})
    print(f"视图（at={last}）: entries={len(view['entries'])} 块={len(view['contributions'])}")
    print(f"select_cut(keep=1) = {select_cut(view, 1)}")
    print(f"settings.compaction = {runtime.settings['compaction']}")

    message = {
        "role": "assistant",
        "content": [],
        "stop_reason": "error",
        "error_message": "prompt is too long: 200000 tokens > 100000 maximum",
        "timestamp": 1,
    }
    await classify(
        runtime,
        {"attempt": 1, "model": {"provider": "p", "modelId": "m"}, "cutoff": last},
        message,
        CTX,
    )
    record = await session._storage.task(runtime.task_id, CTX)  # noqa: SLF001
    print(
        f"classify 之后: status={record['state']['status']} checkpoint={record['state'].get('checkpoint')}"
    )


if __name__ == "__main__" and "--mirror" in __import__("sys").argv:
    asyncio.run(mirror_the_test())

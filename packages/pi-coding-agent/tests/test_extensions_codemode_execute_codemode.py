"""Tier-B④-2b-1：`execute_codemode`（上游 `execute.ts:440-574`）。

**同步沙箱 + executor**：本仓 `CodemodeSandbox.execute` 是同步阻塞调用，跑在
`loop.run_in_executor` 里。`asyncio.to_thread`/executor **不可取消** —— 取消外层任务
不会中断线程，所以**中断只能靠 `abort_event`**。下面的 abort 测试按这条来验：
断言脚本**在预期时间内结束**且结果是**被中断的形态**，而不是断言 `event.is_set()`。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from pi_coding_agent.extensions.codemode import execute as codemode_execute


async def _run(code: str, **kwargs: Any) -> dict[str, Any]:
    return await codemode_execute.execute_codemode("call-1", {"code": code}, **kwargs)  # type: ignore[return-value]


def _body(result: dict[str, Any]) -> str:
    return "\n".join(block.text for block in result["content"][1:])


# ============================================================
# 基本路径
# ============================================================


async def test_success_header_and_value():
    result = await _run("return 1 + 1")
    assert result.get("is_error") is not True
    assert result["content"][0].text.startswith("Script completed\nWall time ")
    assert "Output:\n" in result["content"][0].text
    assert _body(result).strip() == "2"


async def test_script_error_is_reported_with_partial_output_and_calls():
    result = await _run('text("before"); throw new Error("boom")')
    assert result["is_error"] is True
    assert result["content"][0].text.startswith("Script failed")
    body = _body(result)
    assert "before" in body  # 失败仍保留部分输出
    assert "Script error:" in body
    assert "Error: boom" in body
    assert "No tool calls were made." in body


async def test_details_carry_the_call_snapshot():
    result = await _run("return 1")
    assert result["details"].calls == []


# ============================================================
# 源码解析：失败即拒绝执行（不落到默认值）
# ============================================================


async def test_empty_source_is_rejected_before_running():
    with pytest.raises(Exception, match="Expected JavaScript source text"):
        await _run("   ")


async def test_misplaced_options_line_is_rejected():
    """options 行必须在首行 —— 否则会被静默忽略（上游 #10631）。"""
    with pytest.raises(Exception, match="first line"):
        await _run('text("x")\n// @options: {"max_output_tokens": 10}')


# ============================================================
# 输出预算
# ============================================================


async def test_output_budget_truncates_and_spills():
    result = (
        await _run('text("x".repeat(200000))', tool_input_options=None)
        if False
        else await codemode_execute.execute_codemode(  # type: ignore[func-returns-value]
            "c", {"code": '// @options: {"max_output_tokens": 50}\ntext("x".repeat(20000))'}
        )
    )
    body = _body(result)
    assert "Warning: truncated output" in body
    assert "[Full output: " in body
    path = result["details"].full_output_path
    assert path
    import os

    os.unlink(path)


# ============================================================
# 中断：**真的触发一次**
# ============================================================


async def test_abort_actually_stops_a_running_script():
    """**真实触发**：长脚本 + 从另一个任务 set → 断言**确实被中断**。

    反面情形（只断言 `event.is_set()`）无法发现「设了但没生效」——
    而那正是桥接晚一拍的表现。
    """
    signal = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.2)
        signal.set()

    stopper = asyncio.ensure_future(stop_soon())
    started = time.perf_counter()
    try:
        result = await codemode_execute.execute_codemode(
            "c", {"code": "while (true) {}"}, signal=signal
        )
    finally:
        stopper.cancel()
    elapsed = time.perf_counter() - started

    # 1) 在预期时间内结束（没被中断的话 `while(true){}` 会一直跑下去）
    assert elapsed < 10, f"abort 没生效，脚本跑了 {elapsed:.1f}s"
    # 2) 结果是**被中断的形态**
    assert result["is_error"] is True
    body = _body(result)
    assert "Script aborted" in body or "aborted" in body.lower()


async def test_abort_bridge_sets_the_threading_event(monkeypatch):
    """桥接本身：`asyncio.Event` 置位后，传给沙箱的 `threading.Event` 必须被置位。"""
    seen: list[bool] = []
    real = codemode_execute.CodemodeSandbox

    class _Spy:
        """`abort_event` 是传给 **`execute()`** 的，不是构造参数 —— 别抓错地方。"""

        def __init__(self, **kwargs: Any) -> None:
            self._inner = real(**kwargs)
            self._abort: Any = None

        def execute(self, code: str, **kwargs: Any) -> Any:
            self._abort = kwargs.get("abort_event")
            return self._inner.execute(code, **kwargs)

        def close(self) -> None:
            assert self._abort is not None, "execute() 没收到 abort_event"
            seen.append(self._abort.is_set())
            self._inner.close()

    monkeypatch.setattr(codemode_execute, "CodemodeSandbox", lambda **kwargs: _Spy(**kwargs))
    signal = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.2)
        signal.set()

    stopper = asyncio.ensure_future(stop_soon())
    try:
        await codemode_execute.execute_codemode("c", {"code": "while (true) {}"}, signal=signal)
    finally:
        stopper.cancel()
    assert seen == [True], f"桥接没把 abort 传到位：{seen}"


# ============================================================
# 配额
# ============================================================


async def test_worker_quota_is_released_after_a_run():
    await _run("return 1")
    assert codemode_execute.ACTIVE_CODEMODE_WORKERS == 0


async def test_worker_quota_is_released_even_when_the_script_fails():
    await _run('throw new Error("x")')
    assert codemode_execute.ACTIVE_CODEMODE_WORKERS == 0


async def test_worker_limit_throws_instead_of_queueing():
    """上游在这里 `throw`，消息是 *Retry after they exit.* —— **不排队**。"""
    release_a = codemode_execute._acquire_worker()
    release_b = codemode_execute._acquire_worker()
    try:
        with pytest.raises(
            codemode_execute.CodemodeWorkerLimitError, match="Retry after they exit"
        ):
            codemode_execute._acquire_worker()
    finally:
        release_a()
        release_b()
    assert codemode_execute.ACTIVE_CODEMODE_WORKERS == 0

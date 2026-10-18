"""``timeout_ms`` 的「无期限」语义：``None`` 与**非有限值**等价。

上游用 ``Number.POSITIVE_INFINITY`` 表示不设期限（``execute.ts:510`` 的
``sourceOptions.timeoutMs ?? Number.POSITIVE_INFINITY``）。本端如果直接把 ``inf`` 交给
``threading.Event.wait(timeout=)``，会在 ``waiter.acquire(True, timeout)`` 里抛
``OverflowError: timestamp out of range for platform time_t`` —— 而且**抛在看门狗线程内部**：
主路径照常返回结果，只有 stderr 留下 traceback，于是「超时保护已失效」是**静默**的。
本文件把这条钉死。
"""

from __future__ import annotations

import math
import threading

from pi_codemode import CodemodeSandbox
from pi_codemode.run import _deadline_seconds


def test_deadline_seconds_treats_none_and_non_finite_alike():
    assert _deadline_seconds(None) is None
    assert _deadline_seconds(math.inf) is None
    assert _deadline_seconds(-math.inf) is None  # 负无穷同样不是期限
    assert _deadline_seconds(math.nan) is None
    assert _deadline_seconds(1000) == 1.0
    assert _deadline_seconds(0) == 0.0


def _watchdog_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if "watchdog" in t.name]


def test_infinite_timeout_runs_without_a_watchdog():
    """``inf`` 时**不该起看门狗** —— 起了它就会在 ``wait(inf)`` 上溢出。"""
    before = len(_watchdog_threads())
    sandbox = CodemodeSandbox(timeout_ms=math.inf)
    try:
        result = sandbox.execute("return 1 + 1")
        assert result.ok, result.error
        assert result.value == 2
        assert len(_watchdog_threads()) == before, "inf 不该起看门狗线程"
    finally:
        sandbox.close()


def test_default_and_none_timeout_still_work():
    for kwargs in ({}, {"timeout_ms": None}):
        sandbox = CodemodeSandbox(**kwargs)
        try:
            result = sandbox.execute("return 'ok'")
            assert result.ok, result.error
            assert result.value == "ok"
        finally:
            sandbox.close()


def test_finite_timeout_still_fires():
    """反面守护：**不要**为了修 ``inf`` 而把看门狗整个关掉。"""
    sandbox = CodemodeSandbox(timeout_ms=1.0)
    try:
        result = sandbox.execute("while (true) {}")
        assert not result.ok
        assert result.error is not None
        assert result.error.kind == "timeout"
    finally:
        sandbox.close()


def test_infinite_timeout_does_not_print_an_overflow_traceback(capsys):
    """本机看不见的那种失败：主路径 OK，但 stderr 有 traceback。"""
    sandbox = CodemodeSandbox(timeout_ms=math.inf)
    try:
        assert sandbox.execute("return 1").ok
    finally:
        sandbox.close()
    captured = capsys.readouterr()
    assert "OverflowError" not in captured.err
    assert "timestamp out of range" not in captured.err

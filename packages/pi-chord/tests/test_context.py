"""``pi_chord.context`` 单元测试 — 对照上游 ``chord/src/context/index.ts`` 的语义。"""

from __future__ import annotations

import asyncio

import pytest

from pi_chord import Context, ContextKey
from pi_chord.context import (
    BACKGROUND_CONTEXT,
    TODO_CONTEXT,
    AbortController,
    AbortError,
    AbortSignal,
    BaseContext,
    await_with_context,
    create_context_key,
    with_abort_signal,
    with_cancel,
    with_context_value,
    without_abort_signal,
)


def test_empty_contexts_carry_nothing() -> None:
    key: ContextKey[int] = create_context_key("example")
    for context in (BACKGROUND_CONTEXT, TODO_CONTEXT):
        assert isinstance(context, BaseContext)
        assert context.abort_signal is None
        assert context.value(key) is None
    assert str(BACKGROUND_CONTEXT) == "[Context BACKGROUND_CONTEXT]"
    assert str(TODO_CONTEXT) == "[Context TODO_CONTEXT]"


def test_keys_are_identified_by_token_not_description() -> None:
    first: ContextKey[int] = create_context_key("same")
    second: ContextKey[int] = create_context_key("same")
    assert first.token is not second.token
    assert first.description == second.description == "same"

    context = with_context_value(first, 1, BACKGROUND_CONTEXT)
    assert context.value(first) == 1
    assert context.value(second) is None


def test_with_context_value_shadows_and_falls_back_to_parent() -> None:
    key: ContextKey[str] = create_context_key("k")
    other: ContextKey[int] = create_context_key("other")
    parent = with_context_value(key, "parent", BACKGROUND_CONTEXT)
    child = with_context_value(key, "child", parent)

    assert parent.value(key) == "parent"
    assert child.value(key) == "child"
    assert child.value(other) is None

    grandchild = with_context_value(other, 7, child)
    assert grandchild.value(key) == "child"
    assert grandchild.value(other) == 7


def test_deriving_a_value_does_not_mutate_the_parent() -> None:
    key: ContextKey[str] = create_context_key("k")
    parent: Context = BACKGROUND_CONTEXT
    with_context_value(key, "value", parent)
    assert parent.value(key) is None


def test_string_form_describes_the_derivation() -> None:
    key: ContextKey[str] = create_context_key("skills")
    anonymous: ContextKey[str] = create_context_key("")
    assert str(with_context_value(key, "v", BACKGROUND_CONTEXT)).endswith(".WithValue(skills)")
    assert str(with_context_value(anonymous, "v", BACKGROUND_CONTEXT)).endswith(
        ".WithValue(anonymous)"
    )


def test_with_abort_signal_combines_parent_and_child() -> None:
    parent_controller = AbortController()
    parent = with_abort_signal(parent_controller.signal, BACKGROUND_CONTEXT)
    assert parent.abort_signal is parent_controller.signal

    child_controller = AbortController()
    child = with_abort_signal(child_controller.signal, parent)
    combined = child.abort_signal
    assert combined is not None
    assert not combined.aborted

    # Cancelling the child leaves the parent usable.
    child_controller.abort()
    assert combined.aborted
    assert not parent_controller.signal.aborted

    # A later parent cancellation still reaches the derived signal.
    second_child = with_abort_signal(AbortController().signal, parent)
    parent_controller.abort("parent reason")
    assert second_child.abort_signal is not None
    assert second_child.abort_signal.aborted


def test_with_abort_signal_propagates_parent_cancellation() -> None:
    parent_controller = AbortController()
    parent = with_abort_signal(parent_controller.signal, BACKGROUND_CONTEXT)
    child = with_abort_signal(AbortController().signal, parent)
    parent_controller.abort()
    assert child.abort_signal is not None and child.abort_signal.aborted


def test_abort_signal_any_prefers_an_already_aborted_source() -> None:
    controller = AbortController()
    controller.abort("boom")
    combined = AbortSignal.any([controller.signal, AbortController().signal])
    assert combined.aborted
    assert combined.reason == "boom"


def test_abort_signal_any_without_sources_never_aborts() -> None:
    combined = AbortSignal.any([])
    assert not combined.aborted
    assert combined.reason is None


def test_abort_listeners_fire_once_in_order() -> None:
    controller = AbortController()
    calls: list[str] = []
    controller.signal.add_listener(lambda: calls.append("first"))
    controller.signal.add_listener(lambda: calls.append("second"))
    controller.abort()
    controller.abort()
    assert calls == ["first", "second"]
    assert isinstance(controller.signal.reason, AbortError)


def test_abort_listener_registered_after_abort_is_not_called() -> None:
    controller = AbortController()
    controller.abort()
    calls: list[str] = []
    controller.signal.add_listener(lambda: calls.append("late"))
    assert calls == []


def test_remove_listener_ignores_unknown_listeners() -> None:
    controller = AbortController()
    calls: list[str] = []

    def listener() -> None:
        calls.append("called")

    controller.signal.add_listener(listener)
    controller.signal.remove_listener(listener)
    controller.signal.remove_listener(listener)
    controller.abort()
    assert calls == []


def test_without_abort_signal_drops_cancellation_only() -> None:
    key: ContextKey[str] = create_context_key("k")
    controller = AbortController()
    cancellable = with_context_value(
        key, "kept", with_abort_signal(controller.signal, BACKGROUND_CONTEXT)
    )
    cleanup = without_abort_signal(cancellable)
    assert cleanup.abort_signal is None
    assert cleanup.value(key) == "kept"


def test_with_cancel_derives_an_independent_child() -> None:
    scope = with_cancel(BACKGROUND_CONTEXT)
    assert scope.context.abort_signal is not None
    assert not scope.context.abort_signal.aborted
    scope.cancel()
    assert scope.context.abort_signal.aborted
    assert BACKGROUND_CONTEXT.abort_signal is None


def test_with_cancel_keeps_parent_values() -> None:
    key: ContextKey[str] = create_context_key("k")
    scope = with_cancel(with_context_value(key, "v", BACKGROUND_CONTEXT))
    assert scope.context.value(key) == "v"
    scope.cancel("stopped")
    assert scope.context.abort_signal is not None
    assert scope.context.abort_signal.reason == "stopped"


async def test_await_with_context_returns_the_result_without_cancellation() -> None:
    async def work() -> int:
        return 42

    assert await await_with_context(work(), BACKGROUND_CONTEXT) == 42


async def test_await_with_context_raises_immediately_when_already_aborted() -> None:
    controller = AbortController()
    controller.abort()
    context = with_abort_signal(controller.signal, BACKGROUND_CONTEXT)
    started = asyncio.Event()

    async def work() -> int:
        started.set()
        return 1

    with pytest.raises(AbortError):
        await await_with_context(work(), context)
    # 与上游一致：取消只作用于等待者，底层 awaitable 仍然开始并跑完。
    await asyncio.wait_for(started.wait(), timeout=1)


async def test_await_with_context_cancels_only_the_waiter() -> None:
    controller = AbortController()
    context = with_abort_signal(controller.signal, BACKGROUND_CONTEXT)
    finished = asyncio.Event()

    async def work() -> str:
        await asyncio.sleep(0.05)
        finished.set()
        return "done"

    task = asyncio.ensure_future(await_with_context(work(), context))
    await asyncio.sleep(0)
    controller.abort()
    with pytest.raises(AbortError):
        await task
    # The underlying awaitable was not cancelled by the waiter.
    await asyncio.wait_for(finished.wait(), timeout=1)


async def test_await_with_context_propagates_custom_reasons() -> None:
    controller = AbortController()
    context = with_abort_signal(controller.signal, BACKGROUND_CONTEXT)
    controller.abort(ValueError("nope"))

    async def work() -> int:
        await asyncio.sleep(1)
        return 1

    with pytest.raises(ValueError, match="nope"):
        await await_with_context(work(), context)

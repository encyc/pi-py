"""Faux provider 端到端测试：验证整个流式栈（types → events → event_stream → provider → stream 入口）。"""

from __future__ import annotations

import pytest

from pi_ai import Context, ToolCall, UserMessage, complete, get_model, stream
from pi_ai.providers.faux import FauxScript, push_script


@pytest.fixture
def faux_model():
    return get_model("faux", "faux")


@pytest.fixture
def ctx():
    return Context(messages=[UserMessage(content="hi")])


async def _collect(es):
    events = []
    async for ev in es:
        events.append(ev)
    return events


async def test_text_streaming(faux_model, ctx):
    """文本流式：start → text_start → N×text_delta → text_end → done。"""
    push_script(FauxScript(text="Hello"))
    es = stream(faux_model, ctx)
    events = await _collect(es)
    types = [e.type for e in events]
    assert types[0] == "start"
    assert types[1] == "text_start"
    assert types.count("text_delta") == 5  # "Hello" 5 字符
    assert "text_end" in types
    assert types[-1] == "done"

    msg = await es.result()
    assert msg.stop_reason == "stop"
    assert msg.content[0].type == "text"
    assert msg.content[0].text == "Hello"


async def test_tool_call_streaming(faux_model, ctx):
    """工具调用流式：start → toolcall_start → toolcall_end → done，stop_reason=toolUse。"""
    tc = ToolCall(id="c1", name="weather", arguments={"city": "SF"})
    push_script(FauxScript(tool_calls=[tc]))
    es = stream(faux_model, ctx)
    events = await _collect(es)
    types = [e.type for e in events]
    assert types == ["start", "toolcall_start", "toolcall_end", "done"]

    msg = await es.result()
    assert msg.stop_reason == "toolUse"
    block = msg.content[0]
    assert block.type == "toolCall"
    assert block.id == "c1"
    assert block.name == "weather"
    assert block.arguments == {"city": "SF"}


async def test_error_path(faux_model, ctx):
    """错误路径：只发 error 事件，stop_reason=error。"""
    push_script(FauxScript(error="boom"))
    es = stream(faux_model, ctx)
    events = await _collect(es)
    assert len(events) == 1
    assert events[0].type == "error"
    msg = await es.result()
    assert msg.stop_reason == "error"
    assert msg.error_message == "boom"


async def test_complete_non_streaming(faux_model, ctx):
    """complete 聚合得到最终消息。"""
    push_script(FauxScript(text="hi"))
    msg = await complete(faux_model, ctx)
    assert msg.content[0].text == "hi"
    assert msg.stop_reason == "stop"


async def test_empty_response(faux_model, ctx):
    """空脚本：只 start + done，无内容块。"""
    push_script(FauxScript())
    es = stream(faux_model, ctx)
    events = await _collect(es)
    types = [e.type for e in events]
    assert types == ["start", "done"]
    msg = await es.result()
    assert msg.content == []
    assert msg.stop_reason == "stop"


async def test_event_replay(faux_model, ctx):
    """events property 保留历史事件供重放。"""
    push_script(FauxScript(text="ab"))
    es = stream(faux_model, ctx)
    await _collect(es)
    assert len(es.events) == 6  # start + text_start + 2*delta + text_end + done
    assert es.events[0].partial.stop_reason == "pending"


async def test_faux_start_event_is_pending(faux_model, ctx):
    push_script(FauxScript(text="ok"))
    es = stream(faux_model, ctx)
    start_reasons = []

    async for event in es:
        if event.type == "start":
            start_reasons.append(event.partial.stop_reason)

    assert start_reasons == ["pending"]
    assert (await es.result()).stop_reason == "stop"


async def test_faux_rejects_pending_as_final_reason(faux_model, ctx):
    push_script(FauxScript(text="unfinished", stop_reason="pending"))
    es = stream(faux_model, ctx)
    events = await _collect(es)
    result = await es.result()

    assert events[-1].type == "error"
    assert result.stop_reason == "error"
    assert "without a stop reason" in (result.error_message or "")


# ============================================================
# 回归：aborted / error 必须以 ErrorEvent 终结（否则流永不结束）
# ============================================================
#
# 历史 bug：faux 对**所有**终止原因都发 DoneEvent，而 DoneEvent.reason 只接受
# stop|length|toolUse（events.py:97，与上游一致）。于是
# ``FauxScript(stop_reason="aborted")`` 在 push 时抛 ValidationError；该异常发生在
# asyncio 任务内、无人 await，流既不 done 也不 error，消费者 ``async for``
# 永久挂起。上游 faux.ts:444-449 的规则是：error/aborted → ErrorEvent。


@pytest.mark.parametrize("reason", ["aborted", "error"])
async def test_faux_terminal_error_reasons_emit_error_event(faux_model, ctx, reason):
    push_script(FauxScript(text="partial", stop_reason=reason))
    es = stream(faux_model, ctx)
    events = await _collect(es)
    result = await es.result()

    assert events[-1].type == "error"
    assert events[-1].reason == reason
    assert result.stop_reason == reason


@pytest.mark.parametrize("reason", ["stop", "length", "toolUse"])
async def test_faux_success_reasons_emit_done_event(faux_model, ctx, reason):
    push_script(FauxScript(text="ok", stop_reason=reason))
    es = stream(faux_model, ctx)
    events = await _collect(es)
    result = await es.result()

    assert events[-1].type == "done"
    assert result.stop_reason == reason


async def test_faux_aborted_stream_does_not_hang(faux_model, ctx):
    """核心回归：曾经会挂死。用 wait_for 给一个硬上限，超时即失败。"""
    import asyncio

    push_script(FauxScript(stop_reason="aborted"))
    es = stream(faux_model, ctx)
    events = await asyncio.wait_for(_collect(es), timeout=5)
    assert events[-1].type == "error"


async def test_faux_driver_failure_still_terminates_the_stream(faux_model, ctx, monkeypatch):
    """防御层：驱动任务内任何异常都必须转成 error 事件，而不是让流悬空。"""
    from pi_ai.providers import faux as faux_mod

    class _BoomError(Exception):
        pass

    def _explode(*args, **kwargs):
        raise _BoomError("kaboom")

    # 让构造 StartEvent 失败（StartEvent 在 drive() 内被调用）
    monkeypatch.setattr(faux_mod, "StartEvent", _explode)
    push_script(FauxScript(text="x"))
    es = stream(faux_model, ctx)
    events = await _collect(es)

    assert events[-1].type == "error"
    result = await es.result()
    assert result.stop_reason == "error"
    assert "kaboom" in (result.error_message or "")

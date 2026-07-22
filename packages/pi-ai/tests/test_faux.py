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

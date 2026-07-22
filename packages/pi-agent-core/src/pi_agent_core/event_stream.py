"""agent 事件流工厂。

复用 pi-ai 的 EventStream，专门化为 agent 事件。
agent_end 是终止事件，携带最终消息列表。
"""

from __future__ import annotations

from pi_ai import EventStream

from .types import AgentEvent, AgentMessage


def create_agent_stream() -> EventStream[AgentEvent, list[AgentMessage]]:
    """创建 agent 事件流。

    result() 返回 agent_end 事件携带的 messages 列表。
    """
    return EventStream()


__all__ = ["create_agent_stream"]

"""SDK 入口：create_agent_session + CodingAgent 封装。

对应上游 ``core/sdk.ts`` 的 ``createAgentSession``。

本包定位为 SDK 库，提供一个轻量的 ``CodingAgent`` —— 封装 pi-agent-core 的
Agent + 编码工具集 + 默认 system prompt，让用户几行代码就能跑起编码 agent。

与上游 AgentSession（110KB，含 session 树管理/retry/扩展等）相比，这是精简版：
聚焦核心能力（prompt + 工具执行 + 事件订阅），不含 TUI/CLI/扩展系统/session 树。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pi_agent_core import Agent, AgentOptions, AgentTool
from pi_ai import Model

from .tools import create_all_tools, create_coding_tools

#: 默认 system prompt（编码 agent）。
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful coding assistant. You can read, write, and edit files, "
    "run bash commands, and search code. Always use the provided tools to "
    "accomplish tasks. Be concise and precise."
)


class CodingAgent:
    """编码 Agent SDK 封装。

    组合 pi-agent-core Agent + 编码工具集 + 默认 system prompt。

    快速上手：
        agent = CodingAgent(model=deepseek_model, api_key="...")
        await agent.prompt("列出当前目录的 Python 文件")
    """

    def __init__(
        self,
        model: Model,
        api_key: str | None = None,
        cwd: str = ".",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        tools: list[AgentTool] | None = None,
        tool_names: list[str] | None = None,
        thinking_level: str | None = None,
    ) -> None:
        self._cwd = cwd
        # 工具集
        if tools is not None:
            agent_tools = tools
        elif tool_names is not None:
            all_tools = {t.name: t for t in create_all_tools(cwd)}
            agent_tools = [all_tools[n] for n in tool_names if n in all_tools]
        else:
            agent_tools = create_coding_tools(cwd)

        get_api_key = (lambda p: api_key) if api_key else None
        self._agent = Agent(
            AgentOptions(
                initial_state={
                    "system_prompt": system_prompt,
                    "model": model,
                    "tools": agent_tools,
                    "thinking_level": thinking_level,
                },
                get_api_key=get_api_key,
                tool_execution="parallel",
            )
        )

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def state(self) -> Any:
        return self._agent.state

    def subscribe(self, listener: Any) -> Any:
        """订阅 agent 事件。返回取消订阅函数。"""
        return self._agent.subscribe(listener)

    async def prompt(self, message: str) -> None:
        """发送 prompt，运行 agent 循环。"""
        await self._agent.prompt(message)

    async def continue_(self) -> None:
        """从当前上下文继续。"""
        await self._agent.continue_()

    def steer(self, message: str) -> None:
        """工作中注入指令。"""
        from pi_ai import TextContent, UserMessage

        self._agent.steer(
            UserMessage(
                content=[TextContent(text=message)],
                timestamp=int(asyncio.get_event_loop().time() * 1000),
            )
        )

    def follow_up(self, message: str) -> None:
        """完成后追加消息。"""
        from pi_ai import TextContent, UserMessage

        self._agent.follow_up(
            UserMessage(
                content=[TextContent(text=message)],
                timestamp=int(asyncio.get_event_loop().time() * 1000),
            )
        )

    def abort(self) -> None:
        self._agent.abort()

    async def wait_for_idle(self) -> None:
        await self._agent.wait_for_idle()

    def reset(self) -> None:
        self._agent.reset()


def create_agent_session(
    model: Model,
    api_key: str | None = None,
    cwd: str = ".",
    **kwargs: Any,
) -> CodingAgent:
    """SDK 工厂入口。创建一个 CodingAgent。

    对应上游 ``createAgentSession``，精简版。
    """
    return CodingAgent(model=model, api_key=api_key, cwd=cwd, **kwargs)


__all__ = ["CodingAgent", "create_agent_session", "DEFAULT_SYSTEM_PROMPT"]

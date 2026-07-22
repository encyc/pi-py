"""pi-agent-core: Python port of @earendil-works/pi-agent-core.

通用 agent 运行时：双层循环引擎、有状态 Agent 封装、技能加载。

对应上游 ``packages/agent``（TypeScript）。

快速上手：
    from pi_agent_core import Agent, AgentOptions, AgentTool, AgentToolResult

    class MyTool:
        name = "greet"
        description = "打招呼"
        parameters = {"type": "object", "properties": {}}
        label = "Greet"
        async def execute(self, tool_call_id, params, cancel_event, on_update):
            return AgentToolResult(content=[TextContent(text="hello!")])

    agent = Agent(AgentOptions(
        initial_state={"system_prompt": "你是有用的助手", "model": model, "tools": [MyTool()]},
    ))
    await agent.prompt("打个招呼")
"""

from __future__ import annotations

__version__ = "0.81.1"
__upstream_ref__ = "earendil-works/pi@v0.81.1"

# ---- 类型 ----
# ---- 有状态 Agent ----
from .agent import Agent, AgentOptions

# ---- 循环引擎 ----
from .agent_loop import agent_loop, agent_loop_continue

# ---- 事件流 ----
from .event_stream import create_agent_stream

# ---- harness: skills ----
from .harness.skills import (
    LoadSkillsOptions,
    Skill,
    SkillDiagnostic,
    SkillLoadResult,
    format_skill_invocation,
    format_skills_for_prompt,
    load_skill_from_file,
    load_skills,
    load_skills_from_dir,
    parse_frontmatter,
    validate_description,
    validate_name,
)
from .types import (
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentStartEvent,
    AgentState,
    AgentTool,
    AgentToolCall,
    AgentToolResult,
    AgentToolUpdateCallback,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    QueueMode,
    StreamFn,
    ToolExecutionEndEvent,
    ToolExecutionMode,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)

__all__ = [
    "__version__",
    "__upstream_ref__",
    # 类型
    "AgentContext",
    "AgentEndEvent",
    "AgentEvent",
    "AgentLoopConfig",
    "AgentMessage",
    "AgentStartEvent",
    "AgentState",
    "AgentTool",
    "AgentToolCall",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "QueueMode",
    "StreamFn",
    "ToolExecutionEndEvent",
    "ToolExecutionMode",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "TurnEndEvent",
    "TurnStartEvent",
    # 循环引擎
    "agent_loop",
    "agent_loop_continue",
    # 有状态 Agent
    "Agent",
    "AgentOptions",
    # 事件流
    "create_agent_stream",
    # harness: skills
    "Skill",
    "SkillDiagnostic",
    "SkillLoadResult",
    "LoadSkillsOptions",
    "parse_frontmatter",
    "validate_name",
    "validate_description",
    "load_skill_from_file",
    "load_skills_from_dir",
    "load_skills",
    "format_skills_for_prompt",
    "format_skill_invocation",
]

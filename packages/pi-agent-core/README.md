# pi-agent-core

Python port of [@earendil-works/pi-agent-core](https://github.com/earendil-works/pi) — General-purpose agent runtime.

## Install

```bash
pip install pi-agent-core
```

## Quick Start

```python
from pi_agent_core import Agent, AgentOptions, AgentToolResult
from pi_ai import TextContent

class WeatherTool:
    name = "get_weather"
    description = "Query weather"
    label = "Weather"
    parameters = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
        return AgentToolResult(content=[TextContent(text=f"{params['city']} sunny 25°C")])

agent = Agent(AgentOptions(
    initial_state={"system_prompt": "You are helpful", "model": model, "tools": [WeatherTool()]},
    get_api_key=lambda p: "sk-...",
))
await agent.prompt("What's the weather in Beijing?")
```

## Features

- Double-loop engine (follow-up + tool calls + steering)
- Stateful Agent class (prompt/continue_/steer/follow_up/abort)
- Harness: skills loading, session persistence, context compaction

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream

# pi-py

> [Pi](https://pi.dev) 的 Python SDK 复刻 —— AI agent 工具集：统一 LLM API、agent 运行时、编码 agent 工具集。

[Pi](https://github.com/earendil-works/pi)（原名 `badlogic/pi-mono`，作者 Mario Zechner 于 2026 年加入 [Earendil Works](https://github.com/earendil-works) 后项目迁至现址）是一套 TypeScript 的 AI agent 工具集。本仓库将其核心能力移植到 Python，**以 SDK 库形式提供，不含 CLI/TUI**。

## 同步状态

- **当前对齐版本**：[`v0.81.1`](./UPSTREAM_VERSION)（2026-07-21）
- **同步策略**：仅在上游发布 `0.x.0`（minor）时集中同步，详见 [`SYNC.md`](./SYNC.md)

| 包 | 上游对应 | 状态 | 说明 |
|---|---|---:|---|
| [`pi-ai`](./packages/pi-ai) | `@earendil-works/pi-ai` | ✅ | 统一 LLM API（OpenAI + Anthropic + retry） |
| [`pi-agent-core`](./packages/pi-agent-core) | `@earendil-works/pi-agent-core` | ✅ | agent 循环引擎 + harness（技能/会话/压缩） |
| [`pi-storage-sqlite`](./packages/pi-storage-sqlite) | `@earendil-works/pi-storage-sqlite-node` | ✅ | SQLite 会话存储后端 |
| [`pi-coding-agent`](./packages/pi-coding-agent) | `@earendil-works/pi-coding-agent` | ✅ | 编码 agent SDK（bash/read/edit/write/grep/find/ls） |
| [`pi-server`](./packages/pi-server) | `@earendil-works/pi-server` | ✅ | agent 服务化（Unix socket + JSONL + supervisor） |

## 快速上手

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/earendil-works/pi-py.git
cd pi-py
uv sync
```

### 基础 LLM 调用

```python
import asyncio
from pi_ai import stream, Context, UserMessage, Model, StreamOptions

model = Model(
    id="deepseek-chat", api="openai-completions", provider="deepseek",
    base_url="https://api.deepseek.com/v1", input=["text"],
    context_window=64000, max_tokens=8192,
)

async def main():
    ctx = Context(messages=[UserMessage(content="你好")])
    es = stream(model, ctx, StreamOptions(api_key="sk-..."))
    async for event in es:
        if event.type == "text_delta":
            print(event.delta, end="")
    print()
    msg = await es.result()
    print(f"usage: {msg.usage.input} in / {msg.usage.output} out")

asyncio.run(main())
```

### Agent + 工具调用

```python
import asyncio
from pi_ai import Model, TextContent, UserMessage
from pi_agent_core import Agent, AgentOptions, AgentToolResult

class WeatherTool:
    name = "get_weather"
    description = "查询天气"
    label = "Weather"
    parameters = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    async def execute(self, tool_call_id, params, cancel_event=None, on_update=None):
        return AgentToolResult(content=[TextContent(text=f"{params['city']} 晴天 25°C")])

async def main():
    agent = Agent(AgentOptions(
        initial_state={"system_prompt": "你是助手", "model": model, "tools": [WeatherTool()]},
        get_api_key=lambda p: "sk-...",
    ))
    agent.subscribe(lambda ev, sig: print(ev.type), )
    await agent.prompt("北京天气怎么样？")

asyncio.run(main())
```

### 编码 Agent

```python
import asyncio
from pi_ai import Model
from pi_coding_agent import CodingAgent

async def main():
    agent = CodingAgent(
        model=model,
        api_key="sk-...",
        cwd=".",  # 工作目录
    )
    await agent.prompt("读取 README.md 并总结")

asyncio.run(main())
```

## 包结构与依赖

```
packages/
├── pi-ai/              # 叶子 — 统一 LLM API
├── pi-agent-core/      # → pi-ai — agent 运行时
├── pi-storage-sqlite/  # → pi-ai + pi-agent-core — SQLite 后端
├── pi-coding-agent/    # → pi-agent-core + pi-ai — 编码 agent SDK
└── pi-server/          # → pi-coding-agent — RPC 服务
```

依赖方向自底向上，与上游一致。每个有意偏离上游的地方，记录在对应包的 [`PORTING.md`](./packages/pi-ai/PORTING.md) 中。

## 技术选型

| 领域 | 选型 | 对应上游 |
|---|---|---|
| 类型/校验 | Pydantic v2 | typebox |
| 异步 | asyncio + AsyncGenerator | Promise + ReadableStream |
| LLM provider | 各厂原生 Python SDK | 各厂原生 TS SDK |
| 存储 | stdlib `sqlite3` | node:sqlite |
| 包管理 | uv workspace | npm workspaces |

## 开发

```bash
uv sync                 # 安装全部依赖（含 dev）
uv run pytest           # 跑测试（默认跳过 integration）
uv run pytest -m integration  # 真实 LLM 调用测试（需 API key + 消耗额度）
uv run ruff check       # lint
uv run ruff format      # 格式化
uv run mypy             # 类型检查（strict）
```

集成测试需要设置环境变量（参考 [`.env`](./.env)）：
- `OPENAI_API_KEY` — OpenAI 测试
- `DEEPSEEK_API_KEY` — DeepSeek 测试（OpenAI 兼容协议）
- `ANTHROPIC_API_KEY` — Anthropic 测试

贡献指南详见 [`CONTRIBUTING.md`](./CONTRIBUTING.md)。

## 路线图

- [x] 5 包基线完成（对齐上游 v0.81.1）
- [x] OpenAI/DeepSeek provider 真实验证
- [x] Anthropic provider（纯逻辑测试，待真实 API 验证）
- [ ] Google / Mistral / Bedrock provider
- [ ] OAuth 鉴权（`auth/*`）
- [ ] 扩展系统（`extensions/`）

## 许可证

MIT，与上游保持一致。详见 [`LICENSE`](./LICENSE)。

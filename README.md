# pi-py

> [Pi](https://pi.dev) 的 Python SDK 复刻 —— AI agent 工具集：统一 LLM API、agent 运行时、编码 agent 工具集。

[Pi](https://github.com/earendil-works/pi)（原名 `badlogic/pi-mono`，作者 Mario Zechner 于 2026 年加入 [Earendil Works](https://github.com/earendil-works) 后项目迁至现址）是一套 TypeScript 的 AI agent 工具集。本仓库将其核心能力移植到 Python，**以 SDK 库形式提供，不含 CLI/TUI**。

## 同步状态

- **当前对齐版本**：[`v1.3.0`](./UPSTREAM_VERSION)（2026-10-15）
- **同步策略**：每 2 周集中同步一次；期间若扫描器命中「破坏性变更」则提前处理。
  触发源是上游各包的 `CHANGELOG.md`（不是版本号，也不是 Release Notes）。详见 [`SYNC.md`](./SYNC.md)。
- **版本号约定**：**包版本号 == 镜像的上游版本**（与上游保持一致）。因此所有包
  **锁步发版**，且升级版本号必须同时升级 [`UPSTREAM_VERSION`](./UPSTREAM_VERSION) ——
  这条约定由 `tests/test_version_lockstep.py` 在每次提交时校验。
  ⚠️ 版本号表达的是「本包镜像的是上游哪个版本」，**不是覆盖率**；各包的实际覆盖度
  见各自的 `PORTING.md` 与下表状态列。

| 包 | 上游对应 | 状态 | 说明 |
|---|---|---:|---|
| [`pi-ai`](./packages/pi-ai) | `@earendil-works/pi-ai` | ✅ | 统一 LLM API（OpenAI + Anthropic + 模型类型族 + retry/overflow/validation） |
| [`pi-agent-core`](./packages/pi-agent-core) | `@earendil-works/pi-agent-core` | ✅ | agent 循环引擎 + sessions（技能/会话/压缩） |
| [`pi-coding-agent`](./packages/pi-coding-agent) | `@earendil-works/pi-coding-agent` | ✅ | 编码 agent SDK + 内置扩展三件套（tool_search / mcp / codemode）；上游 `core/` 仍有欠账 |
| [`pi-durable`](./packages/pi-durable) | `@earendil-works/pi-durable` | ✅ | 可移植执行环境 + 会话/条目/任务运行时（`harness/` 全覆盖，含 `TaskScheduler`） |
| [`pi-codemode`](./packages/pi-codemode) | `@earendil-works/pi-codemode` | ✅ | JS 沙箱（wasmtime + 上游同一份 `quickjs.wasm`，prelude 原文装载） |
| [`pi-mcp`](./packages/pi-mcp) | `@earendil-works/pi-mcp` | ✅ | MCP 客户端（stdio 子进程 + Streamable HTTP/SSE + OAuth 全套） |
| [`pi-chord`](./packages/pi-chord) | `@earendil-works/chord`（消费侧） | 🔶 | 上下文/JSON diff 面；上游 **host 半边**（8,117 行）在 v1.3.0 无消费者，未移植 |
| [`pi-storage-sqlite`](./packages/pi-storage-sqlite) | _无_（上游已删） | 🗄️ | **已废弃**：转发壳，请改用 `pi_durable.storage.sqlite` |

> 状态含义：✅ 上游该包在本仓有对应实现面；🔶 有意只覆盖一部分（原因见备注与 `PORTING.md`）；
> 🗄️ 上游已删除、本仓仅保留迁移壳。

## 安装

所有包均已发布到 [PyPI](https://pypi.org/user/encyc/)（Python ≥ 3.11），可按需安装单个包，内部依赖会自动解析：

```bash
pip install pi-py-agent-core   # agent 运行时（含 pi-py-ai）
pip install pi-py-coding-agent # 编码 agent SDK
```

> 发行名带 `pi-py-` 前缀（`pi-agent-core` 等短名在 PyPI 已被他人占用），导入名不带前缀，如 `from pi_agent_core import Agent`。

| PyPI 包 | 用途 |
|---|---|
| [`pi-py-ai`](https://pypi.org/project/pi-py-ai/) | 统一 LLM API（OpenAI + Anthropic + retry） |
| [`pi-py-agent-core`](https://pypi.org/project/pi-py-agent-core/) | agent 循环引擎 + sessions |
| [`pi-py-coding-agent`](https://pypi.org/project/pi-py-coding-agent/) | 编码 agent SDK |
| [`pi-py-durable`](https://pypi.org/project/pi-py-durable/) | 执行环境 + 会话/任务运行时 |
| [`pi-py-codemode`](https://pypi.org/project/pi-py-codemode/) | JS 沙箱（wasmtime + quickjs.wasm） |
| [`pi-py-mcp`](https://pypi.org/project/pi-py-mcp/) | MCP 客户端 |
| [`pi-py-chord`](https://pypi.org/project/pi-py-chord/) | 上下文/JSON diff 基元 |
| [`pi-py-storage-sqlite`](https://pypi.org/project/pi-py-storage-sqlite/) | **已废弃**：转发到 `pi-py-durable` |
| `pi-py-server` | **已下线**：上游已于 v1.2.0 删除该包；PyPI 上保留历史版本，不再发新版本 |

发布流程：打 tag **并创建 GitHub Release** 后，[`publish.yml`](./.github/workflows/publish.yml) 自动构建并发布 `packages/` 下全部 workspace 成员到 PyPI。发布记录见 [Releases](https://github.com/encyc/pi-py/releases) 与 [CHANGELOG.md](./CHANGELOG.md)。

> ⚠️ 只推 tag 而不建 Release 会导致发布流程**静默不触发**——`v0.85.1` 就是这样漏发的。
> [`release-guard.yml`](./.github/workflows/release-guard.yml) 会检查这一点。

## 快速上手

也可以直接从源码运行：

```bash
git clone https://github.com/encyc/pi-py.git
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
├── pi-chord/           # 叶子 — 调用作用域上下文（上游 chord 的 context 子路径）
├── pi-agent-core/      # → pi-ai — agent 循环引擎
├── pi-durable/         # → pi-chord — 可移植执行环境（目前仅 env 岛）
├── pi-codemode/        # 叶子（仅依赖 wasmtime）— JS 沙箱（QuickJS/WASM）
├── pi-mcp/             # 叶子（仅依赖 httpx）— MCP 客户端（stdio / Streamable HTTP）
├── pi-storage-sqlite/  # 已废弃 — 转发壳，指向 pi-durable.storage.sqlite
└── pi-coding-agent/    # → pi-agent-core + pi-ai — 编码 agent SDK
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

- [x] 5 包基线完成（对齐上游 v0.85.1）
- [x] OpenAI/DeepSeek provider 真实验证
- [x] Anthropic provider（纯逻辑测试，待真实 API 验证）
- [x] **追赶上游 1.0 大改版**（当前对齐上游 **v1.3.0**）：`pi-server` 下线、
      `pi-storage-sqlite` 并入新建 `pi-durable`、新增 `pi-mcp`/`pi-codemode`、
      `agent` 瘦身、`coding-agent` Tier-A + 内置扩展三件套
- [ ] `pi-coding-agent` 的 `core/` 剩余部分（`agent-session`/`settings-manager`/
      `resource-loader`/`compaction` 等，见其 `PORTING.md` 欠账清单）
- [ ] Google / Mistral / Bedrock provider
- [ ] OAuth 鉴权（`auth/*`）

## 许可证

MIT，与上游保持一致。详见 [`LICENSE`](./LICENSE)。

# py-mono

> Python SDK 复刻版 [earendil-works/pi](https://github.com/earendil-works/pi) —— AI agent 工具集：统一 LLM API、agent 运行时、编码 agent 工具集。

本项目是 [Pi](https://pi.dev) 的 **Python 库** 复刻。Pi（原名 `badlogic/pi-mono`，作者 Mario Zechner 于 2026 年 4 月加入 [Earendil Works](https://github.com/earendil-works) 后项目迁至现址）是一套 TypeScript 的 AI agent 工具集。本仓库将其核心库能力移植到 Python，**只做 SDK 库，不做 CLI/TUI**。

## 同步状态

- **当前对齐版本**：[`v0.81.1`](./UPSTREAM_VERSION)（2026-07-21）
- **同步策略**：仅在上游发布 `0.x.0`（minor）时集中同步，详见 [`SYNC.md`](./SYNC.md)

| 包 | 上游对应 | 状态 | 说明 |
|---|---|---:|---|
| [`pi-ai`](./packages/pi-ai) | `@earendil-works/pi-ai` | 🟡 待复刻 | 统一 LLM API（多 provider 适配 + 鉴权 + 类型） |
| [`pi-agent-core`](./packages/pi-agent-core) | `@earendil-works/pi-agent-core` | 🟡 待复刻 | 有状态/无状态 agent 循环 + harness（技能/会话/压缩） |
| [`pi-storage-sqlite`](./packages/pi-storage-sqlite) | `@earendil-works/pi-storage-sqlite-node` | 🟡 待复刻 | SQLite 会话存储后端 |
| [`pi-coding-agent`](./packages/pi-coding-agent) | `@earendil-works/pi-coding-agent` | 🟡 待复刻 | 编码 agent SDK（bash/read/edit/write/grep 工具 + 会话） |
| [`pi-server`](./packages/pi-server) | `@earendil-works/pi-server` | 🟡 待复刻 | agent 服务化（RPC/IPC，可选） |

🟡 待复刻 / 🟢 可用 / 🔴 不复刻（有意偏离）

## 包结构与依赖

```
packages/
├── pi-ai/              # 叶子，无内部依赖 — 统一 LLM API
├── pi-agent-core/      # → pi-ai — agent 运行时
├── pi-storage-sqlite/  # → pi-ai + pi-agent-core — 可插拔存储后端
├── pi-coding-agent/    # → pi-agent-core + pi-ai — 编码 agent SDK（不含 TUI）
└── pi-server/          # → pi-coding-agent — RPC 服务（可选）
```

依赖方向自底向上，与上游一致。

## 技术选型

| 领域 | 选型 | 对应上游 |
|---|---|---|
| 类型/校验 | Pydantic v2 | typebox |
| 异步 | asyncio + AsyncGenerator | Promise + ReadableStream |
| LLM provider | 各厂原生 Python SDK | 各厂原生 TS SDK |
| 存储 | stdlib `sqlite3` | node:sqlite |
| 包管理 | uv workspace | npm workspaces |

每个有意偏离上游的地方，记录在对应包的 [`PORTING.md`](./packages/pi-ai/PORTING.md) 中。

## 开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                 # 安装全部依赖（含 dev）
uv run pytest           # 跑测试
uv run ruff check       # lint
uv run mypy packages    # 类型检查
```

## 许可证

MIT，与上游保持一致。详见 [`LICENSE`](./LICENSE)。

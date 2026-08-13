# pi-coding-agent 移植注记

对应上游：[`@earendil-works/pi-coding-agent`](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)（v0.84.1）

## 有意偏离上游（重要：本包大幅裁剪）

本包定位为 **SDK 库**，只复刻上游的纯逻辑部分，**不复刻 CLI 与 TUI**。

| 上游部分 | LOC（估） | 是否复刻 | 原因 |
|---|---:|---:|---|
| `src/core/`（AgentSession、tools、extensions、session-manager 等） | ~27k | ✅ | 纯业务逻辑，Python 生态价值高 |
| `src/modes/rpc/` | ~1.6k | ✅ | server 包依赖，程序化调用 |
| `src/extensions/`（内置扩展如 llama） | ~3k | ✅ | 扩展逻辑 |
| `src/utils/` | ~3k | ✅ | 工具函数 |
| **`src/modes/interactive/`（Ink TUI）** | **~16.5k** | **❌** | **TS Ink 渲染代码（interactive-mode.ts 单文件 6032 行 + 40 个 selector 组件），Python 端用 textual 等另行实现更有意义，硬翻译无价值** |
| **`src/cli.ts` / `src/main.ts`（CLI 入口）** | ~31k | **❌** | **本仓库不做 CLI；交互式体验由用户基于 SDK 自行构建** |
| 对 `pi-tui` 的依赖 | — | ❌ | 随 TUI 一并移除 |

> **取舍说明**：本包因此只覆盖上游约 35/54k LOC 的逻辑部分。`pi` 命令行工具不在本仓库目标内。如需 Python 版交互式 agent，建议基于本 SDK + [textual](https://textual.textualize.io/) 单独构建。

## cherry-pick

（暂无）

## v0.84.1 同步说明（破例同步 patch）

- 上游本轮 ``core/tools`` 改动（Windows 路径处理、``*SystemPromptContribution`` 结构、model-runtime/remote-catalog）不映射到本端精简 ``tools.py``；CLI/TUI/modes/extensions/dynamic-catalog 维持裁剪。
- 本包仅同步版本、上游引用与内部依赖约束（``pi-ai``/``pi-agent-core`` → ``>=0.84.1,<0.85``）。

## v0.83.0 同步说明

- 上游 v0.83.0 的凭证导出、OAuth、扩展上下文和交互模式修复均位于本包
  明确裁剪的 CLI/TUI/扩展范围。
- 本包仅同步版本、上游引用和内部依赖约束。

## 待办

- [ ] core/agent-session（AgentSession、createAgentSession SDK）
- [ ] core/tools（bash / read / edit / write / grep / find / ls）
- [ ] core/extensions（Extension/ExtensionRunner/ExtensionAPI）
- [ ] core/session-manager、settings-manager、resource-loader
- [ ] core/compaction
- [ ] modes/rpc/（server 依赖）

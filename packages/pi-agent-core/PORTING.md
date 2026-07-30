# pi-agent-core 移植注记

对应上游：[`@earendil-works/pi-agent-core`](https://github.com/earendil-works/pi/tree/main/packages/agent)（v0.83.0）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| typebox 类型 | Pydantic v2 | 详见 pi-ai/PORTING.md |
| Promise / ReadableStream | asyncio + AsyncGenerator | 同上 |
| 标准日志 | stdlib `logging` | 不引入私有日志包 |
| storage 直接耦合 | 通过抽象接口，storage 为可选后端 | 保持 agent-core 与存储解耦 |

## cherry-pick

（暂无）

## v0.83.0 同步说明

- 上游 agent 包在本轮没有落入当前精简 Python runtime 的行为变更。
- 本包仅同步版本、上游引用和对 `pi-ai>=0.83.0,<0.84` 的依赖约束。

## v0.82.1 同步说明

- Compaction/summary 请求使用独立 routing session，并强制
  `cache_retention="none"`，避免污染主会话缓存。
- Agent tools 会将 `constrained_sampling` 透传到 pi-ai。
- 上游新增的 Harness execution tools 与本仓库 `pi-coding-agent` 工具集职责重叠；
  当前精简 Harness 尚未公开 `ExecutionEnv`/`toolContext`，因此未引入不完整兼容层。

## 待办

- [ ] agent-loop.ts（无状态循环引擎）
- [ ] agent.ts（有状态 Agent 封装）
- [ ] harness/（skills / session / compaction / system-prompt）
- [ ] proxy（修复旧版导入位置错误）

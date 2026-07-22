# pi-agent-core 移植注记

对应上游：[`@earendil-works/pi-agent-core`](https://github.com/earendil-works/pi/tree/main/packages/agent)（v0.81.1）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| typebox 类型 | Pydantic v2 | 详见 pi-ai/PORTING.md |
| Promise / ReadableStream | asyncio + AsyncGenerator | 同上 |
| 标准日志 | stdlib `logging` | 不引入私有日志包 |
| storage 直接耦合 | 通过抽象接口，storage 为可选后端 | 保持 agent-core 与存储解耦 |

## cherry-pick

（暂无）

## 待办

- [ ] agent-loop.ts（无状态循环引擎）
- [ ] agent.ts（有状态 Agent 封装）
- [ ] harness/（skills / session / compaction / system-prompt）
- [ ] proxy（修复旧版导入位置错误）

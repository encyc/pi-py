# pi-ai 移植注记

对应上游：[`@earendil-works/pi-ai`](https://github.com/earendil-works/pi/tree/main/packages/ai)（v0.81.1）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| typebox 类型 | Pydantic v2 BaseModel | Python 生态标准；既做运行时校验又做序列化 |
| Promise / ReadableStream | asyncio + AsyncGenerator | 对应 Python 异步模型 |
| 各厂 TS SDK | 各厂 Python SDK | 语言对等 |
| providers/*.models.ts（静态模型清单） | 待定（配置/JSON 数据） | 大量是数据，复刻阶段评估压缩 |

## cherry-pick

（暂无 —— 基线建立时尚未 cherry-pick 任何 patch）

## 待办

- [ ] types.ts → Pydantic 类型系统
- [ ] 流式 API（PiMessages* 事件）
- [ ] OpenAI provider 适配
- [ ] Anthropic provider 适配
- [ ] Google / Mistral / Bedrock provider
- [ ] auth（OAuth）

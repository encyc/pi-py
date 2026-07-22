# pi-ai 移植注记

对应上游：[`@earendil-works/pi-ai`](https://github.com/earendil-works/pi/tree/main/packages/ai)（v0.81.1）

## 进度

| 模块 | 状态 | 上游文件 |
|---|---|---|
| types（消息/内容块/Usage/Model/Tool/Context） | ✅ | `types.ts` |
| events（12 变体 AssistantMessageEvent） | ✅ | `types.ts` |
| event_stream（EventStream） | ✅ | `utils/event-stream.ts` |
| exceptions（异常体系） | ✅ | 散落各处 |
| models（模型注册表 + api provider 分发） | ✅ | `models.ts` + `api-registry.ts` |
| stream/stream_simple/complete 入口 | ✅ | `stream.ts` |
| providers/faux（测试 mock） | ✅ | `providers/faux.ts` |
| providers/openai（Chat Completions） | ✅ | `api/openai-completions.ts` |
| providers/anthropic | 🟡 下一轮 | `api/anthropic-messages.ts` |
| providers/google / mistral / bedrock | 🟡 后续 | `api/*.ts` |
| auth（OAuth） | 🟡 后续 | `auth/*` |
| images（图像生成） | 🟡 后续 | `images*.ts` |

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| typebox 类型 | Pydantic v2 BaseModel | Python 生态标准；既做运行时校验又做序列化 |
| Promise / ReadableStream | asyncio + AsyncGenerator | 对应 Python 异步模型 |
| 各厂 TS SDK | 各厂 Python SDK（openai / anthropic） | 语言对等 |
| `parseStreamingJson`（partial-json 库） | `json-repair` 库 | Python 生态等价容错 JSON 解析 |
| EventStream（手写队列） | EventStream（asyncio.Queue） | 移植自旧版优秀设计 |
| 模型清单 `models.generated.ts` | 精简硬编码（gpt-4o 系列） | 后续按需扩充或数据化 |

## 技术备忘

- **流式累加用模型实例**：provider 实现中 `output.content` 始终持有真实 Pydantic 模型
  实例（TextContent/ThinkingContent/ToolCall），原地修改属性累加。Pydantic v2 不在
  修改时重新校验（仅构造时），故 final message 的 content 是合法模型对象。
- **工具调用双索引**：按 `delta.index` 和 `delta.id` 索引，参数 JSON 字符串拼接进
  `partial_args`，每个增量重新解析（容忍不完整 JSON）。
- **错误编码为事件**：provider 内部不抛异常，失败 → `stopReason="error"` + error 事件。
  `max_retries=0`，重试是外层关注点（待实现 retry 工具）。
- **pydantic-mypy 插件**：对 `Annotated[Union, Field(discriminator=...)]` 判别联合的
  字段类型解析有偏差，AssistantMessage.content 迭代处用 `__dict__` 直取绕过。

## cherry-pick

（暂无）

## 待办（下一轮）

- [ ] Anthropic provider（含 thinking 支持）
- [ ] retry 工具（``retry_assistant_call`` + 可重试错误正则）
- [ ] 更多 provider（google / mistral / bedrock）

# pi-ai 移植注记

对应上游：[`@earendil-works/pi-ai`](https://github.com/earendil-works/pi/tree/main/packages/ai)（v0.84.1）

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
| providers/anthropic（含 thinking 支持） | ✅ | `api/anthropic-messages.ts` |
| retry（重试工具） | ✅ | `utils/retry.ts` |
| constrained sampling（strict JSON Schema + OpenAI grammar） | ✅ | `api/constrained-sampling.ts` |
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

## v0.84.1 同步说明（破例同步 patch）

> 按现行策略只追上游 minor；本轮由用户点名 0.84.1，属破例跨 minor（0.83→0.84）并叠加 patch 的同步。

- OpenAI-compatible 适配器支持声明 ``compat.supportsFinishReason=False`` 的兼容端点：流结束未见 ``finish_reason`` 时按内容推断 ``stop``/``toolUse``，不再误报错误。
- 新增泛型采样参数：``StreamOptions.sampling_params`` 与 ``Model.sampling_params``，由 OpenAI-compatible 适配器在具名字段之后合并（请求级按 key 覆盖模型级，且覆盖具名字段）。
- 新增 ``thinking_token_budget``（vLLM 等）：当 ``compat.supportsThinkingTokenBudget=True`` 且有 reasoning 时注入顶层预算，保证为答案留出 token 空间。
- Anthropic 适配器保留 ``content_block_start`` 携带的初始 text/thinking/signature。
- ``KnownProvider`` 补 ``baseten``、``qwen-token-plan-individual``（仅类型对齐，未注册实现）。
- **未移植**（裁剪范围）：deferred/background 响应模式（``StopReason "deferred"`` / ``DeferredHandle``，属 OpenAI Responses）、telemetry context、Baseten provider 实现、``baseten`` thinkingFormat / ``chatTemplateArgs``、error-body 的 plain-object 修补（本端错误处理不涉及该 bug）、动态模型目录与 ``models.generated``。

## v0.83.0 同步说明

- `AssistantMessage.stopReason` 增加 `pending` 中间状态，并保留 provider
  原始终止原因为 `rawStopReason`。
- OpenAI/Anthropic 流缺失或返回未知终止原因时以 provider error 结束，
  不再误判为成功。
- 上游按请求注入 `fetch` 的能力映射为 Python SDK 原生的
  `StreamOptions.http_client`（序列化别名 `httpClient`）。
- 修复 OpenAI-compatible delta 同时含有效 `function` 与空 `custom`
  时生成重复工具参数增量的问题。
- TypeBox、OAuth、图片 API、新 provider 和动态模型目录仍属于既有裁剪范围。

## v0.82.1 同步说明

- `Tool.constrainedSampling` 已映射为 Pydantic 的 `constrained_sampling`
  （序列化别名保持 camelCase）。
- OpenAI Chat Completions 支持 strict JSON Schema 与 Lark/regex grammar 工具定义；
  Anthropic Messages 支持 strict tool schema。
- OpenAI/Anthropic SDK 内建重试保持关闭，由可取消的 provider retry 包装器处理。
- DNS `getaddrinfo` / `ENOTFOUND` / `EAI_AGAIN` 错误纳入 assistant 自动重试分类。
- OAuth、新 provider 与完整动态模型目录仍属于既有裁剪范围，未在本轮扩展。

## 待办（下一轮）

- [x] Anthropic provider（含 thinking 支持）
- [x] retry 工具（``retry_assistant_call`` + 可重试错误正则）
- [ ] 更多 provider（google / mistral / bedrock）
- [ ] OAuth 鉴权（``auth/*``）

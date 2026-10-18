# pi-ai 移植注记

对应上游：[`@earendil-works/pi-ai`](https://github.com/earendil-works/pi/tree/main/packages/ai)（**v1.3.0**，行为级对齐；见「v1.3.0 同步说明」）

## 进度

| 模块 | 状态 | 上游文件 |
|---|---|---|
| types（消息/内容块/Usage/Model/Tool/Context/SystemMessage/TranscriptContext） | ✅ v1.3.0 | `types.ts` |
| events（12 变体 AssistantMessageEvent） | ✅ | `types.ts` |
| event_stream（EventStream + AssistantMessageEventStream 计时） | ✅ v1.3.0 | `utils/event-stream.ts` |
| exceptions（异常体系） | ✅ | 散落各处 |
| models（模型注册表 + api provider 分发） | ✅ | `models.ts` + `api-registry.ts` |
| stream/stream_simple/complete 入口（含 `normalizeContext` 归一化） | ✅ v1.3.0 | `stream.ts` + `utils/transcript.ts` |
| **transcript（归一化 / system message 回放 / 工具状态增量）** | ✅ v1.3.0 | `utils/transcript.ts` |
| **text（contentText / getSystemMessageText / renderSystemMessageUpdate）** | ✅ v1.3.0 | `utils/text.ts` |
| **json_parse（parseStreamingJson / repairJson / parseToolCallArguments）** | ✅ v1.3.0 | `utils/json-parse.ts` |
| **estimate（上下文 token 估算，3.5 字符/token）** | ✅ v1.3.0 | `utils/estimate.ts` |
| **headers（providerHeadersToRecord 变参 + 小写折叠）** | ✅ v1.3.0 | `utils/headers.ts` |
| **transform_messages（跨模型思考丢弃）** | 🟡 部分 v1.3.0 | `api/transform-messages.ts` |
| providers/faux（测试 mock） | ✅ v1.3.0 | `providers/faux.ts` |
| providers/openai（Chat Completions） | ✅ v1.3.0 | `api/openai-completions.ts` |
| providers/anthropic（含 thinking 支持） | 🟡 v1.3.0 部分（见同步说明） | `api/anthropic-messages.ts` |
| retry（重试工具 + 60s 延迟封顶） | ✅ v1.3.0 | `utils/retry.ts` |
| constrained sampling（strict JSON Schema + OpenAI grammar） | ✅ v1.3.0 | `api/constrained-sampling.ts` |
| providers/google / mistral / bedrock | 🟡 后续 | `api/*.ts` |
| auth（OAuth 交互登录） | 🟡 后续（**不含** `utils/oauth-page`，见下） | `auth/*` |
| **oauth-page（回调页面 HTML 生成，纯函数）** | ✅ v1.3.0 | `utils/oauth-page.ts` |
| **uuid（`uuidv7()` 时间有序 UUID 生成）** | ✅ v1.3.0 | `utils/uuid.ts` |
| **overflow（`isContextOverflow`/`isRecoverableLength`/`getOverflowPatterns`）** | ✅ v1.3.0 | `utils/overflow.ts` |
| **validation（`validateToolCall`/`validateToolArguments`）** | 🟡 v1.3.0（**形状有差异**，见下） | `utils/validation.ts` |
| **`Models` 门面类型面（Protocol）** | 🟡 v1.3.0（**仅类型，实现未移植**；覆盖 24 之 9） | `models.ts:255-365` |
| `Models` 门面**实现**（`createModels` / `Provider` 运行时 / 目录刷新 / 鉴权） | ⬜ 未移植（**报告 01 Q2 架构决策，需拍板**） | `models.ts` + `auth/*` |
| images（图像生成） | 🟡 后续 | `images*.ts` |
| **模型类型族（类型形状：`ImageModel`/`ClassifierModel`/`AnyModel` 等）** | ✅ v1.3.0（**仅类型**） | `types.ts:825-898` |
| **模型类型判别（`getModelType`/`isModelType`）** | ✅ v1.3.0（含行为） | `utils/model-operations.ts` |
| **惰性流（`lazyStream`）** | ✅ v1.3.0（含行为） | `api/lazy.ts:41-62` |
| classifier 能力（`classify()` + 四个后端实现） | ⬜ 未开始（§4 越界项） | `api/*system-one*.ts` 等 |
| images 能力（`generateImages()` + provider 接线） | ⬜ 未开始（§4 越界项） | `api/openrouter-images.ts` 等 |
| `lazyApi`（包装动态 import 的 API 模块） | ⬜ 未开始 | `api/lazy.ts:74-98` |

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| typebox 类型 | Pydantic v2 BaseModel | Python 生态标准；既做运行时校验又做序列化 |
| Promise / ReadableStream | asyncio + AsyncGenerator | 对应 Python 异步模型 |
| 各厂 TS SDK | 各厂 Python SDK（openai / anthropic） | 语言对等 |
| `parseStreamingJson`（partial-json 库） | 严格解析 → **窄转义修复** → `json-repair` 结构补全（仅流式路径） | Python 生态等价容错 JSON 解析；**收尾路径已改为上游窄修复语义**，见偏离 ② |
| EventStream（手写队列） | EventStream（asyncio.Queue） | 移植自旧版优秀设计；`asyncio.Queue` 天然无上游 #9055 的二次复杂度 |
| 模型清单 `models.generated.ts` | 精简硬编码（gpt-4o 系列） | 后续按需扩充或数据化 |
| `JsonValue` 递归类型 | `JsonValue: TypeAlias = Any`；`JsonObject = dict[str, Any]` | Pydantic v2 对**隐式递归别名**会 `RecursionError`（曾导致全包无法 import，见「v1.3.0 类型族批次」）。`ToolCall.arguments` 亦沿用同一退化，保持一致 |
| `BaseModel`（模型字段基类） | **不导出**，仅内部私有 `_ModelShapeBase` | 会与 `pydantic.BaseModel` 冲突；下游未请求该符号 |
| `@earendil-works/pi-ai/utils/oauth-page` 子路径 | 规范位置 `pi_ai/oauth_page.py`；另提供 `pi_ai/utils/oauth_page.py` 薄转发 | Python 无子路径导出机制；转发使上游路径写法与顶层写法都能用且 `is` 同一（见「oauth-page 说明」） |
| `SessionAffinityFormat` / session-affinity 头 | 声明了字段但 provider 未发送 | 本端未实现会话亲和性头（依赖未移植的动态目录/缓存路由） |
| `onPayload` / `onResponse` 钩子 | 未实现（仅 `onProviderStreamEvent`） | v0.85.1 起即未移植，属既有缺口；本轮只补 M-22 的 `onProviderStreamEvent` |
| 图像降级（`downgradeUnsupportedImages`） | 未实现 | 非 vision 模型收到图像时报错而非替换占位文本，属既有缺口 |
| 孤立工具调用的合成结果（`insertSyntheticToolResults`） | 未实现 | agent-core 侧自行保证工具结果配对 |

### v1.3.0 引入的**默认行为变更**（务必知悉）

| # | 变更 | 上游依据 | 影响 |
|---|---|---|---|
| ① | **重试退避默认被 60s 封顶** | `utils/retry.ts:126-137`（`maxAgentDelayMs?` + `DEFAULT_MAX_AGENT_RETRY_DELAY_MS = 60_000`） | 即便调用方不设 `RetryPolicy.max_agent_delay_ms`，延迟也不再是无上限指数增长。自 v0.85.1 的 `base_delay_ms * 2**(attempt-1)` 改为 `min(该值, 60_000)`。上游 CHANGELOG 0.86.0 Added（issue #8826） |
| ② | **工具参数收尾不再保留部分解析前缀，也不再结构补全** | `utils/json-parse.ts:106-124`（`parseToolCallArguments`） | 模型给出的完整参数若不是合法 JSON 对象 → `arguments = {}` + `arguments_error`，由工具参数校验把原文报回模型。这是**行为收紧**：以前会拿"解析成功的前缀字段"静默执行（issue #10897） |
| ③ | **未知 OpenAI-compatible 端点默认不发 `strict`** | `api/openai-completions.ts:1677`（`supportsStrictMode: false`） | v0.85.1 的启发式（`!isMoonshot && !isTogether && ...`）改为默认关闭，由生成目录显式开启。未声明 `compat.supportsStrictMode` 的端点上，`strict` 键**整个不发**（上游 `:1515`），schema 也不做 strict 化（issue #9816） |
| ④ | **上下文估算从 4 改为 3.5 字符/token** | `utils/estimate.ts:15`（`CHARS_PER_TOKEN = 3.5`） | **高估**输入 token → 输出上限更保守 → 减少 "context length exceeded" 类失败。上游 CHANGELOG 1.1.0 Fixed（issue #10497） |
| ⑤ | **跨模型思考直接丢弃** | `api/transform-messages.ts:127-146` | v0.85.1 把跨模型思考降级为普通文本；v1.3.0 反转为丢弃。本端 v0.85.1 移植时两者都没做（"有签名就回放"），本轮改为与 v1.3.0 一致（issue #10995；同类 400 见 #10324） |
| ⑥ | **同一 delta 内 reasoning 必须先于 content 建块** | `api/openai-completions.ts:592`、`:641` | LiteLLM 等代理把 reasoning 与答案聚合进一个 delta；旧顺序会让思考显示在答案之后（issue #10926） |
| ⑦ | **`error` / `aborted` 终态在 faux 走 `ErrorEvent`** | `providers/faux.ts:444-449` | `DoneEvent.reason` 只接受 `stop|length|toolUse`；此前 faux 一律发 `DoneEvent`，`stop_reason="aborted"` 会在 push 时抛 `ValidationError`，异常留在无人 await 的 task 内，**流永不结束、消费者挂死** |

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

## v1.3.0 同步说明

本轮依据 `recon/01-upstream-ai-delta.md`（侦察报告，含 22 条破坏性变更 M-1…M-22）
做**行为级对齐**：只做"不跟就会行为不一致"的部分，新能力（classifier / images /
OAuth / 动态目录）不在本轮范围。

上游包级版本区间为 `0.85.1 → 1.3.0`；`src/` 178 → 199 文件
（+29 added / −8 removed / 102 modified / 68 unchanged）。

### 已完成（15 项）

| 项 | 内容 | 上游依据 | 主要落点 |
|---|---|---|---|
| **M-2** | `SystemMessage` + `ToolReference`，`Message` 联合新增 `role="system"` | `types.ts:546-562`、`:644`、`:761` | `types.py`、`transcript.py` |
| **M-3** | `ToolCall.argumentsError`（+ `arguments` 收紧为 JSON 对象语义） | `types.ts:440-448` | `types.py` |
| **M-4** | 工具参数**收尾**不保留部分解析前缀：非法 → `{}` + `arguments_error` | `utils/json-parse.ts:106-124`；`openai-completions.ts:464`；`anthropic-messages.ts:839` | `json_parse.py`、两个 provider |
| **M-5** | 移植上游 `repairJson`（**窄转义修复**），`\u` 不再丢字段 | `utils/json-parse.ts:4`、`:34-83` | `json_parse.py` |
| **M-6** | 跨模型思考**丢弃** + 跨模型清除 `thoughtSignature` | `api/transform-messages.ts:122`、`:127-155` | `transform_messages.py`、两个 provider |
| **M-1** | `TranscriptContext` + `normalizeContext` + 入口归一化 + provider 改读取点 | `types.ts:771-790`、`utils/transcript.ts`；`compat.ts:254-263` | `transcript.py`、`stream.py`、`models.py`、`faux.py`、两个 provider |
| **M-7** | `providerHeadersToRecord` 改**变参** + 小写折叠 + `None` 删除 | `utils/headers.ts:11-24` | `headers.py` |
| **M-8** | 重试分类扩容（`server_busy`/`520`/`unable to forward upstream` 等）+ 改写两条模式 + **60s 默认封顶** | `utils/retry.ts:21`、`:30-33`、`:44-48`、`:126-137` | `retry.py` |
| **M-9** | `retry-after`/`retry-after-ms` 加 `isfinite` 守卫 + `noRetryStatuses` | `utils/provider-retry.ts:8`、`:55-65` | `provider_retry.py` |
| **M-10** | `CHARS_PER_TOKEN` 4 → 3.5；估算接受 transcript 且计入 system message | `utils/estimate.ts:15`、`:49-55`、`:97` | `estimate.py` |
| **M-11** | `AssistantMessageEventStream` 写 `duration_ms`；`AssistantMessage`/`ToolResultMessage` 加字段 | `utils/event-stream.ts:97-131`；`types.ts:600`、`:640` | `event_stream.py`、`types.py` |
| **M-13** | openai-completions：reasoning 先于 content、strict 默认 `false`、采样参数三层合并、空 text 块过滤 | `openai-completions.ts:592`/`:641`、`:1677`、`:1006`、`:1271` | `openai_provider.py` |
| **M-17** | `KnownProvider` 加 `azure`/`typesafe`/`meta`（`azure-openai-responses` 作为 api id 保留） | `types.ts:94`、`:97`、`:120` | `types.py` |
| **M-21** | `Model.samplingParamsByThinkingLevel`；`BaseModel` 拆分仅按需最小化 | `types.ts:825-898` | `types.py` |
| **M-22** | `StreamOptions.onProviderStreamEvent`（两个 provider 均调用，支持同步/异步钩子） | `types.ts:223`；`openai-completions.ts:559`；`anthropic-messages.ts:700` | `types.py`、两个 provider、`async_utils.py` |

**额外修复（不在 M 列表，但同源且会挂死测试）**

- `EventStream.__init__` 原先调用 `asyncio.get_event_loop()`，在 Python 3.12+ 无运行
  循环时抛 `RuntimeError`。改为**惰性创建**结果 Future，使
  `create_assistant_message_event_stream()` 可在任意上下文调用（M-11 的前提）。
- faux：`error`/`aborted` 终态改发 `ErrorEvent`（上游 `faux.ts:444-449`），并给驱动
  任务加 `except BaseException` 兜底，使任何异常都会终结流而非让消费者挂死。
- `provider_retry`：修正 `retry-after-ms` 的单位（该头**已是毫秒**，此前误乘 1000）。

### 未完成（附原因）

| 项 | 内容 | 未完成原因 |
|---|---|---|
| **M-12** | deferred tools 机制替换（`deferredToolsMode` / `supportsToolReferences` → transcript 锚定的 `tools_added`） | 依赖 mid-conversation system message **特性**本身。本轮只移植了 transcript 骨架（归一化/回放/折叠），未启用"中途变更"。另：本端 `added_tool_names` 字段从未被任何 provider 读取（纯类型对齐），故无运行时行为需迁移 |
| **M-14** | Anthropic strict 工具 400 降级重试（L 级） | **架构冲突待用户拍板**：上游把"降级 + 重试一次"放在 provider 内（`anthropic-messages.ts:648-690`），与本端"provider 不抛异常、重试是外层关注点"（见「技术备忘」）的原则相反。需先决定是开例外还是改为事件抛出 |
| **M-15 / M-16** | `model-catalog.ts` 导出重命名、38 个 `*.models.ts` 机械改造 | 本端未移植生成目录（精简硬编码），无对应代码 |
| **M-18 / M-20** | `models-store` 存任意模型类型；`Images*` → `Image*` 重命名族 | 本端未移植 models-store 与 images |
| **M-19（部分）** | `LoginOptions`、`refreshStoredOAuthCredential`、`CompatSchemasByApi` 等 | 属未移植的 OAuth / 生成目录校验面 |
| classifier | `ClassifierModel`/`classify()` + 4 个后端实现 | 越界判断（报告 §4）：新能力，非"不跟就出错" |
| images 统一面 | `ImageModel`、`Provider.generateImages`、`Models.generateImages()` | 越界判断：新能力 |
| Anthropic federation | workload identity 鉴权 | 越界判断（依赖 TS SDK 能力） |
| 动态目录 | `personalizeOpenRouterCatalog`、Radius 目录覆盖 | 越界判断（目录刷新非调用协议） |

### 未移植的既有缺口（本轮发现，非 v1.3.0 引入）

- `onPayload` / `onResponse` 钩子从未被 provider 调用（仅有字段与文档）。
- 图像降级：非 vision 模型收到图像时报错，而非替换为占位文本（上游
  `downgradeUnsupportedImages`）。
- 孤立工具调用的合成结果（上游 `insertSyntheticToolResults`）。
- `StopReason` 缺 `"deferred"`（上游 `types.ts:474` 自 v0.85.1 起即含），连带
  `DeferredHandle` / `fetchDeferred` / `cancelDeferred` 未移植。
- `compat.maxTokensField`：上游按该字段决定发 `max_completion_tokens` 还是
  `max_tokens`（`openai-completions.ts:843-849`），本端恒发 `max_tokens`。
- session-affinity 头（`sendSessionAffinityHeaders` / `sessionAffinityFormat`）
  在 provider 侧未发送。

## v1.3.0 类型族批次（模型类型公开符号）

解锁 `coding-agent` 的 `core/model-registry.ts` / `model-runtime.ts` 所需的模型类型族
符号。依据上游 `types.ts:825-898`、`utils/model-operations.ts`、`api/lazy.ts`。

### 纯类型 vs 带行为

| 符号 | 性质 | 落点 | 上游 |
|---|---|---|---|
| `ImageModel` | **纯类型** | `types.py` | `types.ts:875-879` |
| `ClassifierModel` | **纯类型** | `types.py` | `types.ts:882-885` |
| `ClassifierContext` | **纯类型** | `types.py` | `types.ts:686-698` |
| `ClassifierResult` | **纯类型** | `types.py` | `types.ts:722-732` |
| `AssistantImages` | **纯类型** | `types.py` | `types.ts:657-664` |
| `ImagesContext` | **纯类型** | `types.py` | `types.ts:649-651` |
| `ModelType` | **纯类型**（Literal） | `types.py` | `types.ts:895` |
| `AnyModel` | **纯类型**（联合） | `types.py` | `types.ts:898` |
| `getModelType` | **带行为**（1 行判别） | `model_operations.py` | `utils/model-operations.ts:18` |
| `isModelType` | **带行为**（重载 TypeGuard） | `model_operations.py` | `utils/model-operations.ts:23` |
| `lazyStream` | **带行为**（流生命周期） | `lazy.py` | `api/lazy.ts:41-62` |

**支持类型**（为声明上述符号所必需，一并导出）：`ModelTypeMap`、`KnownImageApi`/
`ImageApi`、`KnownClassifierApi`/`ClassifierApi`、`ClassifierQuestion` 三态 +
`ClassifierAnswer` 三态、`ClassifierStopReason`、`ImagesInputContent`/
`ImagesOutputContent`/`ImagesStopReason`、`JsonValue`/`JsonObject`。

### 明确**未做**（不留桩）

- **`classify()` 与 `generateImages()` 的能力实现、provider 接线、四个分类后端**
  ——`§4` 登记的 NEW-CAPABILITY，需用户拍板。本批只给形状。
- `ProviderClassifier` / `ProviderImages` / `ClassifierOptions` / `ImagesOptions`
  ——能力面的 Protocol 与选项，未请求故未做。
- `assertChatModel` / `assertImageModel` / `assertClassifierModel` /
  `assertClassifierInputSupported` / `imageErrorResult` / `classifierErrorResult`
  ——同属 `utils/model-operations.ts`，各约 3 行；**未在符号清单内故未做**，
  需要时可立即补（下游 `model-runtime.ts` 的 lazyStream setup 里调用了 `assertChatModel`）。
- `lazyApi` / `LazyApiCapabilities`（`api/lazy.ts:69-98`）——本端 provider 及时导入，
  无动态模块加载需求。

### 给下游的两条 Python 约束

1. **`ModelTypeMap[TType]` 不可用**。上游 `model-registry.ts:81` 等处的
   `ModelTypeMap[TType]` 依赖 TS 泛型下标；Python 的 `TypedDict` 只接受**字面量键**
   （`ModelTypeMap["chat"]` 可用，与上游写法一一对应）。泛型方法请改用 `@overload`
   或 `is_model_type` 收窄。
2. **`isModelType` 是重载 TypeGuard，不是泛型守卫**。上游
   `model is ModelTypeMap[TType]` 无法在 Python 表达，故按字面量重载三条签名；
   `if is_model_type(m, "image"):` 之后 mypy 能收窄为 `ImageModel`；用变量传
   `type` 时收窄不可用（Python 类型系统固有限制）。

### 事故记录：隐式递归类型别名导致全包无法 import

- **时间**：2026-10-18 19:45–19:52（本批次开发过程中）
- **现象**：`import pi_ai` 直接 `RecursionError: maximum recursion depth exceeded`，
  报错栈指向 `class ClassifierContext(BaseModel)`；连带 `pi_agent_core` /
  `pi_coding_agent` 及三个包的全部测试无法运行
- **根因**：不是 `ClassifierContext` 本身，而是它引用的 `JsonObject` 当时被写成
  **隐式递归类型别名**：
  `JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]`。
  Pydantic v2 在**类创建期**解析该别名会无限递归
  （Pydantic 文档明确建议改用 PEP 695 具名别名）
- **修复**：**改形态**，即 `JsonValue: TypeAlias = Any` + `JsonObject = dict[str, Any]`。
  **这不是"修好了递归表达"**——递归写法至今仍会 `RecursionError`（有回归测试固化）。
  忠实表达有两条路径，本端评估后未采用：
  1. PEP 695 `type JsonValue = ...`：需 Python 3.12+，本包声明 `>=3.11`
  2. `typing_extensions.TypeAliasType`：**实测 Pydantic 可用**，但会让本包新增一个
     未声明的直接依赖（目前仅由 pydantic 传递引入 `typing-extensions>=4.14.1`）
- **回归守卫**：`tests/test_model_type_family.py` 在**子进程**中 import `pi_ai`
  （同进程内无法区分"没坏"与"已导入过"），并断言 `JsonValue is Any`，
  防止有人"顺手改回递归"再次炸掉全包
- **教训**：在共享包内做类型别名迭代时，必须**每步立即 import 自检**；
  本次是编辑后同一步内自检发现并修掉的，但对下游造成约 7 分钟不可用

## `Models` 类型面说明（**类型专用切分**）

### 这是什么 / 不是什么

本批补的是 **`Models` 门面契约的「类型面」**：一个与上游 ``interface Models``
（``models.ts:255-365``）形状对应的 :class:`~pi_ai.models.Models` **Protocol**，
供下游（``pi-durable`` 的 ``TaskRuntime.models``）做类型标注。

**不是**：

- ❌ 没有 ``createModels()`` 之类的门面**实现**
- ❌ 没有 ``Provider`` 运行时/注册表
- ❌ 没有鉴权、OAuth 登录、目录刷新
- ❌ 没有改动本端既有的**扁平注册表**（``_model_registry`` / ``get_model`` /
  ``list_models`` / ``register_api_provider``）——它原样保留

即：**只声明契约，不提供实现**。``Models`` 不可实例化
（``TypeError: Protocols cannot be instantiated``），这是如实声明
「契约存在、实现待补」，不是桩。

### 与「架构错位」（报告 01 §0.2 / Q2）的关系

``recon/01-upstream-ai-delta.md`` §0.2 指出：本端是**扁平注册表**，而上游自
v0.85.1 起是「``Models`` 门面 + ``Provider`` 对象」，并把这个错位列为**排期关键
路径**，在 §8 的 **Q2** 里问「是否补 `Models`/`Provider` 门面」——那是**架构决策，
需用户拍板**。

**本任务不解决它。** 补一个 Protocol 不改变运行时的任何行为：既有的
``get_model`` / ``list_models`` 继续按原样工作，两套东西**并存且互不影响**
（``pi_ai.get_model`` 是注册表查询函数，``Models.get_model`` 是契约方法，同名不同物）。

### 覆盖范围：24 个上游成员中的 9 个

**已声明（9）**，即 ``pi-durable`` 的 ``harness/**`` 实际调用到的 5 个 +
4 个零依赖同期成员：

| 上游成员 | 本端 | pi-durable 调用点 |
|---|---|---|
| `getModel` | `Models.get_model` | `compaction.ts:120,158`、`generation.ts:134,205,238,268` |
| `streamSimple` | `Models.stream_simple` | `generation.ts:431` |
| `completeSimple` | `Models.complete_simple` | `compaction.ts:181` |
| `fetchDeferred` | `Models.fetch_deferred` | `generation.ts:241` |
| `cancelDeferred` | `Models.cancel_deferred` | `generation.ts:271` |
| `getModels` / `getModelsOfType` / `getModelOfType` / `getAllModels` | 同名 snake_case | （未被 durable 调用，零依赖故一并声明） |

**未声明（15）**：见 ``pi_ai.models.MODELS_OMITTED_MEMBERS``（**代码内常量**，
每个都附阻塞原因，并有测试断言"24 = 9 + 15、无遗漏无多余"）。阻塞分三类，
**其中两类与既有决策冲突，故本批不猜形状**：

1. **`Provider`（Q2 架构面）** —— `getProviders` / `getProvider`
2. **鉴权 8 件套（OAuth 越界面）** —— `checkAuth` / `getAvailable` /
   `getAvailableOfType` / `getAllAvailable` / `getAuth` / `login` / `logout`，
   依赖未移植的 `AuthCheck` / `AuthResult` / `AuthType` / `AuthInteraction` /
   `Credential` / `LoginOptions` / `AuthOperationOptions` / `AuthResolutionOverrides`
3. **`ApiStreamOptions<TApi>`（Python 无法等价表达）** —— `stream` / `complete`，
   上游是按 api id 索引的**依赖泛型映射**（`ApiOptionsMap` → 各 api 模块的选项类型，
   本端未移植那些模块）
4. 另有 `refresh`（依赖 `ModelsRefreshOptions/Result` + `AbortSignal` 映射）、
   `generateImages` / `classify`（依赖 task-33 明确延后的 `ImagesOptions` /
   `ClassifierOptions`）、`streamDeferred`（上游另有组合语义待核）

> **给用户的信号：`Models` 的完整形状受两件事门控**
>
> 1. **报告 01 的 Q2** —— 是否补 `Models` / `Provider` 门面（架构决策）。它挡住
>    A 类（`getProviders` / `getProvider`，依赖 `Provider`）。
> 2. **OAuth 越界判断** —— 是否把鉴权类型面（`AuthCheck` / `AuthResult` /
>    `AuthType` / `AuthInteraction` / `Credential` / `LoginOptions` /
>    `AuthOperationOptions` / `AuthResolutionOverrides`）作为**纯类型**声明。
>    它挡住 B 类（7 个鉴权相关成员）。
>
> **这两件一拍板，补 A/B 是机械动作**：C 类是「加一条已登记偏离即可」（`ApiStreamOptions<TApi>`
> 的泛型映射 Python 无法等价表达，改按现有 `StreamOptions` 声明），D 类主要是小形状。
> 之所以停在 9/24 而不是自行补上，是因为**声明 A/B 等于替用户拍上面两个板**，
> 而它们本就登记为「需用户拍板」。
>
> 另注：补全属**契约扩展**而非**契约改写**——`Models` 是 Protocol 且本包无任何实现者，
> 因此将来补 A/B/C/D 不会破坏任何已有实现。

### 已声明的前置形状类型

| 本端 | 上游 | 备注 |
|---|---|---|
| `ProviderHeaders` | `types.ts:148` | 与 `StreamOptions.headers` 同形，零新增语义 |
| `ProviderRequestOptions` | `types.ts:157-...` | 字段名照搬上游；`signal` 按本端既有约定放 `asyncio.Event`（与 `StreamOptions.cancel_event` 同物）；`telemetryContext` 保留占位（telemetry 未移植） |
| `DeferredHandle` | `types.ts:218-229` | **形状 only**：延迟响应特性未移植（`StopReason` 仍缺 `"deferred"`） |
| `DeferredFetchOptions` / `DeferredCancelOptions` | `types.ts:266-275` | 后者上游是纯别名，本端同 |
| `ModelsRequestTransforms` | `models.ts:117-121` | `transformHeaders` 为函数，用 `Any` 承载且不参与序列化 |
| `ModelsSimpleStreamOptions` / `ModelsDeferredFetchOptions` / `ModelsDeferredCancelOptions` | `models.ts:123-125` | 上游是**类型交叉**（`&`）；Python 用**子类**表达（语义等价、类型可用） |

两处 Python 表达差异（如实登记）：

1. 上游 `Models.getModelsOfType<TType extends ModelType>(type): ModelTypeMap[TType][]`
   用**泛型下标**；Python 的 `TypedDict` 不支持 `TypeVar` 下标，改用**按字面量重载**
   （3 条）。传字面量即得精确返回类型；传变量则退化为 `Any`。
2. 上游 `Models.streamSimple(model, context: Context, ...)` 收**公开** `Context`
   （门面内部归一化为 `TranscriptContext`），本端一致——**不是** `TranscriptContext`。

## overflow / validation 说明

### overflow（`utils/overflow.ts`，188 行）

**无模块级可变状态**：三个模式表都是不可变的正则常量，运行时只读（与 `uuid` 的
序列状态形成对比）。转发层无"状态被复制"风险。

导出面与上游一致，共 3 个：

| Python | 上游 | 签名 |
|---|---|---|
| `is_context_overflow` | `isContextOverflow`（`:137-171`） | `(message: AssistantMessage, context_window: int \| None = None) -> bool` |
| `is_recoverable_length` | `isRecoverableLength`（`:179-181`） | `(message: AssistantMessage, desired_max_output: int) -> bool` |
| `get_overflow_patterns` | `getOverflowPatterns`（`:186-188`） | `() -> list[re.Pattern[str]]`（返回**副本**） |

对齐细节：25 条溢出模式与 3 条非溢出模式逐条照搬（含 `[\d,]+` 千分位、
`model'?s` 可选撇号）；`NON_OVERFLOW_PATTERNS` 的**优先排除**（Bedrock 限流的
"Too many tokens" 不得误判为溢出）；`context_window` 用**真值**判断（与 JS 一致，
传 `0`/`None` 等同不传，故不进入静默溢出与 length 截断两种形态）。

### validation（`utils/validation.ts`，353 行）—— **形状有差异，非等价**

导出面与上游一致，共 2 个：

| Python | 上游 | 签名 |
|---|---|---|
| `validate_tool_call` | `validateToolCall`（`:302-308`） | `(tools: list[Tool], tool_call: ToolCall) -> Any` |
| `validate_tool_arguments` | `validateToolArguments`（`:317-353`） | `(tool: Tool, tool_call: ToolCall) -> Any` |

**差异清单（如实登记）**

| 上游 | 本端 | 影响 |
|---|---|---|
| `Value.Convert(schema, args)`（typebox 就地转换，`:323`） | **未复刻** | 见下"为什么可以不复刻" |
| `Compile(schema)`（typebox 生成校验器） | 私有 ``_validate``（JSON Schema 子集） | 见「支持的关键字」 |
| typebox ``.Errors()`` | 同形状 `(path, message)` 列表 | ``  - <path>: <message>`` 前缀、首尾文案一致；**单条 message 措辞不保证与 typebox 逐字相同** |
| `validatorCache`（`WeakMap`，按 schema 对象身份记忆化） | **无缓存** | 纯性能差异；该缓存是透明 memoization，无可观察语义。本端因此**无模块级可变状态** |
| `structuredClone` | `copy.deepcopy` | 等价 |
| `Tool.parameters` 可为 typebox schema | **只能是纯 JSON Schema dict** | 本端 Pydantic 侧无 typebox，故上游那条"非 typebox schema"分支（`:326`）**永远成立** |

**为什么可以不复刻 `Value.Convert`**：它只在 typebox schema 上做就地转换。对纯
JSON Schema，随后**无条件**执行的 `coerceWithJsonSchema`（其触发条件是"schema 上
没有 TypeBox.Kind 符号"——本端永远成立）已经覆盖同等转换，且上游自己的用例期望值
正是由 `coercePrimitiveByType` 的规则产生的。为验证这一点，本端把上游
``test/validation.test.ts`` 的**全部用例逐条移植**为 Python 测试（见下）。

**本端校验器支持的 JSON Schema 关键字**：`$ref`（本地指针，含 `$defs`/`definitions`）/
`type`（字符串或数组）/ `enum` / `const` / `properties` / `required` /
`additionalProperties`（bool 或 schema）/ `items`（含元组形式）/ `minItems` /
`maxItems` / `uniqueItems` / `minProperties` / `maxProperties` / `minimum` / `maximum` /
`exclusiveMinimum` / `exclusiveMaximum` / `multipleOf` / `minLength` / `maxLength` /
`pattern` / `allOf` / `anyOf` / `oneOf`。

**未支持**（被忽略，不报错）：`format` / `not` / `if-then-else` /
`patternProperties` / `dependencies` / `contains` 等。若下游工具 schema 用到这些，
校验会**偏宽松**——需要时按需补。

对齐中的两个 Python 特有坑（均已处理并有测试）：

1. **必须用身份比较**：上游 ``candidate !== nextValue`` 是**严格不等**。Python 若用
   ``!=``，``True != 1`` 为 False 会让 ``1 -> True`` 这类转换被漏判；故用 ``is not``。
2. **``String(value)`` 的 JS 语义**：Python ``str(True)`` 是 ``"True"``、``str(1.0)``
   是 ``"1.0"``，而 JS 分别给 ``"true"``/``"1"``。本端按 JS 语义转换（整数值的 float
   输出整数字面量），有测试覆盖。

另注：错误消息回显用 ``json.dumps(..., ensure_ascii=False)``——JS ``JSON.stringify``
不转义非 ASCII，Python 默认会转成 ``\uXXXX``，中文参数会让报错文本与上游不同。

### 测试对齐

| 上游测试 | 本端 | 用例数 |
|---|---|---|
| `test/overflow.test.ts`（197 行） | `tests/test_overflow.py` | 上游全用例 + 边界（缓存读计入输入、`0` 窗口、入参不被修改等） |
| `test/validation.test.ts`（229 行） | `tests/test_validation.py` | 上游全用例（含 15 条转换表、4 条拒绝表、`$ref` 可空、`oneOf`/`anyOf` 可空、`#10897`）+ 边界（必填/可选、嵌套路径、数组下标、额外字段、枚举/常量、数值与字符串约束、`allOf`、元组 items、入参不被修改、错误消息形状、非 ASCII 回显） |

## uuid 说明（`uuidv7`）

上游 `utils/uuid.ts`（45 行，单一导出 `uuidv7`；v0.85.1 → v1.3.0 期间**未变更**）。
本端**逐位对齐**，并用上游真实实现做了逐字节验证。

### 位布局

| 字节 | 内容 |
|---|---|
| 0–5 | 48 位 Unix 毫秒时间戳（大端） |
| 6 | 高 4 位 = 版本 `0x7`；低 4 位 = 序列 bit[40:37] |
| 7 | 序列 bit[36:29] |
| 8 | 高 2 位 = 变体 `0b10`；低 6 位 = 序列 bit[28:23] |
| 9–10 | 序列 bit[22:15]、bit[14:7] |
| 11 | 序列 bit[6:0] 左移 1 位，**最低位保留首次随机的 bit0** |

序列共占 41 位（4+8+6+8+8+7），与上游 `MAX_SEQUENCE = 2**41 - 1` 一致。

### 单调性语义（两处容易搞错，均有测试）

1. **序列每次调用都 +1**，不只是同毫秒内。配合"普通调用时间戳取
   `max(now, 上次)`"，使 (时间戳‖序列) 这个 89 位整数**严格递增**。
2. **显式传入时间戳时不钳制、也不更新内部基准**。上游注释：
   "A supplied timestamp is preserved for follower ids"（`uuid.ts:7`）。
   因此显式传一个更早的时间戳会让整体**不再单调**——这是**上游有意语义**，
   本端对齐，未"修正"。

### 与 JS 的语言层差异（语义等价）

| 上游 | 本端 | 说明 |
|---|---|---|
| `RangeError` | `ValueError` | Python 对越界值的惯用异常 |
| `globalThis.crypto.getRandomValues` | `os.urandom` | 同为密码学安全随机源 |
| `Number.isInteger` | `_is_integer_like()` | `bool` **不算**整数（Python 的 `bool` 是 `int` 子类，须显式排除）；整数值的 `float` **算**整数（`Number.isInteger(1.0) === true`） |

### 导入路径

规范位置 `pi_ai.uuid`；`pi_ai.utils.uuid` 为对齐上游子路径的**薄转发**；
顶层 `pi_ai.uuidv7` 亦可用（上游 `index.ts` 也从根导出）。

> **⚠️ 转发模块不得复制状态**
> `uuidv7` 依赖 `pi_ai.uuid` 的**模块级序列状态**。转发模块**不得**复制状态
> （否则两条路径产生两个独立序列、破坏单调性）——有测试
> `test_both_import_paths_share_the_same_generator_state` 守护。
> 这是与 `oauth-page` 的关键差别：`oauth-page` 是纯函数，转发无状态问题；
> `uuid` 有状态，转发的正确性需要显式测试。

### 保真度验证

`tests/data/uuid-reference.json` 由**上游 TypeScript 实现**以确定性随机流驱动产出
（生成脚本 `tests/data/generate_uuid_reference.mjs`，随机流公式
`byte(c, i) = (c * 31 + i * 7 + 11) & 0xff` 写在两份文件里并由测试断言一致）。
`test_uuid.py` 以同一字节流驱动本端实现，**12/12 逐字节一致**——位布局由机器判定。

## oauth-page 说明（为什么它不属于被排除的 OAuth 登录面）

本包**排除**的是 provider 的**交互式登录流程**：`auth/oauth/openai-chatgpt.ts`
（Sign in with ChatGPT）、`auth/oauth/meta.ts`（Muse 订阅）、
`auth/oauth/callback-server.ts`（本地回调服务器 + 浏览器交互）、以及
Anthropic workload identity federation。这些涉及**凭据获取**与**网络/端口交互**，
`PORTING.md` 早已登记为「越界」并由用户拍板不纳入。

`utils/oauth-page.ts` 与之**不同**，应当移植：

| | 被排除的登录面 | 本模块 |
|---|---|---|
| 职责 | 获取/刷新凭据 | 把一段文案渲染成 HTML 字符串 |
| 副作用 | 起本地 HTTP 服务、开浏览器、发 token 请求 | **无**（纯函数） |
| 依赖 | node:crypto / http / 各厂 SDK | 无 |
| 上游位置 | `src/auth/oauth/*` | `src/utils/oauth-page.ts`（v1.3.0 由 `auth/oauth/` **迁出**至 `utils/`，见上游 CHANGELOG 0.99.0 Changed） |
| 消费方 | 各 provider 的 login | `coding-agent/src/extensions/mcp/oauth.ts:16`，用于 MCP OAuth 的本地回调页面 |

上游把它从 `auth/oauth/` 迁到 `utils/`，本身就是"这是通用渲染工具、不是登录逻辑"的
信号。故本模块**不推翻**既有排除决定。

**导出面（与上游严格一致，`oauth-page.ts` 仅导出两个函数）**

| Python（规范） | 上游 | 签名 |
|---|---|---|
| `oauth_success_html` | `oauthSuccessHtml`（`:94-100`） | `(message: str) -> str` |
| `oauth_error_html` | `oauthErrorHtml`（`:102-108`） | `(message: str, details: str \| None = None) -> str` |

`escapeHtml`（`:3-10`）与 `renderPage`（`:12-92`）在上游**未导出**，本端同样保持私有
（`_escape_html` / `_render_page`）。

**导入路径**：`from pi_ai import ...`（顶层）、`from pi_ai.oauth_page import ...`
（规范，与其它上游 `utils/*` 模块平铺惯例一致）、
`from pi_ai.utils.oauth_page import ...`（对齐上游子路径，薄转发）。
三者解析到同一批函数对象。

**安全边界**

上游**已实现**转义且顺序正确：`escapeHtml` 先替换 `&` 再替换其余，避免
`<` → `&lt;` → `&amp;lt;` 的二次转义；四个插值字段（title/heading/message/details）
在插值前**全部**转义（`:13-16`）。四个插值点（`<title>`/`<h1>`/`<p>`/`.details`）
均位于**元素内容**位置，不涉及无引号属性、`<style>`、`<script>` 或 URL 上下文，
因此转义集（`& < > " '`）是**充分的，不存在注入面**。本端逐条对齐。

另有一处本端主动加固（非偏离，上游逻辑上等价）：模板占位符替换用**单次正则扫描**
而非连续 `str.replace`。若用后者，用户文案里含 `__MESSAGE__` 之类的标记会在后续
替换轮次被**再次替换**——即用户内容被当作模板指令。单次扫描杜绝该类问题
（`test_user_content_cannot_forge_template_placeholders` 守护）。

**保真度验证**：`tests/data/oauth-page-reference.json` 是**由上游 TypeScript 实现
原样产出**的黄金样本（生成脚本 `tests/data/generate_oauth_page_reference.mjs`，
用 Node ≥22.6 的原生类型剥离直接执行上游 `.ts`）。`test_oauth_page.py`
对 11 个用例（含注入、实体二次转义、Unicode、换行）逐字节比对，**全部一致**。

## cherry-pick

（暂无）

## v0.85.1 同步说明

- **reasoning_details 结构化回放**（OpenRouter 等）：流式 ``delta.reasoning_details``
  （``reasoning.summary`` / ``reasoning.encrypted`` / ``reasoning.text``）按增量收集、
  连续同类合并、加密条目独立，**不发流式事件**；块结束时一次性序列化进
  ``ThinkingContent.thinkingSignature``（错误路径同样补写）。消息回放时优先取
  thinking 签名中的结构化 details，回退到遗留的工具调用 ``thoughtSignature`` 加密
  条目，作为 assistant 消息的 ``reasoning_details`` 字段发送。
- **thinkingTokenBudgetField 泛化**：``compat.thinkingTokenBudgetField`` 指定封顶
  字段名（``thinking_budget`` Qwen/DashScope/SGLang、``thinking_budget_tokens``
  llama.cpp），遗留布尔 ``supportsThinkingTokenBudget`` 等价于 ``thinking_token_budget``。
- **strict JSON Schema 转换**：新增 ``make_strict_json_schema``（可选属性包
  ``anyOf``+null、required 收拢为全部属性、``additionalProperties: false``）；
  strict 工具发送转换后的 schema，schema 无法 strict 化时回退普通工具
  （``strict="require"`` 除外，此时报错）。
- **SimpleStreamOptions.toolChoice**（``"auto"/"none"``，provider 中性）：OpenAI
  适配器透传字符串，Anthropic 适配器映射为 ``{"type": ...}``。
- **Anthropic server-side fallback**：``compat.allowedFallbackModels`` 经请求体
  ``fallbacks``（extra_body）声明；``message_start`` 的响应模型切换时计费改用
  fallback 本地费率；``fallback`` 声明块跳过（出现在输出中间时报错）。
- **Anthropic mid-conversation effort**（``compat.supportsMidConvoEffort``）：adaptive
  thinking + ``block_binding.prefix_mismatch_behavior="drop_block"``，effort 通过
  system 消息按轮切换（历史轮取 ``providerThinkingLevel``，末尾追加当前 effort），
  ``output_config={"effort": "high"}``，且不发送 temperature；相关 beta 经 extra_body
  ``betas`` 发送（SDK 未类型化该字段）。
- **AssistantMessage.providerThinkingLevel / endTurn**、**ToolCall.namespace**：仅类型
  对齐（endTurn/namespace 由未移植的 Responses/Codex 适配器产生）。
- **vllmPriority**：``compat.vllmPriority`` 注入顶层 ``priority`` 请求字段。
- OpenAI-compatible usage 解析补顶层 ``cached_tokens`` 回退（Kimi）。
- 所有 provider 请求统一携带 ``User-Agent: pi (<platform> <release>; <arch>)``
  （``pi_ai.get_pi_user_agent``）。
- retry 分类新增 ``exceeded request buffer limit while retrying upstream``。
- **未移植**（裁剪范围）：Google/Bedrock/Mistral/Codex/Responses 等适配器的本轮
  变更、动态模型目录与 ``models.generated``、OAuth（Copilot/Kimi 登录流程）、
  ``assistant-message-frame.ts``（harness v3 的帧 reducer）、``utils/validation.ts``
  的 optional-null 归一化（本端未移植 typebox 校验）、``detectCompat`` 的 baseUrl
  探测（含 DeepSeek 大小写不敏感检测与 max_tokens 切换）、Anthropic
  ``input_transformations`` 诊断与 ``claude-code`` stealth UA（本端无 OAuth 模式）。
  另注：anthropic SDK 0.117.1 尚不能解析 ``fallback`` 内容块事件，启用
  server-side fallback 需升级 SDK。

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
- [x] v1.3.0 行为级对齐（M-1…M-11、M-13、M-17、M-21、M-22，见「v1.3.0 同步说明」）
- [ ] **M-14** Anthropic strict 工具降级重试（待拍板架构方向）
- [ ] **M-12** mid-conversation system message / 工具变更特性（transcript 骨架已就位）
- [ ] 更多 provider（google / mistral / bedrock）
- [ ] OAuth 鉴权（``auth/*``）——交互登录面仍排除；``utils/oauth-page`` 已补（见「oauth-page 说明」）
- [x] 模型类型族公开符号（仅类型 + 判别 + lazyStream；见「v1.3.0 类型族批次」）
- [x] `utils/oauth-page`（回调页 HTML）与 `utils/uuid`（`uuidv7`）——见对应说明小节
- [x] `utils/overflow`、`utils/validation`——见「overflow / validation 说明」
- [ ] `validation.py` 补 `format`/`not`/`if-then-else` 等关键字（当前偏宽松，按需）
- [ ] **`Models` 门面**：补全其余 15 个成员的类型（需先拍板 `Provider` 与鉴权类型面）；
      实现 `createModels()` / `Provider` 运行时属报告 01 Q2 架构决策
- [ ] classifier / images **能力**实现（§4 越界项，需拍板）
- [ ] 补齐既有缺口：``onPayload``/``onResponse``、图像降级、合成工具结果、
      ``StopReason="deferred"``、``compat.maxTokensField``、session-affinity 头

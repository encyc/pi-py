# 侦察报告 01：上游 `@earendil-works/pi-ai` v0.85.1 → v1.3.0 增量盘点

- 侦察范围：`packages/ai`（npm 包 `@earendil-works/pi-ai`）
- 旧基线：`v0.85.1`（2026-09-05），源码树 `/tmp/pi-up/v0.85.1/packages/ai/`
- 新目标：`v1.3.0`（2026-10-15），源码树 `/tmp/pi-up/v1.3.0/packages/ai/`
- pi-py 现状：`packages/pi-ai/`，`UPSTREAM_VERSION` = `0.85.1`，`PORTING.md` 声明已移植 types / events / event_stream / exceptions / models / stream / providers{faux, openai-completions, anthropic-messages} / retry / constrained_sampling / provider_retry
- 方法：`diff -rq` 取权威清单 → 逐文件读源码确认签名 → 最后用包级 `CHANGELOG.md` 交叉印证（release notes 仅作印证，不作结论来源）

> **证据约定**：本文所有行号形如 `文件:行号`，相对路径根为 `packages/ai/`（即 `/tmp/pi-up/<ver>/packages/ai/`）。`[CHANGELOG]` 标记表示该结论有 `v1.3.0/packages/ai/CHANGELOG.md` 交叉印证。

---

## 0. 结论摘要（TL;DR）

1. **量级**：`src/` 178 → 199 文件。**+29 added / −8 removed / 102 modified / 68 unchanged**（数值自洽：178+29−8=199）。
2. **最大结构性变更**（v0.86.0 Breaking）：provider 面向的入参从 `Context` 改为带 brand 的 `TranscriptContext`；`systemPrompt` / `tools` 折进 transcript 首条 `SystemMessage`。新增 `Message` 联合成员 `role: "system"`。
3. **pi-py 直接受影响的"行为级"修复很多**（不是新功能，是踩坑）：跨模型 thinking 处理反转、JSON 转义 `\u` bug、坏 JSON 工具参数不再静默执行、`Retry-After` 不可解析导致立即重试、重试分类新增 8 类错误串、字符/token 估算 4→3.5、未知端点 strict schema 默认关闭。**这些是"不跟就会行为不一致"的核心。**
4. **最大新增能力**：classifier（JEV 风格结构化分类，4 个后端实现）、image models 并入统一 `Provider`/`Models` 面、mid-conversation system message / 工具变更、Sign in with ChatGPT + Meta OAuth。
5. **对 pi-py 冲击最小的好消息**：pi-py 未移植的适配器（Google / Bedrock / Mistral / Codex / Responses / OpenRouter images / PMML）占了变更面的大头，本轮可继续裁剪。
6. **一个必须先解决的架构错位**：pi-py 的 `models.py:48-100` 是**扁平注册表**（`register_model` / `get_model` / `register_api_provider`），而上游 v0.85.1 起已是 `Models` 门面（`models.ts:255`）+ `Provider` 对象（`models.ts:155`）+ `ProviderStreams`。上游这一轮全部新能力（`getModelsOfType` :272、`getAllModels` :278、`generateImages` :352、`classify` :359、`filterAllModels`）都挂在 `Provider` 接口上——**不先补 `Provider`/`Models` 抽象，后面每一项新能力都要重写一遍接线**。这是本轮排期的关键路径。

---

## 1. 模块级增量表

### 1.1 顶层聚合

| 顶层目录/文件 | 旧 | 新 | added | removed | modified | unchanged |
|---|---:|---:|---:|---:|---:|---:|
| `(src 根文件)` | 19 | 17 | 0 | 2 | 13 | 4 |
| `api/` | 32 | 43 | 11 | 0 | 19 | 13 |
| `auth/`（含 `auth/oauth/`） | 16 | 18 | 3 | 1 | 8 | 7 |
| `compat/` | 1 | 1 | 0 | 0 | 0 | 1 |
| `providers/`（含 `providers/images/`） | 87 | 95 | 11 | 3 | 52 | 32 |
| `utils/` | 23 | 25 | 4 | 2 | 10 | 11 |
| **合计** | **178** | **199** | **29** | **8** | **102** | **68** |

复核命令：

```bash
cd /tmp/pi-up
diff -rq v0.85.1/packages/ai/src v1.3.0/packages/ai/src | grep -c '^Only in v1.3.0'   # 29
diff -rq v0.85.1/packages/ai/src v1.3.0/packages/ai/src | grep -c '^Only in v0.85.1'   # 8
diff -rq v0.85.1/packages/ai/src v1.3.0/packages/ai/src | grep -c '^Files'             # 102
find v0.85.1/packages/ai/src -type f | wc -l   # 178
find v1.3.0/packages/ai/src -type f | wc -l    # 199
```

### 1.2 ADDED（29 个，逐个列出）

| # | 路径 | 行数 | 作用 |
|---|---|---:|---|
| 1 | `src/api/azure-openai-config.ts` | 107 | Azure deployment/baseUrl 解析，供 `azure` provider 的两种 api 复用 |
| 2 | `src/api/classifier-shared.ts` | 128 | classifier HTTP POST + usage 解析公共层 |
| 3 | `src/api/cloudflare-workers-ai-system-one.lazy.ts` | 6 | 懒加载包装 |
| 4 | `src/api/cloudflare-workers-ai-system-one.ts` | 48 | Cloudflare Workers AI 上的 System One classifier |
| 5 | `src/api/llama-cpp-classify.lazy.ts` | 6 | 懒加载包装 |
| 6 | `src/api/llama-cpp-classify.ts` | 459 | llama-server next-token 概率分类 |
| 7 | `src/api/openai-decisions.lazy.ts` | 6 | 懒加载包装 |
| 8 | `src/api/openai-decisions.ts` | 197 | OpenAI Decisions API classifier |
| 9 | `src/api/system-one-shared.ts` | 135 | TypeSafe System One wire 协议公共层 |
| 10 | `src/api/typesafe-system-one.lazy.ts` | 6 | 懒加载包装 |
| 11 | `src/api/typesafe-system-one.ts` | 22 | TypeSafe System One classifier |
| 12 | `src/auth/oauth/callback-server.ts` | — | 统一 OAuth 回调服务器（Anthropic/Codex/OpenRouter/Radius 共用） |
| 13 | `src/auth/oauth/meta.ts` | — | Meta（Muse 订阅）OAuth |
| 14 | `src/auth/oauth/openai-chatgpt.ts` | — | Sign in with ChatGPT OAuth |
| 15 | `src/providers/azure.models.ts` | 14 | 生成目录（含 Chat Completions 模型） |
| 16 | `src/providers/azure.ts` | 55 | `azureProvider()`，api = `azure-openai-responses` + `openai-completions` |
| 17 | `src/providers/compat-schema.ts` | 641 | typebox-first compat 类型定义（原 `types.ts` 内联 interface 迁到这里） |
| 18 | `src/providers/meta.models.ts` | 14 | 生成目录 |
| 19 | `src/providers/meta.ts` | 24 | `metaProvider()`，走 `openai-responses` |
| 20 | `src/providers/model-schema.ts` | 96 | typebox-first 模型元数据类型（cost / thinking / inputLimits / promptCache） |
| 21 | `src/providers/opencode-headers.ts` | 25 | `x-opencode-session` 会话头包装 |
| 22 | `src/providers/openrouter-api.ts` | 77 | OpenRouter pricing/model 类型 + `openRouterCost()` |
| 23 | `src/providers/radius.models.ts` | 14 | Radius 静态基线目录 |
| 24 | `src/providers/typesafe.models.ts` | 14 | 生成目录 |
| 25 | `src/providers/typesafe.ts` | 18 | `typesafeProvider()`，classifier-only |
| 26 | `src/utils/model-operations.ts` | 81 | `getModelType` / `isModelType` / assert* / *ErrorResult |
| 27 | `src/utils/models-error.ts` | 21 | `ModelsError` 抽出为独立模块（原在 `auth/resolve.ts`） |
| 28 | `src/utils/oauth-page.ts` | 109 | OAuth 页面 HTML（自 `auth/oauth/oauth-page.ts` **移动**而来，见 1.3） |
| 29 | `src/utils/transcript.ts` | 237 | transcript 归一化 / system message 回放 / 工具集解析 |

> **注意包装性质**：`providers/compat-schema.ts` 与 `providers/model-schema.ts` 不是新语义，而是把 v0.85.1 `types.ts` 里的 `OpenAICompletionsCompat` / `OpenAIResponsesCompat` / `AnthropicMessagesCompat` / `BedrockCompat` / `OpenRouterRouting` / `VercelGatewayRouting` / `ModelCost*` / `ThinkingLevel*` / `CacheRetention` / `SessionAffinityFormat` / `ThinkingTokenBudgetField` / `ChatTemplateKwargValue` 迁到 typebox schema-first 文件，并在 `types.ts` 顶部 re-export。对 `import ... from "types.ts"` 的调用方**不构成破坏性变更**（`src/types.ts:40-64` 是 re-export 段）。
>
> **`providers/data/*.json`（模型目录数据）不在提供的两棵源码树中**（`ls v1.3.0/.../providers/data` → No such file）。因此**本报告无法给出"哪些具体模型新增/移除/改价"的结论**。所有 38 个 `*.models.ts` 的差异都恰好是 12 行（9+/3−），即纯机械的三行导出改造，不含数据。见 §8 开放问题 Q1。

### 1.3 REMOVED（8 个，逐个列出 + 去向）

| # | 路径 | 替代/去向 | 分类 |
|---|---|---|---|
| 1 | `src/auth/oauth/oauth-page.ts` | **移动**到 `src/utils/oauth-page.ts`（109 行 → 109 行，导出 `oauthSuccessHtml` / `oauthErrorHtml` 两函数一致）。`[CHANGELOG 0.99.0]` 明确"OAuth page helpers 现在是 `@earendil-works/pi-ai/utils/oauth-page`" | IRRELEVANT（pi-py 未移植 OAuth） |
| 2 | `src/image-models.generated.ts` | 合并进 `src/models.generated.ts`；`src/image-models.ts:1` 的 import 由 `./image-models.generated.ts` 改为 `./models.generated.ts` | NEW-CAPABILITY 的配套 |
| 3 | `src/images-models.ts` | **能力删除**：`ImagesModels` 集合被并入统一的 `Provider`/`Models`。`[CHANGELOG 0.99.0 Breaking]`：`createImagesModels()` / `createImagesProvider()` / `ImagesProvider` / `openrouterImagesProvider()` / `builtinImagesProviders()` / `builtinImagesModels()` 全部移除 | NEW-CAPABILITY 的配套 |
| 4 | `src/providers/azure-openai-responses.models.ts` | 重命名 → `src/providers/azure.models.ts`（内容结构同构） | 见 §3 B-9 |
| 5 | `src/providers/azure-openai-responses.ts` | 重写 → `src/providers/azure.ts`（`azureOpenAIResponsesProvider` → `azureProvider`，新增 Chat Completions 支持） | 见 §3 B-9 |
| 6 | `src/providers/openrouter-images.ts` | OpenRouter images provider 被并入 `providers/openrouter.ts`：`openrouter.ts:` 新增 `images: { "openrouter-images": openrouterImagesApi() }` | NEW-CAPABILITY 的配套 |
| 7 | `src/utils/assistant-message-frame.ts` | **能力删除，无替代**。全树 grep `assistant-message-frame` / `AssistantMessageFrame` 在 v1.3.0 `packages/` 下 0 命中。`[CHANGELOG 1.3.0 Breaking]`："Removed `AssistantMessageFrame`, `AssistantMessageFrameEncoder`, and `reduceAssistantMessageFrames()`. They were only used by the removed experimental agent harness." | IRRELEVANT（pi-py 未移植，`PORTING.md` 已注明裁剪） |
| 8 | `src/utils/deferred-tools.ts` | **能力删除，无替代**。全树 grep `deferred-tools` / `splitDeferredTools` 在 v1.3.0 `src/` 下 0 命中。语义被两套机制替代：(a) OpenAI-compatible 侧 `compat.deferredToolsMode: "kimi"` 移除，改为 transcript 锚定的 `toolsAdded`；(b) Anthropic 侧 `supportsToolReferences` 移除，改为 `inline-tools-2026-09-15` beta 的 `tool_addition`/`tool_removal` 块 | MUST-PORT（见 §3 M-12） |

复核命令：

```bash
cd /tmp/pi-up
grep -rn "deferred-tools\|assistant-message-frame" v1.3.0/packages/ai/src/   # 无输出
diff <(grep -n '^export ' v0.85.1/packages/ai/src/auth/oauth/oauth-page.ts | sed 's/^[0-9]*://') \
     <(grep -n '^export ' v1.3.0/packages/ai/src/utils/oauth-page.ts | sed 's/^[0-9]*://')   # 无差异
```

### 1.4 MODIFIED（102 个）

**根文件（13）**
`bun-oauth.ts` `cli.ts` `compat.ts` `env-api-keys.ts` `image-models.ts` `images-api-registry.ts` `images.ts` `index.ts` `model-catalog.ts` `models-store.ts` `models.generated.ts` `models.ts` `types.ts`

**`api/`（19）**
`anthropic-messages.ts` `azure-openai-responses.ts` `bedrock-converse-stream.ts` `cloudflare.ts` `constrained-sampling.ts` `google-generative-ai.ts` `google-shared.ts` `google-vertex.ts` `lazy.ts` `mistral-conversations.ts` `openai-codex-responses.ts` `openai-completions.ts` `openai-responses-shared.ts` `openai-responses.ts` `openrouter-images.lazy.ts` `openrouter-images.ts` `pi-messages.ts` `simple-options.ts` `transform-messages.ts`

**`auth/`（8）**
`helpers.ts` `oauth/anthropic.ts` `oauth/load.ts` `oauth/openai-codex.ts` `oauth/openrouter.ts` `oauth/radius.ts` `resolve.ts` `types.ts`

**`providers/`（52 = 38 机械 + 14 实质）**

- 机械改造（38，每个恰好 9+/3−，仅改 `flattenModelCatalog` → `flattenChatModelCatalog` 并新增 IMAGE/CLASSIFIER 两个导出）：
  `amazon-bedrock` `ant-ling` `anthropic` `baseten` `cerebras` `cloudflare-ai-gateway` `cloudflare-workers-ai` `deepseek` `fireworks` `github-copilot` `google` `google-vertex` `groq` `huggingface` `kimi-coding` `minimax` `minimax-cn` `mistral` `moonshotai` `moonshotai-cn` `nvidia` `openai` `openai-codex` `opencode` `opencode-go` `openrouter` `qwen-token-plan` `qwen-token-plan-cn` `qwen-token-plan-individual` `together` `vercel-ai-gateway` `xai` `xiaomi` `xiaomi-token-plan-ams` `xiaomi-token-plan-cn` `xiaomi-token-plan-sgp` `zai` `zai-coding-cn`（均为 `*.models.ts`）
- 实质改造（14）：
  `all.ts`(provider 注册表) `anthropic.ts`(federation auth) `cloudflare-stream.ts` `cloudflare-workers-ai.ts` `faux.ts` `images/register-builtins.ts` `kimi-coding.ts` `openai.ts`(ChatGPT OAuth + classifier + filterAllModels) `openai-codex.ts` `opencode.ts` `opencode-go.ts` `radius.ts` `openrouter.ts`(image+classifier+`personalizeOpenRouterCatalog`) `vercel-ai-gateway.ts`

**`utils/`（10）**
`diagnostics.ts` `estimate.ts` `event-stream.ts` `headers.ts` `json-parse.ts` `overflow.ts` `provider-retry.ts` `retry.ts` `text.ts` `validation.ts`

### 1.5 UNCHANGED（68 个，字节级相同）

- 根文件（4）：`bedrock-provider.ts` `legacy-api-aliases.ts` `oauth.ts` `session-resources.ts`
- `api/`（13）：`anthropic-messages.lazy.ts` `azure-openai-responses.lazy.ts` `bedrock-converse-stream.lazy.ts` `cloudflare-ai-binding.ts` `github-copilot-headers.ts` `google-generative-ai.lazy.ts` `google-vertex.lazy.ts` `mistral-conversations.lazy.ts` `openai-codex-responses.lazy.ts` `openai-completions.lazy.ts` `openai-prompt-cache.ts` `openai-responses.lazy.ts` `pi-messages.lazy.ts`
- `auth/`（7）：`context.ts` `credential-store.ts` `oauth/device-code.ts` `oauth/github-copilot.ts` `oauth/kimi-coding.ts` `oauth/pkce.ts` `oauth/xai.ts`
- `compat/`（1）：`extension-oauth-types.ts`
- `providers/`（32）：`amazon-bedrock.ts` `ant-ling.ts` `baseten.ts` `cerebras.ts` `cloudflare-ai-gateway.ts` `cloudflare-auth.ts` `data-json.d.ts` `deepseek.ts` `fireworks.ts` `github-copilot.ts` `google-vertex.ts` `google.ts` `groq.ts` `huggingface.ts` `minimax-cn.ts` `minimax.ts` `mistral.ts` `moonshotai-cn.ts` `moonshotai.ts` `nvidia.ts` `qwen-token-plan-cn.ts` `qwen-token-plan-individual.ts` `qwen-token-plan.ts` `radius-config.ts` `together.ts` `xai.ts` `xiaomi-token-plan-ams.ts` `xiaomi-token-plan-cn.ts` `xiaomi-token-plan-sgp.ts` `xiaomi.ts` `zai-coding-cn.ts` `zai.ts`
- `utils/`（11）：`abort-signals.ts` `abort.ts` `error-body.ts` `hash.ts` `node-http-proxy.ts` `pi-user-agent.ts` `provider-env.ts` `sanitize-unicode.ts` `sleep.ts` `typebox-helpers.ts` `uuid.ts`

### 1.6 包外围（非 `src/`，一并记录）

- `package.json`：`version` 0.85.1 → 1.3.0；新增 `"./models"` 子路径导出；移除 `generate-image-models` script；`build:offline` 的 `tsgo` → `tsc`；依赖大版本跳跃（`@anthropic-ai/sdk` 0.123.0→0.129.0、`openai` 6.40.0→7.19.0、`@google/genai` 1.52.0→2.21.0、`@aws-sdk/client-bedrock-runtime` 3.1048.0→3.1127.0、`http-proxy-agent` 7.0.2→9.1.0、`typebox` 1.3.7→1.3.27）
- `scripts/`：新增 `ai-gateway-pricing.ts` `hydrate-model-catalog.ts` `openrouter-catalog.ts`；移除 `generate-image-models.ts`；`generate-models.ts` `model-data.ts` 有改动
- `test/`：新增约 40 个测试文件（含 `classifier-models` `llama-cpp-classify` `typesafe-system-one` `openai-decisions` `meta-oauth` `openai-chatgpt-oauth` `oauth-callback-server` `transcript-tool-changes` `transform-messages-cross-model-thinking` `system-message-replay` `event-stream` `json-parse` `model-cost-tiers` 等），**可作为 pi-py 移植的行为规格**（见 §7）

---

## 2. 三分类总表

图例：**MP** = MUST-PORT（影响已有 SDK 公开 API 或运行时行为）；**NC** = NEW-CAPABILITY；**IR** = IRRELEVANT。

| 变更组 | 分类 | 说明 |
|---|---|---|
| `TranscriptContext` + `normalizeContext()` 归一化 | **MP** | 所有 provider 面向签名变更 |
| `Message` 联合新增 `SystemMessage` | **MP** | 公开类型变更 |
| 跨模型 thinking 丢弃（不再转文本） | **MP** | v1.3.0 行为反转 |
| `parseToolCallArguments` + `argumentsError` + 校验拒绝 | **MP** | 坏 JSON 不再静默执行 |
| `json-parse` 合法转义集去掉 `"u"` | **MP** | `\usepackage` 丢字段 bug |
| 重试分类新增错误串（8 类） | **MP** | 不跟 → 不重试 |
| `RetryPolicy.maxAgentDelayMs` 60s 默认封顶 | **MP** | 默认行为变更 |
| `provider-retry` `Number.isFinite` 守卫 | **MP** | 不可解析 `Retry-After` 的立即重试 bug |
| `provider-retry` `noRetryStatuses` | **MP** | 新选项 |
| `utils/estimate` `CHARS_PER_TOKEN` 4 → 3.5 | **MP** | 输出上限估算 |
| `utils/overflow` z.ai / Cerebras / 非 Cerebras 判定 | **MP** | 溢出识别 |
| `utils/event-stream` FifoQueue + `durationMs` | **MP** | 性能 bug + 新字段 |
| `utils/headers` `providerHeadersToRecord` 变参 + 大小写折叠 | **MP** | **签名破坏** |
| `utils/validation` 拒绝 `argumentsError` | **MP** | 与上面配套 |
| `types.ts` `ToolCall.arguments: JsonObject` / `JsonValue` readonly / `JsonRepresentation` | **MP** | 公开类型收紧 |
| `ToolResultMessage` 条件类型 + `nestedCalls` + `durationMs` | **MP** | 公开类型变更 |
| `AssistantMessage.durationMs` / `thinkingLevel` | **MP** | 新字段（`durationMs` 自动写入） |
| `openai-completions` reasoning 先于 content | **MP** | LiteLLM 顺序 bug |
| `openai-completions` `supportsStrictMode` 默认改 `false` | **MP** | 行为反转 |
| `openai-completions` session-affinity 默认值改 `isOpenRouter` | **MP** | 行为变更 |
| `resolveSamplingParams()` + `samplingParamsByThinkingLevel` | **MP** | `model.samplingParams` 直连时被丢的 bug |
| `anthropic-messages` strict 工具降级重试 | **MP** | 400 回退 |
| Anthropic strict 不支持关键字集 | **MP** | `minimum` 等 400 |
| Anthropic `output.responseModel` 不再覆盖 `model` | **MP** | 计费/回放正确性 |
| Anthropic `x-session-id`（OpenRouter） | **MP** | 缓存路由 |
| Anthropic `thinking: {type:"disabled"}` 无 effort | **MP** | Haiku 5.5 / Opus 5 400 |
| `splitErrorToolResultImages`（错误 tool result 带图） | **MP** | Anthropic/Bedrock 400 |
| `api/lazy.ts` setup 失败用请求开始时间戳 | **MP** | 时间戳语义 |
| `model-catalog.ts` `ModelCatalog` → `ChatModelCatalog` 等重命名 | **MP** | **导出名破坏**（若 pi-py 对齐命名） |
| `env-api-keys` azure key 改名 + `TYPESAFE_API_KEY` / `META_API_KEY` | **MP** | provider 键名 |
| `KnownProvider` 新增 `azure` / `typesafe` / `meta` | **MP** | 枚举对齐 |
| `providers/*.models.ts` ×38 机械改造 | **MP**（若移植生成目录）/ **NC**（否则） | 依赖 `model-catalog` 改名 |
| mid-conversation system message / 工具变更（transcript 回放） | **NC** | 新特性；但 `TranscriptContext` 骨架是 MP |
| classifier 全家桶（4 API + 类型 + 4 provider 接入） | **NC** | 全新能力 |
| image models 并入统一 `Provider`/`Models` | **NC** | 全新能力；pi-py 未移植 images |
| `Models` 门面 *OfType / getAll* / `generateImages` / `classify` | **NC**（但对齐上游架构是必要前置） | 见 §0.6 |
| `promptCache` 元数据、`inputLimits`（图像 resize） | **NC** | 新元数据 |
| `onProviderStreamEvent` | **NC** | 观察钩子 |
| Sign in with ChatGPT / Meta OAuth / 统一 callback server | **NC** | OAuth |
| Anthropic workload identity federation | **NC** | 新鉴权方式 |
| `personalizeOpenRouterCatalog()`（per-key 模型列表） | **NC** | 动态目录 |
| `azure` provider 支持 Chat Completions | **NC** | provider 扩展 |
| `meta` / `typesafe` provider | **NC** | 新 provider |
| `./models` 子路径入口（不加载 typebox/catalog/SDK） | **NC** | 打包形态（Python 侧无对应） |
| `providers/compat-schema.ts` / `model-schema.ts` 物理迁移 | **IR**（类型等价迁移） | 见 1.2 注 |
| `cli.ts`（dev OAuth CLI） | **IR** | CLI |
| `bun-oauth.ts` | **IR** | Bun 打包胶水 |
| `scripts/*`、`package.json` 构建项 | **IR** | 打包/发布 |
| Google / Bedrock / Mistral / Codex / Responses / OpenRouter-images / PMML 适配器改动 | **IR**（本轮） | pi-py 明确未移植 |

---

## 3. 破坏性变更清单

`[CHANGELOG]` 标注为上游自认为 Breaking 的项。每条给 before/after、影响模块、迁移动作。

### M-1 `Context` → `TranscriptContext`（v0.86.0 Breaking）

- **Before**（`v0.85.1/src/types.ts:272`、`:333`）：
  ```ts
  export interface ProviderStreams {
      stream(model: Model<Api>, context: Context, options?: StreamOptions): AssistantMessageEventStream;
      streamSimple(model: Model<Api>, context: Context, options?: SimpleStreamOptions): AssistantMessageEventStream;
  }
  export type StreamFunction<TApi extends Api = Api, TOptions extends StreamOptions = StreamOptions> = (
      model: Model<TApi>, context: Context, options?: TOptions,
  ) => AssistantMessageEventStream;
  ```
  `Context`（`v0.85.1/src/types.ts:524`）= `{ systemPrompt?: string; messages: Message[]; tools?: Tool[] }`
- **After**（`v1.3.0/src/types.ts:311-320`、`:390`、`:771`、`:785`）：
  ```ts
  export interface ProviderStreams {
      stream(model: Model<Api>, context: TranscriptContext, options?: StreamOptions): AssistantMessageEventStream;
      streamSimple(model: Model<Api>, context: TranscriptContext, options?: SimpleStreamOptions): AssistantMessageEventStream;
  }
  export type StreamFunction<TApi extends Api = Api, TOptions extends StreamOptions = StreamOptions> = (
      model: Model<TApi>, context: TranscriptContext, options?: TOptions,
  ) => AssistantMessageEventStream;

  export interface Context { systemPrompt?: string; messages: Message[]; tools?: Tool[] }   // 公开入参不变
  declare const transcriptContextBrand: unique symbol;
  export type TranscriptContext = { messages: Message[]; readonly [transcriptContextBrand]: true };
  ```
  唯一生产者是 `utils/transcript.ts:30 normalizeContext(context)`（把 `systemPrompt`/`tools` 折成首条 `SystemMessage`）。`Message` 联合扩展为 `SystemMessage | UserMessage | AssistantMessage | ToolResultMessage`（`v1.3.0/src/types.ts:644`）。
- **`[CHANGELOG 0.86.0 Breaking]`**："Changed provider-facing `ProviderStreams` and `StreamFunction` inputs from `Context` to normalized `TranscriptContext` values. … custom providers must read them with `getCurrentSystemPrompt()` and `getCurrentTools()`."
- **影响 pi-py**：`types.py`（`Context`）、`models.py:25-40`（`ProviderStreams` Protocol）、`stream.py:18-35`（`stream` / `stream_simple` 入口）、`providers/openai_provider.py` / `anthropic_provider.py`（读 `context.systemPrompt` 的位置）。
- **迁移动作**：
  1. `types.py` 增 `SystemMessage` 模型 + `Message` 联合加 `role="system"`；
  2. 新增 `transcript.py`（对应 `utils/transcript.ts`），实现 `normalize_context()` / `get_system_message_text()` / `get_current_tools()` / `collapse_system_messages()` / `resolve_transcript()`；
  3. `stream.py` 入口对 `Context` 调 `normalize_context()` 后再分发（与上游 `compat.ts:254-263` 同构）；
  4. provider 侧 `context.systemPrompt` → `get_initial_system_message(context.messages)` 后再取文本，`context.tools` → `get_current_tools(context.messages)`；
  5. `stream.py` / `models.py:110-138 register_builtins` 行为不变（上游公开 `Context` 仍是 `systemPrompt` + `tools`，调用方不用改）。
- **工作量：M**（新增一个模块 + 改 2 个 provider + 改入口；但语义边界清晰）

### M-2 `Message` / `SystemMessage` 公开类型新增

- **After**（`v1.3.0/src/types.ts:546-562`）：
  ```ts
  export interface SystemMessage {
      role: "system";
      content: string | TextContent[];
      sections?: Record<string, string | null>;
      toolsAdded?: Tool[];
      toolsRemoved?: ToolReference[];      // ToolReference = { name: string }  (:761)
      timestamp: number;
  }
  ```
- **影响 pi-py**：`types.py:287` 的 `Message = Annotated[...]` 判别联合。
- **迁移动作**：加 `SystemMessage` 并纳入联合。注意 `sections` 值可为 `null`（表示删除该 section）。
- **工作量：S**

### M-3 `ToolCall.arguments` 收紧 + `argumentsError`（v0.86.0 Breaking）

- **Before**（`v0.85.1/src/types.ts:373-380`）：`arguments: Record<string, any>;`（`namespace?: string` 在 `:380`）
- **After**（`v1.3.0/src/types.ts:436-449`）：
  ```ts
  arguments: JsonObject;            // :440
  thoughtSignature?: string;        // :441
  namespace?: string;               // :443
  /** Set when the model's complete arguments text was not a valid JSON object. `arguments` is then `{}`. */
  argumentsError?: string;          // :448
  ```
- **`[CHANGELOG 0.86.0 Breaking]`**："Restricted `ToolCall.arguments` and `ToolResultMessage.details` to JSON-compatible values, changed `ToolResultMessage` into a conditional type, and made `JsonValue` arrays readonly."
- **附带** `JsonValue`（`v1.3.0/src/types.ts:476`）：`null | boolean | number | string | readonly JsonValue[] | JsonObject`（数组变 `readonly`）；新增 `JsonObject`（`:477`）与推导类型 `JsonRepresentation<T>`（`:512`）。
- **影响 pi-py**：`types.py:155 ToolCall`、`:172 AssistantContentBlock`、`constrained_sampling.py`。
- **迁移动作**：`ToolCall` 加 `arguments_error: str | None = Field(default=None, alias="argumentsError")`；`arguments` 类型在 Python 侧保持 `dict[str, Any]`（Pydantic 无法表达递归 JsonValue 的实用收益），但**必须新增该字段**，否则 §M-4 的校验联动断链。
- **工作量：S**

### M-4 工具参数坏 JSON 不再静默执行

- **Before**（`v0.85.1/src/utils/json-parse.ts` 只有 `parseStreamingJson`；`v0.85.1/src/api/openai-completions.ts:452` 与 `v0.85.1/src/api/anthropic-messages.ts` 收尾处直接 `block.arguments = parseStreamingJson(block.partialArgs)`）：坏 JSON 时保留已解析前缀字段，工具照常执行。
- **After**：
  - 新增 `parseToolCallArguments(json)`（`v1.3.0/src/utils/json-parse.ts:106-124`）：
    ```ts
    export function parseToolCallArguments(json: string | undefined): Pick<ToolCall, "arguments" | "argumentsError"> {
        if (!json?.trim()) return { arguments: {} };
        try {
            const parsed = parseJsonWithRepair<ToolCall["arguments"] | null>(json);
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
                throw new Error("Tool arguments must be a JSON object");
            return { arguments: parsed };
        } catch (error) {
            return { arguments: {}, argumentsError: `${...}\n\nReceived arguments:\n${json}` };
        }
    }
    ```
  - 两个适配器改为 `Object.assign(block, parseToolCallArguments(...))`：`v1.3.0/src/api/openai-completions.ts:464`、`v1.3.0/src/api/anthropic-messages.ts:839`（原 `parseStreamingJson` 调用点）。
  - 校验侧：`v1.3.0/src/utils/validation.ts:318` `if (toolCall.argumentsError) throw new Error(\`Invalid arguments for tool "${toolCall.name}": ${toolCall.argumentsError}\`)`。
- **`[CHANGELOG 1.3.0 Fixed]`** #10897："tool calls whose final arguments are malformed JSON silently running with only the fields parsed before the error; such calls now carry `argumentsError` and fail validation with the raw arguments text"。
- **影响 pi-py**：`providers/openai_provider.py`（参数收尾）、`providers/anthropic_provider.py`（同）、`types.py`
- **迁移动作**：新增 `parse_tool_call_arguments()`，两个 provider 收尾处改用它；若 pi-py 后续移植 `validation.ts`，需加 `arguments_error` 首检。**流式增量阶段仍用 `parseStreamingJson` 等价物，不要改。**
- **工作量：M**（涉及 2 个 provider + 新函数 + 与 M-3 联动）

### M-5 JSON 合法转义集去掉 `"u"`

- **Before**（`v0.85.1/src/utils/json-parse.ts:3`）：`new Set(['"', "\\", "/", "b", "f", "n", "r", "t", "u"])`
- **After**（`v1.3.0/src/utils/json-parse.ts:4`）：`new Set(['"', "\\", "/", "b", "f", "n", "r", "t"])`
- **`[CHANGELOG 1.3.0 Fixed]`** #10868："tool-call argument parsing dropping fields when a string contains an invalid `\u` escape such as `\usepackage`"。
- **影响 pi-py**：`providers/*.py` 的容错 JSON 修复逻辑（pi-py 用 `json-repair` 库，见 `PORTING.md` "有意偏离"）。**这是移植偏差点**：pi-py 依赖 `json-repair`，其行为不由 pi-py 控制，需实测 `\usepackage` 场景。
- **迁移动作**：构造 `{"path": "C:\\usepackage"}` 类回归用例，确认修复后不丢字段。
- **工作量：S**（可能只是加测试；若是库缺陷需改为自研修补）

### M-6 跨模型 thinking 处理反转（v1.3.0 Breaking 行为）

- **Before**（`v0.85.1/src/api/transform-messages.ts:99-115` 附近）：`redacted` thinking 跨模型丢弃；有签名的同模型保留；**其他情况把 thinking 转成 `{ type: "text", text: block.thinking }`**。
- **After**（`v1.3.0/src/api/transform-messages.ts:126-140`）：
  ```ts
  if (block.type === "thinking") {
      // Thinking from another model is dropped. …
      if (!isSameModel) return [];
      if (block.redacted || block.thinkingSignature) return block;
      if (!block.thinking || block.thinking.trim() === "") return [];
      return block;
  }
  ```
- **`[CHANGELOG 1.3.0 Changed]`** #10995："model switching to drop thinking blocks from earlier assistant messages produced by a different model, instead of sending them as plain text."
- **影响 pi-py**：`providers/openai_provider.py` / `providers/anthropic_provider.py` 的消息转换（pi-py 目前把 transform 逻辑内联在 provider 里，没有独立 `transform_messages.py`）。
- **迁移动作**：跨模型 thinking → 丢弃；同模型且 `redacted` 或 `thinking_signature` 非空 → 保留（即使 thinking 文本为空）；其余空 thinking → 丢弃。**注意这是反转，不是新增**。
- **工作量：S**

### M-7 `utils/headers.ts: providerHeadersToRecord` 签名破坏

- **Before**（`v0.85.1/src/utils/headers.ts:11-19`）：
  ```ts
  export function providerHeadersToRecord(headers: ProviderHeaders | undefined): Record<string, string> | undefined
  ```
- **After**（`v1.3.0/src/utils/headers.ts:11-24`）：
  ```ts
  export function providerHeadersToRecord(
      ...headerSources: (ProviderHeaders | undefined)[]
  ): Record<string, string> | undefined
  ```
  语义变化：多个来源**按小写名折叠**，后者覆盖前者，`null` 表示删除该键。
- **影响 pi-py**：`user_agent.py` / provider 的 header 合并路径（若 pi-py 有对应函数）。
- **迁移动作**：改为变参 + 小写折叠；这是 [#10429] "model and caller headers not overriding `originator`/`User-Agent` of Codex requests" 的支撑。
- **工作量：S**

### M-8 `utils/retry.ts`：延迟封顶 + 重试分类扩容

- **Before**（`v0.85.1/src/utils/retry.ts:103` `baseDelayMs: number;`；实际延迟在 `:226` 附近 `const delayMs = policy!.baseDelayMs * 2 ** (attempt - 1);`）
- **After**（`v1.3.0/src/utils/retry.ts:124-137`）：
  ```ts
  export interface RetryPolicy {
      maxRetries: number;
      baseDelayMs: number;
      /** Optional cap for agent-level retry delays in ms. Defaults to 60 seconds. */
      maxAgentDelayMs?: number;                                    // :128
  }
  export const DEFAULT_MAX_AGENT_RETRY_DELAY_MS = 60_000;          // :131
  export function retryDelayMs(policy, attempt: number): number {  // :133
      const delay = policy.baseDelayMs * 2 ** Math.max(0, attempt - 1);
      const safeDelay = Number.isSafeInteger(delay) ? delay : Number.MAX_SAFE_INTEGER;
      return Math.min(safeDelay, policy.maxAgentDelayMs ?? DEFAULT_MAX_AGENT_RETRY_DELAY_MS);
  }
  ```
  调用点 `:229` 改为 `retryDelayMs(policy!, attempt)`。
- **⚠️ 默认行为变更**：即使调用方不设 `maxAgentDelayMs`，延迟也被封顶 60s（原为无上限指数增长）。`[CHANGELOG 0.86.0 Added]` #8826 / `[v1.0.4 release note]` "Capped agent-level retry backoff at `retry.maxAgentDelayMs` (60s by default)"。
- **新增可重试错误串**（`v1.3.0/src/utils/retry.ts`，`RETRYABLE_PROVIDER_ERROR_PATTERN` 与 `RETRYABLE_ASSISTANT_ERROR_PATTERNS`）：
  - 新增：`server_busy`、`servers are currently busy`、`currently experiencing high demand`、`model is at capacity`、`520`、`unable to forward upstream`、`pending stream has been canceled`、`subscription_sharing_usage_limit_exceeded`、`subscription_sharing_usage_unavailable`、`subscription_sharing_user_unavailable`
  - 改写：`service.?unavailable` → `service.?(?:is.?)?(?:temporarily.?)?unavailable`（覆盖 "The service is temporarily unavailable" #10965）；`provider.?returned.?error` → `provider.?returned.?(?:an.?)?error`（覆盖 "The upstream provider returned an error" #10842）
- **影响 pi-py**：`retry.py`（`RetryPolicy` / 可重试正则集 / 延迟计算）。
- **迁移动作**：
  1. `RetryPolicy` 加 `max_agent_delay_ms: int = 60_000`，延迟计算封顶并防溢出；
  2. 同步正则集新增/改写项（含 `"520"`、`"server_busy"` 等）；
  3. 注意 `subscription_sharing_*` 属 Sign in with ChatGPT 专属（NC），但**前 6 项对现有 provider 立即有效**。
- **工作量：S**（纯正则 + 一个封顶函数）

### M-9 `utils/provider-retry.ts`：不可解析 `Retry-After` 立即重试 bug + `noRetryStatuses`

- **Before**（`v0.85.1/src/utils/provider-retry.ts:55`、`:62`）：`if (!Number.isNaN(value)) return validate...` / `return validateServerRetryDelayMs(delayMs, ...)`（后者无条件返回，`delayMs` 为 `NaN` 时也进入退避计算 → 立即重试）
- **After**（`v1.3.0/src/utils/provider-retry.ts:57`、`:64`）：`if (Number.isFinite(value))` / `if (Number.isFinite(delayMs))`；并在 `:8` 新增 `noRetryStatuses?: readonly number[]`，`:121` `if (error.status !== undefined && options.noRetryStatuses?.includes(error.status)) throw error;`
- **`[CHANGELOG 0.99.2 Fixed]`** #9571："provider retries firing immediately when a `Retry-After` header contains an unparseable date; they now use exponential backoff"。
- **影响 pi-py**：`provider_retry.py`。
- **迁移动作**：`Retry-After` 解析加 isfinite 守卫（Python 侧注意 `float("nan")` / `float("inf")`）；加 `no_retry_statuses` 参数。
- **工作量：S**

### M-10 `utils/estimate.ts`：字符/token 比 4 → 3.5

- **Before**（`v0.85.1/src/utils/estimate.ts:14`）：`const CHARS_PER_TOKEN = 4;`
- **After**（`v1.3.0/src/utils/estimate.ts:15`）：`const CHARS_PER_TOKEN = 3.5;`
- **签名变化**：`estimateMessages`（内部）不再导出；`estimateContextTokens` 改为（`v1.3.0/src/utils/estimate.ts:97`）：
  ```ts
  export function estimateContextTokens(context: TranscriptContext | readonly Message[]): ContextUsageEstimate
  ```
  （旧版接受 `Context | readonly Message[]`，并在有 `systemPrompt`/`tools` 时补 prefix token；新版该逻辑移入 system message 分支 `:49-55`）
- **`[CHANGELOG 1.1.0 Fixed]`** #10497："Reduced context-limit request failures by estimating input at 3.5 characters per token instead of 4 when calculating output limits."
- **影响 pi-py**：`types.py` / provider 的 context token 估算（`clampMaxTokensToContext` 等价物）；若未移植则仅记录。
- **迁移动作**：常量改 3.5；估算函数接受 `Context | list[Message]`，并把 `systemPrompt`/`tools` 折算为 system message 参与估算。
- **工作量：S**

### M-11 `utils/event-stream.ts`：FifoQueue + `durationMs`

- **Before**（`v0.85.1/src/utils/event-stream.ts:4-` ）：`private queue: T[] = []` + `this.queue.shift()`（数组头删，drain 时 O(n²)）
- **After**（`v1.3.0/src/utils/event-stream.ts:3-20`）新增 `FifoQueue<T>`（incoming/outgoing 双栈）；`:19` `protected done = false`（由 `private` 放宽）；`AssistantMessageEventStream`（`:97`）新增：
  ```ts
  readonly #startedAt = Date.now();
  readonly #startedAtMonotonic = performance.now();
  override push(event) { if (event.type === "done") this.#time(event.message); else if (event.type === "error") this.#time(event.error); super.push(event); }
  override end(result?) { if (result !== undefined) this.#time(result); super.end(result); }
  #time(message) { if (this.done || message.durationMs !== undefined || message.timestamp < this.#startedAt) return;
                   message.durationMs = Math.max(0, Math.round(performance.now() - this.#startedAtMonotonic)); }   // :128
  ```
- **`[CHANGELOG 0.86.0 Fixed]`** #9055（O(n²) drain）；`[CHANGELOG 1.1.0 Added]` #10549（`durationMs`）。
- **影响 pi-py**：`event_stream.py`、`events.py`、`types.py`（`AssistantMessage.durationMs`）。
- **迁移动作**：队列改双栈/`collections.deque`（避免 `list.pop(0)`）；`AssistantMessageEventStream` 记录单调起点并在 done/error/end 时写 `duration_ms`（`timestamp < started_at` 时跳过，兼容 deferred）。
- **工作量：S/M**

### M-12 deferred tools 机制替换（`deferredToolsMode` / `supportsToolReferences` 移除）

- **Before**：
  - `OpenAICompletionsCompat.deferredToolsMode?: "kimi"`，openai-completions 侧 `getDeferredToolNames()` 扫 `ToolResultMessage.addedToolNames`，并在 tool 结果后插入 `{ role: "system", tools: [...] }`（`v0.85.1/src/api/openai-completions.ts` 多处）
  - `AnthropicMessagesCompat.supportsToolReferences?: boolean` + `defaultSupportsToolReferences(model)` + `splitDeferredTools(...)`（`v0.85.1/src/api/anthropic-messages.ts`）
  - `ToolResultMessage.addedToolNames?: string[]`（`v0.85.1/src/types.ts:452` 内）
- **After**：
  - `ToolResultMessage` 移除 `addedToolNames`；改由 `SystemMessage.toolsAdded` / `toolsRemoved` 表达（`v1.3.0/src/types.ts:558-560`）
  - openai-completions：`resolveTranscriptTools(context.messages, compat.supportsMidConvoSystemMessages === true && compat.supportsMidConvoToolAdditions === true)`（`v1.3.0/src/api/openai-completions.ts:815-818`（`buildParams`）与 `:1231-1234`（`convertMessages`）），system 消息携带 `toolsAdded` 时发出 `{ role: "system", tools: convertTools(added) }`（`v1.3.0/src/api/openai-completions.ts:1250-1257`）
  - anthropic：`supportsToolReferences` 移除，改为 `supportsMidConvoToolChanges` + `INLINE_TOOLS_BETA = "inline-tools-2026-09-15"`（`v1.3.0/src/api/anthropic-messages.ts:199`）+ `DEFERRED_TOOL_PLACEHOLDER`（`:208`）+ `tool_addition`/`tool_removal` 块（`:1390-1398`，`convertToolDefinitions` 形参 `:1359`）
  - `compat.deferredToolsMode` **字段删除**（`v0.85.1/src/types.ts` 内 → v1.3.0 无）
- **影响 pi-py**：`types.py`（`ToolResultMessage.added_tool_names` 若已移植）、`providers/openai_provider.py`、`providers/anthropic_provider.py`。**`PORTING.md` 未提及 pi-py 移植过 `addedToolNames`，需先确认（见 Q3）。**
- **迁移动作**：若 pi-py 未移植 → 直接跳过（记 IRRELEVANT）。若已移植 → 随 M-1 transcript 一起改。
- **工作量：S（未移植）/ M（已移植）**

### M-13 `api/openai-completions.ts` 行为变更（多）

1. **reasoning 先于 content**（`v1.3.0/src/api/openai-completions.ts:592` 注释 + `:641` 的 content 分支被移到 reasoning/details 处理之后）：同一 delta 同时含 reasoning 与 content 时，thinking 块必须先创建。`[CHANGELOG 1.3.0 Fixed]` #10926（LiteLLM）。
2. **`supportsStrictMode` 默认改 `false`**（`v1.3.0/src/api/openai-completions.ts:1677`）：原为 `!isMoonshot && !isTogether && !isCloudflareAiGateway && !isNvidia`，现统一 `false`，由生成目录显式开启。`[CHANGELOG 0.87.0 Fixed]` #9816。
3. **`sendSessionAffinityHeaders: isOpenRouter`**（`:1682`）+ `sessionAffinityFormat: isOpenRouter ? "openrouter" : "openai"`（`:1683` 附近）：原为 `false` / `"openai"`。`[CHANGELOG 0.86.0 Fixed]` #9102。
4. **`resolveSamplingParams(model, options?.reasoningEffort ?? "off", options?.samplingParams)`**（`:1006`）：合并 `model.samplingParams` → `model.samplingParamsByThinkingLevel[level]` → 请求级。`[CHANGELOG 0.99.0 Fixed]` #9506（`model.samplingParams` 直连时被丢）、`[1.0.2 Added]` #9776（分层覆盖）。
5. **user 内容过滤空 text part**（`:1271`）：`.filter((item) => item.type !== "text" || item.text.length > 0)`。`[CHANGELOG 0.87.1 Fixed]` #9797（纯图消息被拒）。
6. **`onProviderStreamEvent` 钩子**（`:559` `await options?.onProviderStreamEvent?.(chunk, model)`）。
7. `params.stream_options` 去掉 `as any`（类型收紧，无语义）。

- **影响 pi-py**：`providers/openai_provider.py`（全部 7 项）、`types.py`（`sampling_params_by_thinking_level`、`on_provider_stream_event`）
- **迁移动作**：逐条对齐；第 2/3 项是**默认值反转**，务必同步。
- **工作量：M**

### M-14 `api/anthropic-messages.ts` 行为变更（多）

1. **strict 工具降级重试**（`v1.3.0/src/api/anthropic-messages.ts:648-690` 的 `for (;;)` 请求环 + `:684 disablePreferredStrictTools = true`）：捕获 400 `invalid_request_error` 且消息命中 `"The compiled grammar is too large"` / `"Too many strict tools ("` / `"Schemas contains too many parameters with union types ("` 时，`strict: "prefer"` 的工具降为非 strict 后**重试一次**，并写 `anthropic_strict_tools_fallback` 诊断（`hasPreferredStrictTools` 由 `convertToolDefinitions` 在 `:1182`/`:1198` 计算，经 `buildParams` 返回值 `:1331` 传出）。`[CHANGELOG 1.2.0 Fixed]` #10409。
2. **Anthropic strict 不支持关键字集**（`:1589-1600` `ANTHROPIC_STRICT_UNSUPPORTED_KEYWORDS`、`:1602-1612` `ANTHROPIC_STRICT_STRING_FORMATS`、`:1615` `isAnthropicStrictUnsupportedKeyword`、`:1637` 传入 `resolveJsonSchemaStrictSampling`）：`minimum` `maximum` `exclusiveMinimum` `exclusiveMaximum` `multipleOf` `maxItems` `uniqueItems` `minContains` `maxContains` `minProperties` `maxProperties`；`minItems` 仅 0/1 可；`format` 仅白名单（`date-time` `time` `date` `duration` `email` `hostname` `uri` `ipv4` `ipv6` `uuid`）。经 `UnsupportedStrictSchemaKeywordCheck`（`v1.3.0/src/api/constrained-sampling.ts:13`）注入 `constrained-sampling.ts`。`[CHANGELOG 0.99.2 Fixed]` #9953。
3. **`output.responseModel` 而非覆盖 `output.model`**（`:704-712`）：原 `output.model = event.message.model`，现 `if (responseModel !== model.id) output.responseModel = responseModel;`。`[CHANGELOG 0.86.0 Fixed]` #9188（relay 报不同 response model 破坏签名 thinking 回放）。
4. **`x-session-id` for OpenRouter**（`:1071-1076`）：`compat.sessionAffinityFormat === "openrouter"` 时用 `x-session-id`，否则 `x-session-affinity`。
5. **`thinking: { type: "disabled" }` 无 effort**（`:1205-1207` `thinkingDisabled`、`:1278-1281` 的 `params.thinking` 分支）：`thinkingDisabled = model.reasoning && options?.thinkingEnabled === false && model.thinkingLevelMap?.off !== null`，此时不发 effort marker。`[CHANGELOG 1.3.0 Fixed]` #10753。
6. **错误 tool result 带图拆分**（`:1509` 调用 `splitErrorToolResultImages`，定义在 `v1.3.0/src/api/transform-messages.ts:31-46`）。`[CHANGELOG 1.3.0 Fixed]` #10794。
7. **1h cache write 从流式 delta 读取**（`:881`，Vercel AI Gateway）。`[CHANGELOG 0.99.0 Fixed]` #9210。
8. **Claude Code stealth 版本** `2.1.251` → `2.1.280`（`:96`，旧 `:81`）。`[CHANGELOG 0.87.1 Fixed]`。
9. `PiAnthropic` 子类覆写 `_shouldResolveDefaultCredentials(): false`（`:339-341`，关掉 SDK 自带凭据链，避免绕过 pi 的 auth resolver）。
10. Anthropic workload identity federation（`getAnthropicFederation`，定义 `:353`，调用 `:614` / `:969`）：**NC**。

- **影响 pi-py**：`providers/anthropic_provider.py`、`constrained_sampling.py`
- **迁移动作**：第 1/2/3/4/5/6 项必跟（第 4/5 项属默认行为，第 1 项需要请求级重试环）。第 9 项 Python SDK 无对应。
- **工作量：L**（strict 降级重试需要把 `build_params` 抽成可重入 + 诊断写入）

### M-15 `model-catalog.ts` 导出重命名（v0.99.0）

- **Before**（`v0.85.1/src/model-catalog.ts:19-27`）：
  ```ts
  export type ModelCatalog<TGroups extends ModelGroups, TProvider extends ProviderId> = {...};
  export function flattenModelCatalog<...>(_provider, groups): ModelCatalog<TGroups, TProvider>
  ```
- **After**（`v1.3.0/src/model-catalog.ts:62-81`）：
  ```ts
  export type ChatModelCatalog<...>       // 原 ModelCatalog
  export type ImageModelCatalog<...>
  export type ClassifierModelCatalog<...>
  export function flattenChatModelCatalog<...>(_provider, groups): ChatModelCatalog<...>
  export function flattenImageModelCatalog<...>(...)
  export function flattenClassifierModelCatalog<...>(...)
  ```
  内部 `flattenModelCatalog(groups, type)` 变为私有（按 `model.type` 过滤）。
- **影响 pi-py**：若 pi-py 有 `model_catalog.py` 等价物（当前 `PORTING.md` 说"模型清单 `models.generated.ts` → 精简硬编码"，**未移植 catalogs**）→ 实际上 **不影响**。
- **迁移动作**：暂不动。若未来移植生成目录，按新命名。
- **工作量：S / 不适用**

### M-16 `providers/*.models.ts` 机械改造（38 个）

- **Before**（例 `v0.85.1/src/providers/anthropic.models.ts:5-8`）：
  ```ts
  import { flattenModelCatalog, type ModelCatalog } from "../model-catalog.ts";
  export const ANTHROPIC_MODELS: ModelCatalog<typeof values, "anthropic"> = flattenModelCatalog("anthropic", values);
  ```
- **After**（`v1.3.0/src/providers/anthropic.models.ts:5-14`）：额外导出 `*_IMAGE_MODELS` / `*_CLASSIFIER_MODELS`（各 3 行），并把 `ModelCatalog` → `ChatModelCatalog`、`flattenModelCatalog` → `flattenChatModelCatalog`。
- **影响 pi-py**：否（未移植）。**若移植，建议不要逐文件搬运，改为在 Python 侧建单一 `model_catalog.py` + 数据文件。**
- **工作量：S**

### M-17 `env-api-keys.ts` provider 键名与新增键

- **Before**（`v0.85.1/src/env-api-keys.ts:85`）：`"azure-openai-responses": "AZURE_OPENAI_API_KEY"`
- **After**（`v1.3.0/src/env-api-keys.ts:90`）：`azure: "AZURE_OPENAI_API_KEY"`；新增 `typesafe: "TYPESAFE_API_KEY"`、`meta: "META_API_KEY"`；新增 5 个 Anthropic federation 环境变量常量（`:32-36`）
- **影响 pi-py**：`types.py` `KnownProvider` 字面量（`types.py:28`、`:48` 含 `"azure-openai-responses"`）
- **迁移动作**：`KnownProvider` 中 `"azure-openai-responses"` 保留（**上游 `KnownApi` 里 `"azure-openai-responses"` 仍是 api id，只有 provider id 改名**——见 `v1.3.0/src/types.ts:65` 在 `KnownApi` 内、`:94` 在 `KnownProvider` 内），provider 侧新增 `"azure"`；新增 `"typesafe"` / `"meta"` 字面量仅类型对齐。
- **工作量：S**

### M-18 `models-store.ts`：存储条目可含任意模型类型

- **Before**（`v0.85.1/src/models-store.ts:4`）：`models: readonly Model<Api>[];`
- **After**（`v1.3.0/src/models-store.ts:5`）：`models: readonly AnyModel[];`
- **影响 pi-py**：未移植 models-store（pi-py 无动态目录）→ 不影响。
- **工作量：不适用**

### M-19 其它公开导出增删

| 项 | 位置 | 类型 |
|---|---|---|
| `index.ts` 移除 `export * from "./images-models.ts"` | `v0.85.1/src/index.ts:33` | 删除 |
| `index.ts` 移除 `export * from "./utils/assistant-message-frame.ts"` | `v0.85.1/src/index.ts:39` | 删除 |
| `index.ts` 新增 `getSystemMessageText` / `renderSystemMessageUpdate` 具名导出 | `v1.3.0/src/index.ts:43` | 新增 |
| `index.ts` 新增 `export * from "./utils/transcript.ts"` | `v1.3.0/src/index.ts:44` | 新增 |
| `utils/text.ts` 新增 `getSystemMessageText` / `renderSystemMessageUpdate` | `v1.3.0/src/utils/text.ts:15`、`:28` | 新增 |
| `utils/model-operations.ts` 新增 `getModelType` / `isModelType` / `assert*` / `*ErrorResult` | `v1.3.0/src/utils/model-operations.ts:18-80` | 新增 |
| `utils/models-error.ts` 抽出 `ModelsError` / `ModelsErrorCode` | `v1.3.0/src/utils/models-error.ts:3-13` | 移动（原 `auth/resolve.ts:16`/`:26`） |
| `auth/resolve.ts` 新增 `refreshStoredOAuthCredential` | `v1.3.0/src/auth/resolve.ts:119` | 新增 |
| `auth/types.ts` 新增 `LoginOptions`（`getDeviceId` / `agentName`），`OAuthAuth.login(interaction, options?)` 加第二参 | `v1.3.0/src/auth/types.ts:202-215`、`:231` | **签名扩展（可选参，兼容）** |
| `CompatSchemasByApi` / `ProviderCompatSchema` 新增 | `v1.3.0/src/providers/compat-schema.ts:603-619` | 新增（模型目录校验用） |
| `api/lazy.ts` setup 失败的时间戳改用请求开始时间 | `v1.3.0/src/api/lazy.ts:50`（`const startedAt = Date.now()`）、`:56`（`createSetupErrorMessage(model, error, startedAt)`） | 行为修正（`[CHANGELOG 1.1.0 Fixed]`："the error message of a failed lazy API setup … using its failure time as `timestamp` instead of the request start"） |

### M-20 `types.ts` 中 `Images*` → `Image*` 重命名族（v0.99.0 Breaking）

- **Before**（`v0.85.1/src/types.ts:31-33`、`:78-80`、`:289`、`:339`、`:874`）：
  ```ts
  export type KnownImagesApi = "openrouter-images";
  export type ImagesApi = KnownImagesApi | (string & {});
  export type KnownImagesProvider = "openrouter";
  export type ImagesProviderId = KnownImagesProvider | string;
  export interface ProviderImages { generateImages(model: ImagesModel<ImagesApi>, ...): Promise<AssistantImages> }
  export type ImagesFunction<TApi extends ImagesApi = ImagesApi, TOptions extends ImagesOptions = ImagesOptions> = (...)
  export interface ImagesModel<TApi extends ImagesApi> extends Omit<Model<Api>, "api"|"provider"|"reasoning"|"contextWindow"|"maxTokens"|"compat"> { ... }
  ```
- **After**（`v1.3.0/src/types.ts:75-77`、`:332-338`、`:358`、`:396`、`:875-879`）：
  ```ts
  export type KnownImageApi = "openrouter-images";
  export type ImageApi = KnownImageApi | (string & {});
  // KnownImagesProvider / ImagesProviderId 已删除；AssistantImages.provider: ProviderId
  export interface ProviderImages { generateImages(model: ImageModel<ImageApi>, ...): Promise<AssistantImages> }
  export type ImagesFunction<TOptions extends ImagesOptions = ImagesOptions> = (model: ImageModel<ImageApi>, ...)
  export interface ImagesOptions extends ProviderRequestOptions<ImageModel<ImageApi>> { metadata?: ... }
  export interface ImageModel<TApi extends ImageApi> extends BaseModel<TApi> { type: "image"; output: ("text"|"image")[] }
  ```
- **`[CHANGELOG 0.99.0 Breaking]`** 原文见 1.3 节。
- **影响 pi-py**：pi-py 未移植 images（`PORTING.md` 标 🟡 后续）→ 本轮**不影响**。**若将来移植，直接用新命名，不要引入 `ImagesModel`。**
- **工作量：不适用**

### M-21 `Model` 拆分出 `BaseModel`（v0.99.0）

- **Before**（`v0.85.1/src/types.ts:843`）：单一 `Model<TApi>`，含 `id/name/api/provider/baseUrl/reasoning/thinkingLevelMap/input/cost/contextWindow/maxTokens/samplingParams/headers/compat`
- **After**（`v1.3.0/src/types.ts:825-898`）：
  ```ts
  export interface BaseModel<TApi extends string> {          // :825
      id; name; api; provider; baseUrl;
      input: ModelInputModality[];
      inputLimits?: ModelInputLimits;
      cost: ModelCost;
      headers?: Record<string, string>;
  }
  export interface Model<TApi extends Api> extends BaseModel<TApi> {   // :839
      type?: "chat";
      reasoning: boolean;
      thinkingLevelMap?: ThinkingLevelMap;
      promptCache?: ModelPromptCache;
      contextWindow: number;
      maxTokens: number;
      samplingParams?: SamplingParams;
      samplingParamsByThinkingLevel?: SamplingParamsByThinkingLevel;
      compat?: ... | MistralConversationsCompat | never;      // 新增 mistral 分支
  }
  export interface ImageModel<TApi extends ImageApi> extends BaseModel<TApi> { type: "image"; output: (...)[] }        // :875
  export interface ClassifierModel<TApi extends ClassifierApi> extends BaseModel<TApi> { type: "classifier"; contextWindow: number }  // :882
  export interface ModelTypeMap { chat; image; classifier }   // :888
  export type ModelType = keyof ModelTypeMap;                  // :895
  export type AnyModel = ModelTypeMap[ModelType];              // :898
  ```
- **影响 pi-py**：`types.py:359 Model`。**对纯 chat 用途是向后兼容的**（`type` 可省），但要加 `input_limits` / `prompt_cache` / `sampling_params_by_thinking_level`。
- **迁移动作**：加可选字段；若引入 image/classifier 则再拆 `BaseModel`。
- **工作量：S**

### M-22 `StreamOptions.onProviderStreamEvent`（v0.99.0 Added）

- **After**（`v1.3.0/src/types.ts:223`）：
  ```ts
  onProviderStreamEvent?: (data: unknown, model: Model<Api>) => void | Promise<void>;
  ```
  且在 `simple-options.ts:57` 由 `buildBaseOptions` 透传；适配器侧 `openai-completions.ts:559`、`anthropic-messages.ts:700` 调用。
- **影响 pi-py**：`types.py:391 StreamOptions`、`providers/*.py`、`api/simple-options` 等价物
- **迁移动作**：加字段 + 在两个 provider 的流循环首行调用（`data` 为 provider 原始解析事件）。
- **工作量：S**

---

## 4. 越界判断：不属于「统一 LLM API」范畴的新增

以下模块**技术上在 `packages/ai` 内**，但语义上超出「统一 LLM API」的边界，需要用户拍板是否纳入 pi-py：

| # | 模块/能力 | 为什么越界 | 建议 |
|---|---|---|---|
| 1 | `auth/oauth/openai-chatgpt.ts`（Sign in with ChatGPT）、`auth/oauth/meta.ts`（Muse 订阅 OAuth）、`auth/oauth/callback-server.ts`（浏览器回调服务器 + HTML 页面） | 这是**交互式登录流程 + 本地 HTTP 回调服务器 + 浏览器页面渲染**，属于「凭据获取的用户界面」，不是 LLM 调用协议。`callback-server.ts` 还引入本地端口绑定、HTML 生成 | **不纳入本轮**；若 pi-py 需要，另立「pi-ai-auth」子模块并单独排期 |
| 2 | Anthropic workload identity federation（`env-api-keys.ts:32-36` 五个常量 + `providers/anthropic.ts:49-69` 环境探测 + `api/anthropic-messages.ts:353` `getAnthropicFederation`，调用点 `:614`/`:969`；`api/anthropic-messages.ts:339-341` 的 `PiAnthropic` 子类） | 依赖 `@anthropic-ai/sdk` 的 `config.authentication` 与 token 缓存/`withOptions()` 克隆机制；Python SDK 无对等能力 | **不纳入**，或降级为「透传 env，由用户自备 client」 |
| 3 | `personalizeOpenRouterCatalog()`（`providers/openrouter.ts:24-60`；`RefreshModelsContext.baseUrl` 见 `models.ts:93-101`） | **模型目录刷新**：直接 `fetch` OpenRouter `GET /models/user`、覆盖 contextWindow/maxTokens/cost。属于「目录管理」而非「调用协议」 | 归 NEW-CAPABILITY 的独立子项，**用户决策** |
| 4 | `Models` 门面中的目录/持久化相关：`refreshModels()`、`checkAuth()`、`getAvailable*()`、`ModelsStore`（`models-store.ts:5`） | 上游把「目录刷新 + 凭据状态 + 可用性过滤」编进了统一面（`models.ts:255` `interface Models`）。pi-py 目前完全没有这层 | **需要决策**：是补齐 `Models` 门面（含目录/鉴权），还是保持扁平注册表并只做调用面 |
| 5 | classifier 全家桶（4 个 API 实现 + `ClassifierModel`/`ClassifierContext`/`ClassifierResult`） | 严格说 classifier 是「第二次 LLM 调用」，与 chat 共用 auth/cost/retry。但 459 行的 `llama-cpp-classify.ts` 依赖 llama-server 的 next-token logprob 语义 | 归 NEW-CAPABILITY；**若不做 JEV/router 场景可整体跳过** |
| 6 | `image models` 并入统一面（`ImageModel` / `ProviderImages` / `Models.generateImages()` / `models.generated.ts` 合并） | 图像生成与 chat 的请求/响应结构完全不同，只是共享 auth 与 cost | 归 NEW-CAPABILITY；pi-py 已标 🟡 后续 |
| 7 | `src/cli.ts`（dev OAuth CLI）、`src/bun-oauth.ts`（Bun bundling glue） | CLI/运行时胶水 | **IRRELEVANT** |
| 8 | `src/providers/compat-schema.ts` / `model-schema.ts` 的 typebox-first 形态 | pi-py 用 Pydantic，schema-first 的**动机**（发布 JSON Schema 给编辑器校验）在 Python 侧收益不同 | **不照搬形态**，只取字段集 |

---

## 5. 工作量估算（MUST-PORT 项）

分级口径：**S** = 单文件/纯常量或正则，可在一个改动批次内完成；**M** = 跨 2-3 个模块，需要新函数或新小模块 + 测试；**L** = 需要结构调整（重入请求环、新抽象层），有回归风险。

| 项 | 分级 | 理由 |
|---|---|---|
| M-2 `SystemMessage` 类型 | **S** | 纯 Pydantic 模型 + 联合扩展；无逻辑分支 |
| M-3 `ToolCall.argumentsError` | **S** | 加一个可选字段 |
| M-5 `json-parse` 转义集 | **S** | 常量 + 一个回归测试；风险在 `json-repair` 库行为不可控，可能要写适配 |
| M-6 跨模型 thinking 丢弃 | **S** | 条件是反转，代码量小；但要改 2 个 provider 的转换分支 |
| M-7 `providerHeadersToRecord` | **S** | 变参 + 小写折叠，10 行内 |
| M-8 `retry` 封顶 + 分类 | **S** | 正则集 + 一个封顶函数；**但默认行为变更需在 PORTING.md 标注** |
| M-9 `provider-retry` isfinite | **S** | 两个守卫 + 新参数 |
| M-10 `estimate` 3.5 | **S** | 常量 + 签名放宽 |
| M-22 `onProviderStreamEvent` | **S** | 字段 + 2 处调用 |
| M-17 `env-api-keys` / `KnownProvider` | **S** | 字面量与常量表 |
| M-11 `event-stream` | **S/M** | FifoQueue + `durationMs` 注入；pi-py 用 `asyncio.Queue`（PORTING.md 已声明偏离），性能项可能已天然规避，主要成本是 `duration_ms` 写入语义 |
| M-13 `openai-completions` 行为 | **M** | 7 个子项，含 2 处默认值反转；需逐条回归 |
| M-21 `Model` → `BaseModel` 可选字段 | **M** | 需同时加 `inputLimits`/`promptCache`/`samplingParamsByThinkingLevel` 及其解析 |
| M-4 坏 JSON 参数 | **M** | 新函数 + 2 个 provider 收尾 + 与 M-3 联动；且要区分「流式增量」与「收尾」两条路径 |
| M-12 deferred tools 替换 | **M** | 取决于 pi-py 是否移植过 `addedToolNames`（见 Q3）；若已移植则必须与 M-1 同步 |
| M-1 `TranscriptContext` + `transcript.py` | **M** | 新模块 237 行等价物 + Protocol 签名 + 入口归一化 + 2 个 provider 改读取点；**但不涉及 mid-convo 回放逻辑时可控** |
| M-14 `anthropic-messages` 行为 | **L** | strict 降级重试要求 `build_params` 可重入 + 请求级重试环 + 诊断写入；另含 strict 关键字集、responseModel、session-affinity 格式、thinking disabled 等 6 项必跟子项。是单项里最大的一块 |

**汇总**：S 级 11 项、M 级 5 项、L 级 1 项。

**建议实施顺序（依赖驱动）**：
1. **批次 A（低风险高收益，可立即做）**：M-2、M-3、M-5、M-7、M-8、M-9、M-10、M-17、M-22 → 纯行为/常量对齐
2. **批次 B（provider 行为）**：M-6 → M-13 → M-11
3. **批次 C（依赖 A/B）**：M-4（依赖 M-3）、M-21
4. **批次 D（结构性，需要先决策 Q2/Q3）**：M-1、M-12
5. **批次 E（最大风险，单独排期）**：M-14

---

## 6. 上游测试文件作为行为规格

上游新增了约 40 个测试文件，**其中多个测试与本文的 MUST-PORT 项一一对应**，可直接作为 pi-py 的验收用例来源（读测试比读 release notes 精确得多）：

| 上游测试 | 覆盖的 MUST-PORT |
|---|---|
| `test/json-parse.test.ts`（新增） | M-5 |
| `test/event-stream.test.ts`（新增） | M-11 |
| `test/transform-messages-cross-model-thinking.test.ts`（新增） | M-6 |
| `test/system-message-replay.test.ts`（新增） | M-1 / M-12 |
| `test/transcript-tool-changes.test.ts`（新增） | M-12 |
| `test/anthropic-strict-tool-fallback.test.ts`、`test/anthropic-strict-tool-schema.test.ts`（新增） | M-14 (1)(2) |
| `test/anthropic-error-tool-result-images.test.ts`（新增） | M-14 (6) |
| `test/openai-completions-provider-stream-event.test.ts`（新增） | M-22 |
| `test/openai-responses-mid-convo-effort.test.ts`、`test/openai-responses-usage-limit.test.ts`（新增） | M-13 / NC |
| `test/model-cost-tiers.test.ts`（新增） | 定价分层（见 Q4） |
| `test/message-types.test.ts`、`test/model-types.test.ts`（新增） | M-2 / M-21 |
| `test/oauth-callback-server.test.ts`、`test/openai-chatgpt-oauth.test.ts`、`test/meta-oauth.test.ts`（新增） | NC（OAuth） |
| `test/classifier-models.test.ts`、`test/llama-cpp-classify.test.ts`、`test/typesafe-system-one.test.ts`、`test/openai-decisions.test.ts`、`test/cloudflare-workers-ai-system-one.test.ts`（新增） | NC（classifier） |
| `test/bedrock-cache-write-1h-cost.test.ts`、`test/opencode-provider-headers.test.ts`、`test/radius-provider.test.ts`、`test/transcript-tool-changes.test.ts` 等 | IR（本轮） |

复核命令：

```bash
cd /tmp/pi-up
diff <(find v0.85.1/packages/ai/test -type f | sed 's|.*/||' | sort) \
     <(find v1.3.0/packages/ai/test -type f | sed 's|.*/||' | sort) | grep '^>'
```

---

## 7. 证据附录（可复现命令）

```bash
# 1) 权威 added/removed/modified 清单
cd /tmp/pi-up
diff -rq v0.85.1/packages/ai/src v1.3.0/packages/ai/src | sort

# 2) 文件数自洽
find v0.85.1/packages/ai/src -type f | wc -l    # 178
find v1.3.0/packages/ai/src -type f | wc -l     # 199

# 3) 单文件 diffstat（判断机械改造 vs 实质改造）
for f in $(diff -rq v0.85.1/packages/ai/src v1.3.0/packages/ai/src | grep '^Files' | sed 's/^Files //; s/ and .*//'); do
  echo "$(diff "$f" "${f/v0.85.1/v1.3.0}" | grep -c '^[<>]') $f"; done | sort -rn

# 4) 包级 CHANGELOG（比 monorepo release notes 更精确）
sed -n '1,290p' v1.3.0/packages/ai/CHANGELOG.md

# 5) 确认移除模块无替代
grep -rn "deferred-tools\|assistant-message-frame" v1.3.0/packages/ai/src/   # 无输出

# 6) pi-py 现状对照
grep -n 'azure\|KnownProvider' /Users/mac/pi-py/packages/pi-ai/src/pi_ai/types.py
grep -n 'StopReason' /Users/mac/pi-py/packages/pi-ai/src/pi_ai/types.py
```

**关于 release notes**：任务给出的 `gh api repos/earendil-works/pi/releases/tags/<v>` 返回的是 **monorepo 级** 发布说明（含 coding-agent / TUI 大量条目），对 `packages/ai` 的覆盖不完整且混入无关内容。本文改用包级 `CHANGELOG.md`（`v1.3.0/packages/ai/CHANGELOG.md:3-277` 覆盖 1.3.0 → 0.86.0）作为交叉印证，monorepo release notes 仅用于确认 `[CHANGELOG]` 未记录的用户可见描述。**所有结论均以源码 diff 为准。**

---

## 8. 开放问题（无法判断 / 需要用户拍板）

| # | 问题 | 为什么需要拍板 |
|---|---|---|
| **Q1** | **模型目录数据缺失**：`providers/data/*.json` 不在提供的源码树中，因此无法盘点"新增/移除/改价了哪些具体模型"。是否需要补拉数据（走 `gh api` 取 `src/providers/data/*.json` 或生成的 `models.json`）？ | 这直接影响 `PORTING.md` 中"模型清单 → 精简硬编码 gpt-4o 系列"这条**有意偏离**是否还成立。若不补，pi-py 会持续落后于厂商模型；若补，需要一套数据化方案（本轮工作量未计入） |
| **Q2** | **pi-py 是否要补 `Models` / `Provider` 门面？** 上游 v0.85.1 起已是 `Models` + `Provider` 对象；pi-py 仍是扁平注册表（`models.py:48-100`）。本轮所有新能力（classifier / images / *OfType / filterAllModels）都挂在 `Provider` 接口上 | 这是架构级分叉：不补 → 每项新能力都要单独接线且与上游持续漂移；补 → 一次结构性重构，影响 `pi-agent-core` 等下游包的调用方式。**建议在本轮同步前决定** |
| **Q3** | **pi-py 是否移植过 `ToolResultMessage.addedToolNames`（deferred tools）？** `PORTING.md` 未提及，`types.py:270` 的 `ToolResultMessage` 需实测确认 | 决定 M-12 是 S 还是 M 级；也决定 M-1 是否需要一并处理 tool 变更语义 |
| **Q4** | **定价分层（`ModelCostTier` / `inputTokensAbove`）**：`ModelCostTier` 在 v0.85.1 已存在，但上游在 1.1.0 修复了"长 prompt 按短档计价"（`[CHANGELOG 1.1.0 Fixed]`，涉及 Haiku 5.5 / Gemini 3.1 Pro / GPT-5.4），并新增 `test/model-cost-tiers.test.ts`。pi-py 的 cost 计算是否实现了 tiers？ | 若无 tier 逻辑，接错价会静默少算成本。需确认是否纳入本轮 |
| **Q5** | **`StopReason` 缺 `"deferred"`**：上游 v0.85.1 起即含 `"deferred"`（`v1.3.0/src/types.ts:474`），pi-py `types.py:112` 只有 6 个值 | 这是**既有偏离**（`PORTING.md` 已声明 deferred 未移植），但会让类型对齐检查持续报警。本轮是否顺手补上（补了就要处理 `DeferredHandle` / `fetchDeferred` / `cancelDeferred`）？ |
| **Q6** | **`typebox` → Pydantic 的 schema-first 形态**：上游把 compat/model 类型改成 typebox-first（`providers/compat-schema.ts` 641 行、`providers/model-schema.ts` 96 行）以发布 JSON Schema。pi-py 用 Pydantic，是否需要同等的"发布 Schema"能力？ | 决定是否照搬 schema-first 结构，还是只取字段集合并保持 Pydantic 惯例 |
| **Q7** | **Anthropic `strict` 降级重试的语义归属**：上游把"400 → 降级 → 重试一次"放在 `api/anthropic-messages.ts` 内（provider 层重试环），而 pi-py 的 `PORTING.md:43` 声明"provider 内部不抛异常；重试是外层关注点"。这两条原则现在冲突 | 需要用户决定：在 provider 内开一个例外，还是把降级信息以事件形式抛出由外层重试？后者更符合 pi-py 现有架构但改动更大 |
| **Q8** | **Sign in with ChatGPT 的 `deviceId` 持久化**：上游 `LoginOptions.getDeviceId()` 要求"应用自己持久化并在后续调用返回同一 ID"，`cli.ts:51` 明确"dev CLI 不持久化安装 ID"。pi-py 若移植 OAuth 需定义 ID 存放位置 | 越界项（§4 #1），但若决定纳入则需产品决策 |
| **Q9** | **`@anthropic-ai/sdk` / `openai` 大版本跳跃的连带影响**：`openai` 6.40.0 → 7.19.0、`@anthropic-ai/sdk` 0.123.0 → 0.129.0。pi-py 用 Python SDK（`openai` / `anthropic`），版本不对等 | 需确认 Python 侧 SDK 版本能否表达上游新增的请求字段（如 Anthropic `tool_addition` / `tool_removal` / `output_config.effort`）；`PORTING.md:88-90` 已记录过同类问题（anthropic SDK 0.117.1 无法解析 `fallback` 块） |

---

## 9. 与 `PORTING.md` 现有记载的差异

发现两处 `PORTING.md` 需要更新（**本任务未改动该文件**，仅记录）：

1. `PORTING.md:83-88`「未移植」段落里提到 `assistant-message-frame.ts`（harness v3 的帧 reducer）——该文件在 v1.3.0 已**整体删除**（`[CHANGELOG 1.3.0 Breaking]`），不再是"未移植"，而是"上游已移除"。
2. `PORTING.md:20-22` 把 `auth（OAuth）` / `images（图像生成）` 标为「🟡 后续」。上游已把 images 并入统一 `Provider`/`Models` 面，OAuth 新增两个流程 + 统一 callback server。这两项的追赶成本结构已变化，建议在下一轮排期时重估。

---

*报告生成：2026-10-18。所有 `文件:行号` 引用基于 `/tmp/pi-up/v0.85.1/packages/ai/` 与 `/tmp/pi-up/v1.3.0/packages/ai/` 两棵只读源码树。*

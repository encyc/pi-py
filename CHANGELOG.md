# 变更日志

本项目追踪上游 [earendil-works/pi](https://github.com/earendil-works/pi)，
版本号与上游保持一致。

本项目在 `CHANGELOG.md` 中保留公共的、用户可见的变更记录。
具体的提交记录参见 [GitHub Releases](https://github.com/earendil-works/pi-py/releases)。

## 1.3.0 (2026-10-19)

对齐上游 v1.3.0。这是一次**包结构重组**：上游在 1.0 之后删除了 `server`/`protocol`/`client`，
把 `session-backends` 换成了 `durable`，并新增了 `mcp`/`codemode`/`env`。

### 新增包

- **`pi-durable`** —— 上游 `@earendil-works/pi-durable` 的移植：
  可移植执行环境（fs/shell/watch，含上游 `env-conformance` 可执行规格）、
  会话/条目/存储（内存 + SQLite 两后端，上游 `storage-conformance` 24/24）、
  以及 `harness/` 全套（文档/任务/调度器，含 63 方法的 `TaskScheduler`
  与 `ToolTask`/`GenerationTask`/`CompactionTask` 三个任务定义）。
- **`pi-mcp`** —— 上游 `@earendil-works/pi-mcp`：MCP 客户端（stdio 子进程 +
  Streamable HTTP/SSE）与 OAuth 全套（含回调服务器的 S1–S12 安全边界）。
- **`pi-codemode`** —— 上游 `@earendil-works/pi-codemode`：JS 沙箱，用 wasmtime
  加载与上游**同一份** `quickjs.wasm`（sha256 断言），prelude 原文装载。
- **`pi-chord`** —— 上游 `@earendil-works/chord` 的**消费侧**（上下文、JSON diff、
  输出追踪）。上游 host 半边（8,117 行）在本版本无任何消费者，未移植。

### 移除

- **`pi-server` 下线**：上游 `@earendil-works/pi-server`（`packages/server`）已在
  v1.2.0 被删除（连同 `protocol`/`client`，无替代实现），本包失去对应的上游包，
  停止维护与发布。`pi-py-server` 不再出现在后续版本的 PyPI 发布中；已发布的历史
  版本（0.83.0 / 0.84.1 / 0.85.1）仍保留在 PyPI，源码可通过
  `git log -- packages/pi-server` 追溯。
- **`pi-storage-sqlite` 并入 `pi-durable`**：上游对应包已随 `session-backends`
  一起删除，且旧数据模型在新模型里没有对应物。本包保留为**转发壳**：新 API 从
  `pi_durable.storage.sqlite` 转发（同一对象），5 个旧名字
  （`SqliteSessionRepo` 等）明确抛 `AttributeError` 并给出迁移路径 ——
  「看起来能用但语义不同」的名字比直接删掉更危险。

### 行为对齐（上游 v1.0 → v1.3.0）

- **`pi-agent-core`**：`harness/` 更名为 `sessions/`；新增 `finishTurn`/
  `prepareRequest` 等 agent turn API。
- **`pi-ai`**：15 项 MUST-PORT 行为对齐；新增模型类型族
  （`ImageModel`/`ClassifierModel`/`AnyModel` 等）、`uuidv7`、`utils/overflow`、
  `utils/validation`、`Models` 类型面。
- **`pi-coding-agent`**：Tier-A 对齐 + 内置扩展三件套
  （`tool_search`/`mcp`/`codemode`，三者均可从包入口导入工厂）。

### 版本号统一

- 此前各包版本号不一致：`pi-ai`/`pi-agent-core` 的 `pyproject` 是 `0.85.1`
  而 `__version__` 是 `1.3.0`；`pi-chord`/`pi-durable` 等是 `1.3.0`；
  `pi-coding-agent`/`pi-storage-sqlite` 是 `0.85.1`。依赖上界也因此失真。
- 现统一为 **`1.3.0`**，并恢复约定「**包版本号 == 镜像的上游版本**」。
  新增 `tests/test_version_lockstep.py` 在**每次提交**校验三件事：
  所有包版本锁步、`__version__` 与包元数据一致、`__upstream_ref__` 与版本号一致。

### 发布基建

- 新增 `release-guard` 工作流：**tag 存在但没有对应 GitHub Release 时 CI 报错**
  （v0.85.1 漏发到 PyPI 的成因，此前无任何检查）。
- `publish.yml` 加固：发布前校验 tag 存在、`packages/*` 版本与 tag lockstep 一致、
  `CHANGELOG.md` 有对应小节；构建改为 `uv build --all-packages`（新增包不会因为
  忘记加进列表而漏发）；发布后回读 PyPI，要求每个包的 latest 等于本次 tag
  （防止 `skip-existing` 掩盖「其实没传上去」）。
- 补发通道改为显式指定 tag（`workflow_dispatch` 的 `tag` 输入必填并检出该 tag），
  修复此前手动触发会检出 `main` HEAD、构建出与 tag 不符的包的问题。
- 修复补发**历史** tag 会失败的问题：校验脚本与被校验内容改为分别检出
  （脚本取默认分支、内容取 tag）—— 历史 tag 上通常没有 `.github/scripts/`。
- **补发 v0.85.1**：该版本此前只推了 tag、没建 Release，因此从未发到 PyPI
  （PyPI 上一度停在 0.84.1）。本次已补发并创建对应 Release。

## 0.85.1 (2026-09-07)

对齐上游 v0.85.1（minor 0.85 + patch 0.85.1）。

### pi-ai

- OpenAI-compatible 适配器支持 ``reasoning_details`` 结构化回放（OpenRouter 等）：
  流式期间收集增量（连续同类合并、加密条目独立）且不发流式事件，块结束时一次性
  序列化进 thinking 签名；多轮对话时回放为 assistant 消息的 ``reasoning_details``
  字段（含遗留工具调用 ``thoughtSignature`` 加密条目的回退路径）。
- ``compat.thinkingTokenBudgetField`` 泛化封顶 reasoning 的顶层字段名
  （``thinking_budget`` Qwen/DashScope/SGLang、``thinking_budget_tokens`` llama.cpp）；
  遗留布尔 ``supportsThinkingTokenBudget`` 等价于 ``thinking_token_budget``（vLLM）。
- 新增 strict JSON Schema 转换：strict 工具发送 strict 化 schema（可选属性包
  ``anyOf``+null、全部属性 required、``additionalProperties: false``）；schema
  无法 strict 化时自动回退普通工具（``strict="require"`` 时报错）。
- ``SimpleStreamOptions.toolChoice``（``"auto"/"none"``）：provider 中性的工具
  选择，OpenAI 透传字符串、Anthropic 映射为 ``{"type": ...}``。
- Anthropic 适配器支持 server-side fallback（``compat.allowedFallbackModels``）：
  响应模型切换时按 fallback 本地费率计费，``fallback`` 声明块自动跳过。
- Anthropic 适配器支持 mid-conversation effort
  （``compat.supportsMidConvoEffort``）：adaptive thinking + 按轮 effort system
  消息 + ``providerThinkingLevel`` 记录。
- ``compat.vllmPriority`` 注入顶层 ``priority`` 请求字段（vLLM 调度优先级）。
- usage 解析补顶层 ``cached_tokens`` 回退（Kimi）；retry 分类新增
  ``exceeded request buffer limit while retrying upstream``；所有 provider 请求
  统一携带 pi User-Agent。
- 类型对齐：``AssistantMessage.providerThinkingLevel``/``endTurn``、
  ``ToolCall.namespace``、``ToolChoice``/``ThinkingTokenBudgetField``。

### pi-agent-core

- ``prepare_next_turn`` 钩子正式接入 agent loop（v0.85.1 时机语义）：在
  turn_end 后、下一轮 turn_start 前执行，仅当循环继续时调用，可替换
  context/model/thinking level，并在准备后补拉 steering 消息。
- 工具准备阶段完成后被取消的调用不再执行，产出 ``"Operation aborted"``
  错误结果（覆盖并行批次内尚未轮到的调用）。
- ``agent_loop_continue`` 路径补发首个 ``turn_start`` 事件。
- skills：根目录普通 ``.md`` 文件必须带非空 ``description`` frontmatter 才算
  技能，否则静默跳过。

### pi-coding-agent

- bash 工具：被信号杀死的进程映射为 ``128 + 信号号`` 退出码（如 SIGKILL →
  137），不再暴露 asyncio 负值、避免误判为成功。

### 其它

- ``pi-storage-sqlite`` / ``pi-server``：上游本轮改动（harness v3 / durable
  drive / 远程 worker 体系）不在本端精简实现范围内，仅同步版本与依赖约束。
- 五个 Python 包统一升级到 0.85.1，内部依赖范围更新为 ``>=0.85.1,<0.86``。

## 0.84.1 (2026-08-12)

对齐上游 v0.84.1（破例同步 patch：跨 minor 0.83→0.84 并叠加 0.84.1 patch）。

### pi-ai

- OpenAI-compatible 适配器支持 ``compat.supportsFinishReason=False`` 的兼容端点：流结束未见 ``finish_reason`` 时按内容推断 ``stop``/``toolUse``，不再误报错误。
- 新增泛型采样参数 ``StreamOptions.sampling_params`` / ``Model.sampling_params``，由 OpenAI-compatible 适配器在具名字段之后合并（请求级按 key 覆盖模型级，并覆盖具名字段）。
- 新增 ``thinking_token_budget``（vLLM 等）：``compat.supportsThinkingTokenBudget=True`` 且有 reasoning 时注入顶层预算，保证为答案留出 token 空间。
- Anthropic 适配器保留 ``content_block_start`` 携带的初始 text/thinking/signature。
- ``KnownProvider`` 补 ``baseten``、``qwen-token-plan-individual``（仅类型对齐）。

### pi-agent-core

- ``should_stop_after_turn`` 钩子接入 agent loop 并在 ``Agent`` / ``AgentOptions`` 上公开。
- ``before_tool_call`` 返回 ``block + terminate`` 时被 block 的工具调用可终止后续轮次。
- ``Agent.reset()`` 在活跃运行期间拒绝并抛错。

### 其它

- ``pi-storage-sqlite`` / ``pi-coding-agent`` / ``pi-server``：上游本轮改动不在本端精简实现范围内，无运行时行为变更，仅同步版本与依赖约束。
- 五个 Python 包统一升级到 0.84.1，内部依赖范围更新为 ``>=0.84.1,<0.85``。
- deferred/background 响应模式、telemetry、harness v2、CLI/TUI、新 provider 等上游能力仍在裁剪范围之外。

## 0.83.0 (2026-07-30)

对齐上游 v0.83.0。

### 新增

- 流式消息增加 `pending` 中间状态和 `rawStopReason` 原始终止原因。
- OpenAI/Anthropic provider 支持按请求注入异步 HTTP client。

### 修复

- 缺失或未知 provider 终止原因不再被误判为成功。
- 修复 OpenAI-compatible 工具增量同时含有效 `function` 和空 `custom`
  时丢失参数的问题。

### 维护

- 五个 Python 包统一升级到 0.83.0，内部依赖范围更新为 `<0.84`。
- OAuth、CLI/TUI、扩展系统、TypeBox 和未支持 provider 继续保持裁剪。

## 0.82.1 (2026-07-27)

对齐上游 v0.82.1。

### 新增

- `Tool.constrainedSampling`：支持 strict JSON Schema 与 OpenAI Lark/regex grammar 工具。
- OpenAI 与 Anthropic provider 请求级可取消重试，并遵循服务端 retry headers。

### 修复

- DNS `getaddrinfo`、`ENOTFOUND`、`EAI_AGAIN` 传输失败可触发 assistant 重试。
- Compaction 摘要请求使用独立 routing session，并禁用 prompt-cache 写入。

### 维护

- 五个 Python 包统一升级到 0.82.1，内部依赖范围更新为 `<0.83`。

## 0.81.1 (2026-07-22) — 初始基线

首次发布，对齐上游 v0.81.1。

### 包

- **pi-ai**：统一 LLM API 抽象层
  - OpenAI Chat Completions provider（流式文本 + 工具调用 + usage + 容错 JSON 解析）
  - Anthropic Messages provider（流式 + thinking 双模式 + signature 累加）
  - retry 工具（双正则错误分类 + 纯指数退避）
  - Faux 测试 provider
  - Model registry + API provider 分发
- **pi-agent-core**：agent 运行时
  - agent_loop 双层循环引擎（follow-up + tool calls + steering）
  - 有状态 Agent 封装（prompt/continue_/steer/follow_up/abort）
  - harness/skills：两阶段发现算法 + XML system prompt 格式化
  - harness/session：内存 + JSONL 存储 + fork + compaction 展开
  - harness/compaction：token 估算 + cut point + LLM 摘要
- **pi-storage-sqlite**：SQLite 会话后端
  - 7 表 schema + migration + PRAGMA 配置
  - SessionStorage 协议实现 + 多 session repo
- **pi-coding-agent**：编码 agent SDK（core only，不含 TUI/CLI）
  - bash / read / edit / write / grep / find / ls 7 个工具
  - truncate_head/tail/line 截断
  - CodingAgent SDK 封装
- **pi-server**：agent 服务化
  - Unix socket + JSONL 协议
  - Supervisor 进程内实例管理（spawn/stop/list/RPC）
  - rpc_stream 事件流双向通道

### 真实验证

- DeepSeek：pi-ai 流式 + 工具调用，pi-agent-core agent 循环 + 工具执行，compaction 摘要，pi-coding-agent 读/写/编辑/执行
- 191 单元测试 + 14 集成测试

### 已知限制

- Anthropic provider 未做真实 API 调用验证（无 API key）
- pi-server 用 mock agent 测试，无真实 LLM 集成

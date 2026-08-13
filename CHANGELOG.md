# 变更日志

本项目追踪上游 [earendil-works/pi](https://github.com/earendil-works/pi)，
版本号与上游保持一致。

本项目在 `CHANGELOG.md` 中保留公共的、用户可见的变更记录。
具体的提交记录参见 [GitHub Releases](https://github.com/earendil-works/pi-py/releases)。

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

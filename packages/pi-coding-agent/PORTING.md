# pi-coding-agent 移植注记

对应上游：[`@earendil-works/pi-coding-agent`](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)

- **声明基线**：v0.85.1（与根目录 `UPSTREAM_VERSION` 一致）
- **已部分对齐**：v1.3.0 的 SDK(core) 面 —— 见下方「v1.3.0 Tier-A 对齐」小节

> ⚠️ **本文件此前描述的覆盖度与代码严重不符**（声称 `src/modes/rpc/`、`src/extensions/`、
> `src/utils/` 已复刻，实际 0 行）。2026-10-18 已按源码实测修正，请以本版为准。

## 本包实际规模（实测，2026-10-18）

| 文件 | LOC |
|---|---:|
| `src/pi_coding_agent/tools.py` | 1016 |
| `src/pi_coding_agent/truncate.py` | 268 |
| `src/pi_coding_agent/sdk.py` | 139 |
| `src/pi_coding_agent/__init__.py` | 96 |
| `src/pi_coding_agent/render_utils.py` | 68 |
| `src/pi_coding_agent/extensions/types.py` | 233 |
| `src/pi_coding_agent/extensions/registry.py` | 190 |
| `src/pi_coding_agent/extensions/tool_search/tool.py` | 274 |
| `src/pi_coding_agent/extensions/tool_search/__init__.py` | 55 |
| `src/pi_coding_agent/extensions/__init__.py` | 51 |
| `src/pi_coding_agent/mcp_servers.py` | 397 |
| `src/pi_coding_agent/resolve_config_value.py` | 244 |
| `src/pi_coding_agent/auth_storage.py` | 283 |
| `src/pi_coding_agent/model_registry.py` | 60 |
| `src/pi_coding_agent/nested_tool_calls.py` | 305 |
| `src/pi_coding_agent/extensions/mcp/config.py` | 305 |
| `src/pi_coding_agent/extensions/mcp/log.py` | 85 |
| `src/pi_coding_agent/extensions/mcp/tools.py` | 438 |
| `src/pi_coding_agent/extensions/mcp/resources.py` | 400 |
| `src/pi_coding_agent/extensions/mcp/oauth.py` | 823 |
| `src/pi_coding_agent/extensions/mcp/runtime.py` | 600 |
| `src/pi_coding_agent/extensions/mcp/index.py` | 320 |
| `src/pi_coding_agent/extensions/mcp/registration.py` | 300 |
| **源码合计** | **6999** |

上游 `packages/coding-agent/src/core/` 同期为 **35,514 LOC TS / 103 文件**；
`src/extensions/` 为 **7,506 LOC TS**。
本包覆盖 core 约 **4.5%**、extensions 约 **4%**（仅 tool-search），且是**自研精简版**
而非逐文件移植。请勿按「已移植 core」理解本包。

## 裁剪范围（真实状态）

| 上游部分 | 上游规模 | 本包状态 | 说明 |
|---|---|---:|---|
| `src/core/tools/`（bash/read/edit/write/grep/find/ls + truncate） | ~2.5k | ✅ **已复刻（精简）** | 7 个工具 + `truncate.py` + `render_utils.py`。见「有意偏离」 |
| `src/extensions/tool-search/` | 262 | ✅ **已复刻** | `create_tool_search_extension()` + `ToolRegistry`（见「Tier-B」） |
| `src/extensions/codemode/` | 1,396 | ❌ **未复刻** | 阻塞于 `pi-codemode` 的 declarations/source 子模块 + `ModelRegistry`（见「Tier-B」） |
| `src/extensions/mcp/` | 4,257 | ✅ **SDK 面已完成** | `config.py` / `log.py` / `tools.py` / `resources.py` / `oauth.py` / `runtime.py` / `index.py` / `registration.py` / `lifecycle.py` / `factory.py` 全部就位，`create_mcp_extension` 已导出。Manager(`787–1011`) 与 `/mcp` 命令(`1260–1346`) **判定不做**（非欠账） |
| `src/extensions/llama/` | 1,577 | ❌ 未复刻 | 不在本批次范围 |
| `src/core/extensions/`（扩展宿主：runner/loader/事件/boundary） | ~2.3k | ⚠️ **声明面 + 参考宿主** | `extensions/types.py`（声明面）+ `extensions/registry.py`（`ToolRegistry`：装载语义 + 设置/条目/事件/命令/MCP 注册）。**仍不含** runner/loader/boundary/UI 上下文。见「Tier-B」 |
| `src/core/mcp-servers.ts` | 346 | ✅ **完整移植** | `mcp_servers.py`（含 `McpServerRegistry`） |
| `src/core/resolve-config-value.ts` | 287 | ✅ **完整移植** | `resolve_config_value.py`（含 `!cmd` 行为面） |
| `src/core/auth-storage.ts` | 507 | ⚠️ **窄接口** | `auth_storage.py`：只做 `AuthStorageBackend` + `FileAuthStorageBackend`；其余 4 个凭证类不移植 |
| `src/core/model-registry.ts` | 244 | ⚠️ **仅形状（跨包阻塞）** | `model_registry.py`：`pi-ai` 无多模型类型面，故只声明 Protocol |
| `src/core/nested-tool-calls.ts` | 263 | ✅ **完整移植** | `nested_tool_calls.py` |
| `src/core/` 其余部分（AgentSession / session-manager / settings-manager / compaction / model-* / resource-loader …） | ~31k | ❌ **未复刻** | 全部缺失。`sdk.py` 是自研的轻量 `CodingAgent`，不是上游 `AgentSession` 的移植 |
| `src/core/export-html/` | ~1k | ❌ 未复刻 | HTML 会话导出（CLI/TUI 面） |
| `src/core/tools/renderers/` + `render-utils` 的 TUI 部分 | ~1k | ❌ 未复刻 | 依赖 `@earendil-works/pi-tui`；只移植了无渲染依赖的 `format_read_line_range` |
| `src/modes/`（interactive TUI + rpc） | ~24k | ❌ **未复刻** | 上游 Ink TUI 硬翻译无价值；`modes/rpc` 亦未移植 |
| `src/cli.ts` / `src/main.ts` | ~3.6k | ❌ 未复刻 | 本仓库不做 CLI |
| `src/utils/` | ~3.8k | ❌ **未复刻** | 上游工具层依赖它（`utils/shell.ts`、`utils/ansi.ts`、`utils/tools-manager.ts` …）；本包把这部分逻辑**内联**进了 `tools.py` |

> **取舍说明**：本包定位为**编码 agent SDK 的最小子集** —— 「给 Python 用户一个能跑 7 个工具
> + agent 循环的库」。交互式体验由用户基于本 SDK 自行构建（如 textual）。
> `pi` 命令行工具不在本仓库目标内。

## v1.3.0 Tier-A 对齐（2026-10-18）

对齐依据：`recon/04-upstream-coding-agent-delta.md` §6 Tier-A。

| 项 | 上游 | 本包改动 | 状态 |
|---|---|---|---|
| A1 | `core/tools/{bash,read,edit,write}.ts` 的 `constrainedSampling` 从 `PI_EXPERIMENTAL` 门控改为**默认开启** `{type:"json_schema",strict:"prefer"}` | 4 个工具类加 `constrained_sampling` 类属性（`tools.py`） | ✅ |
| A2 | `core/experimental.ts` 删除 `getExperimentalToolSampling()` | 本包未移植该文件，无对应代码 | ✅ 无需改动 |
| A3 | `core/tools/truncate.ts` 新增 `truncateMiddle()` / `MiddleTruncationResult` | `truncate.py: truncate_middle()` | ✅ |
| A4 | `core/tools/bash.ts`：无退出码 → 失败；非零退出码 → `isError` 结果；新增 `bashOutputSchema` / `structuredContent` | `BashTool.execute()` + `_bounded_output()` | ✅ |
| A5 | `core/tools/read.ts` 新增 `readOutputSchema` / `structuredContent` | `ReadTool.execute()` | ✅（`resizeOptions` 部分见下） |
| A5-skip | 同上的 `ReadToolOptions.resizeOptions` / `ctx.model.inputLimits.images.resize` | — | ⏭️ **跳过**：`pi-ai` 尚无 `inputLimits`，属跨包前置 |
| A6 | `core/tools/render-utils.ts` 新增 `formatReadLineRange()`（上游包入口导出） | 新增 `render_utils.py` | ✅ |
| A7 | `core/bash-executor.ts` 跨 chunk ANSI 修复 + `OutputAccumulator.readFullOutput()` | — | ⏭️ **跳过**：本包无 `bash-executor` / `OutputAccumulator` 架构，属「补齐上游架构」而非修 bug |
| A8 | `core/usage-totals.ts` `combineUsage()` | — | ⏭️ **跳过**：本包无 usage 汇总 / nested-call 消费者 |
| A9 | `core/extensions/types.ts` 的 `ToolDefinition` 新字段（`exposure`/`namespace`/`annotations`/`reachesTools`/`prepareLoadout`…） | 仅补 `output_schema`（协议要求） | ⏭️ **跳过**：需扩展系统，本包未移植 |

**跨包依赖说明**：`AgentTool` 协议（`pi-agent-core`）已新增必需的 `output_schema`
成员，以及 `AgentToolResult` 的 `structured_content` / `is_error` / `usage`。本包 7 个工具类
均声明 `output_schema` —— `bash` / `read` 为真实 JSON Schema，其余 5 个为 `None`
（与上游一致：上游只给这两个工具声明了 `outputSchema`）。

## Tier-B：内置扩展（SDK 面，2026-10-18）

对齐依据：`recon/04` §0.4（「本轮最大的新增面不在 core，而在 `src/extensions/`」）。
**只做 SDK 面** —— 被上游包入口 `src/index.ts` re-export 的 `create*Extension` 工厂及其依赖。
CLI 装载逻辑（各扩展的 `cli.ts`、`src/cli/`）与 TUI 部分（`ui.ts`、`renderer.ts`、`log.ts`）不做。

| 上游文件 | 上游 LOC | 本包 | 说明 |
|---|---:|---|---|
| `tool-search/tool.ts` | 244 | ✅ **逐函数移植** | BM25 排序器、分词/词干化、检索文档构造、工具定义与执行 |
| `tool-search/index.ts` | 18 | ✅ | `create_tool_search_extension()` |
| `codemode/tool.ts` | 411 | ❌ 未移植 | 见下方阻塞项 |
| `codemode/execute.ts` | 772 | ❌ 未移植 | 需 `ctx.executeTool()` 嵌套调用 + `ModelRegistry` |
| `codemode/index.ts` | 49 | ❌ 未移植 | |
| `codemode/renderer.ts` | 156 | ➖ 不做 | TUI 渲染 |
| `mcp/index.ts` | 1,347 | ❌ 未移植 | |
| `mcp/{tools,resources,config,runtime,oauth}.ts` | 1,984 | ❌ 未移植 | |
| `mcp/{cli,cli.lazy}.ts` | 596 | ➖ 不做 | CLI 装载逻辑 |
| `mcp/{ui,log}.ts` | 328 | ➖ 不做 | TUI |
| `llama/` | 1,577 | ➖ 不在本批次 | |

### 已移植：扩展声明面 + 参考宿主

要让任何内置扩展落地，最小前提是扩展的**声明面**（上游 `core/extensions/types.ts` 中被
扩展实际引用的那部分，约 839 行 TS 含注释）：

| 本包 | 对应上游 | 说明 |
|---|---|---|
| `extensions/types.py` | `core/extensions/types.ts` | `ToolExposure` / `ToolAnnotations` / `ToolNamespace` / `ToolLoadout` / `ToolLoadoutChanges` / `ToolInfo` / `ToolDefinition` / `ExtensionAPI` / `ExtensionFactory` / `to_agent_tool()` |
| `extensions/registry.py` | `core/agent-session.ts` 的装载逻辑 | `ToolRegistry`：exposure、注册即激活规则、活跃集合、可调用集合、loadout、`prepareLoadout` |

`ToolRegistry` 的语义逐条对齐上游：

- exposure 缺省 `direct`（上游 `_getToolExposure`）
- `direct` / `model-only` 注册即激活；`codemode` / `deferred` / `hidden` 不激活；
  显式 `default_active=False` 抑制激活（上游 `_isDeclarable` / `_isActivatedOnRegistration`）
- 活跃集合只保留已注册且非 `hidden` 的条目，并去重（上游 `_applyToolLoadout`）
- 可调用集合 =「活跃的 `direct`」∪「全部 `codemode`」∪「全部 `deferred`」（上游 `_getCallableTools`）

**未移植的宿主部分**（因此本包**没有**扩展运行时）：`extensions/runner.ts`、
`extensions/loader.ts`、`ExtensionEvent` 派发与 boundary、`ExtensionUIContext`、
`ExtensionCommandContext`、`ToolRenderers`、`prepareLoadout` 的隐藏声明投影。
`ExtensionAPI` 目前只声明 tool-search 需要的 4 个成员；codemode/mcp 还需要
`getSettings` / `appendEntry` / `on` / `registerCommand` / `registerMcpServer` /
`registerToolRenderer` / `getMcpServers`，**尚未声明**。

### Tier-B②：扩展基建（2026-10-18）

对齐依据：task-31。做了两个扩展本体的**前置基建**（不依赖 `pi-mcp/oauth` / `pi-codemode`）。

| 项 | 上游 | 本包 | 状态 |
|---|---|---|---|
| `ExtensionAPI` 成员 | `core/extensions/types.ts` | `getSettings` / `appendEntry` / `on` / `registerCommand` / `registerMcpServer` / `unregisterMcpServer` / `getMcpServers`（6+1 个） | ✅ |
| `ExtensionContext` | 同上 | `cwd` / `mode` / `hasUI` / `isProjectTrusted` / `modelRegistry` / `signal` / `executeTool` / `sessionManager` / `tools` / `ui` | ✅（见下「无 UI 宿主」） |
| `core/model-registry.ts` | 244 行 | `ModelRegistry` Protocol（`getModelsOfType` / `getAvailableOfType` / `getModelOfType` / `classify` / `generateImages`） | ⚠️ **仅形状** |
| `core/nested-tool-calls.ts` | 263 行 | `nested_tool_calls.py` | ✅ 完整移植 |
| `core/mcp-servers.ts` | 346 行 | `mcp_servers.py` | ✅ 完整移植 |
| `core/resolve-config-value.ts` | 287 行 | `resolve_config_value.py` | ✅ 完整移植 |
| `core/auth-storage.ts` | 507 行 | `auth_storage.py` | ⚠️ 窄接口 |

参考宿主 `ToolRegistry` 现在是 `ExtensionAPI` 的**具体实现**：设置读取、会话条目追加、
事件订阅/派发、命令注册、MCP server 注册都是真实实现（可存可查可派发），不是占位。

### Tier-B③：MCP 扩展本体（进行中）

C1→C2→C3 按序做，**每个 C 块边界都必须能 import、过 ruff/mypy、有测试**。

| 块 | 上游 | 状态 |
|---|---|---|
| 基础层 | `config.ts`(242) + `log.ts`(69) | ✅ **已完成**（`config.py` 305 / `log.py` 85，19 条测试） |
| **C1** | `tools.ts`(391) | ✅ **已完成**（`tools.py` 438 行，27 条测试）—— 工具名净化+哈希、`CallToolResult` 输出 schema（`$ref` 重定位）、20KB 中部截断 + 全文落临时文件、二进制资源落盘、资源链接、注解、进度回调 |
| **C2a** | `resources.ts`(339) | ✅ **已完成**（`resources.py` 400 行，18 条测试）—— 三个资源工具、MCP App 资源过滤、`_meta`/icons 剥离、单页/全量、按 server 聚合错误、多内容 URI 标注 |
| **C2b** | `runtime.ts`(454) | ✅ **已完成**（`runtime.py` 600 行，22 条测试）—— 默认传输（http/stdio + 配置值解析 + `~` 展开）、`McpServerConnection`（惰性重连、瞬时错误退避、session 续期、401 升级、`needs-auth`/`failed`/`disconnected` 状态机）、providerToken 认证 |
| **C3a** | `oauth.ts`(558) | ✅ **已完成**（`oauth.py` ~700 行，30 条测试）—— 凭据存储（按 server+URL 作键、旧键接管）、刷新锁、auth provider（预刷新 + 401 刷新 + settled）、CIMD 文档、粘贴式 redirect 解析、`sign_in_mcp_server` |
| **C3b-1** | `index.ts` 69–277 + 1343–1346（≈280） | ✅ **已完成**（`index.py` ~320 行，21 条测试）—— `McpExtensionOptions`、`MCP_SERVERS_SECTION`/`MAX_SERVERS_SECTION_CHARS`/`McpServerListing`/**`render_servers_section`**（上游均为 export）、状态判定族（`exposure_of`/`configured_exposures`/`has_direct_tools`/`has_indirect_tools`/`describe_state`/`attention_rank`/`describe_transport`）、`default_load_config` |
| **C3b-2a** | `index.ts` 279–536（≈258） | ✅ **已完成**（`registration.py` 300 行，21 条测试）—— `registerTools`（命名稳定/哈希后缀、hidden 撤下）、`hideTools`、资源工具同步（取最宽 exposure）、`ensureDiscoveryActive` |
| **C3b-2b-α** | `index.ts` 543–645 | ✅ **已完成**（`lifecycle.py`）—— 令牌快照 / `createConnection` / `startConnection` / `waitForServers` / `reconnectSignedIn` |
| **C3b-2b-β** | `index.ts` **1114–1259** + `643–655` | ✅ **已完成**（`factory.py`，20 条测试）—— 6 个 `pi.on(...)` 钩子 + `reportProblems` + **`create_mcp_extension`** |
| — | `index.ts` 643–783 的其余 6 个 server 动作 | ➖ **按消费路径判定不做（非欠账）** —— 见下「β-1 的消费路径判定」；**不要当欠账去补** |
| — | `index.ts` 1016–1113（`formatStatus` / `pickServer` / `loginCommand`） | ➖ **按消费路径判定不做（非欠账）** —— 见下「β-2 的消费路径判定」；**不要当欠账去补** |
| ~~C3b-3~~ | ~~800–1259~~ | ❌ **已取消** —— 该区段落在 TUI 管理器内（见下） |
| — | `index.ts` 784–1346（**563**） | ➖ 不做 —— Manager（`/mcp` in the TUI）。上游在 `index.ts:786-788` **自带分节标记**，即边界 |

#### TUI 相关工作整体不移植（一条口径，双向判定）

**判定标准**：只看**依赖与消费路径**，不看文件名或所在目录。这条要**双向**使用 ——
既不把 TUI 的东西划进来，也不把非 TUI 的东西划出去。

> **必要补正（2026-10-18 裁决）**：**上游的分节注释说明的是「上游」的组织意图，不是「本包」的
> 消费路径。要看的是「谁调用它」。**
>
> 因此 `1013–1015` 上游自标 `Subcommands and plain status (no TUI)` **并不等于**本包该做它 ——
> 那一节四个符号的消费点全部落在 `/mcp` 命令处理器（`index.ts:1289/1300/1305/1317/1273`）内，
> 而该命令本包不做。**分节注释不能代替调用点检索。**

#### 「欠账」与「判定不做」是两种东西（判定结果请勿当欠账去补）

| | 含义 | 后来者该做什么 |
|---|---|---|
| **欠账** | 该做、还没做（时间/前置不够） | 排期补上 |
| **判定不做** | 按「依赖与消费路径」判定**不该做**，已有共识 | **不要补**；若要推翻，先推翻判据本身并更新本节 |

本包目前判为**判定不做**的有：`index.ts` 643–783 的 6 个 server 动作、`1016–1113` 整节、
`787–1011` Manager、`1260–1346` `/mcp` 命令、`ui.ts`、`cli.ts`，以及
`createMcpToolRenderers()` / `registerToolRenderer()`（无渲染宿主）。
它们的**共同判据**：消费点只在本包不移植的宿主里，或需要本包没有的 UI/渲染宿主。

**「搬过来备用」不算数**：能被真实调用路径消费、且消费有可观察效果，才算支持；
没有消费者的代码是死代码，还会在将来被误认为「已支持」。

**向内（不是 TUI → 要做）**
* `extensions/mcp/log.ts`（69 行）：名字像日志渲染，实为 `mcp.log` 的文件追加 + 5 MiB 轮转，
  **零 TUI 依赖** → 已完整移植（`log.py`）。

**向外（是 TUI → 不做）**
* `extensions/mcp/ui.ts`（259 行）+ `index.ts:784-1346` 的 **Manager（`/mcp` in the TUI）**（563 行）
  —— 上游**自己在 `index.ts:786-788` 标了分节** `// Manager (/mcp in the TUI)`。
  即便它占 `index.ts` 全文 42%，也不移植。
* `extensions/mcp/index.ts` 里的 `pi.registerToolRenderer(...)` 调用（上游 382 行）
  —— 需要渲染宿主，本包没有 → **删除该调用**。
* `extensions/mcp/tools.ts: createMcpToolRenderers()` —— **不导出**（有测试断言不存在）。

**例外（在 TUI 文件里，但是 SDK 公开 API → 要导出）**
* `index.ts: renderServersSection` / `MCP_SERVERS_SECTION` / `MAX_SERVERS_SECTION_CHARS` /
  `McpServerListing` —— 上游均为 `export`，属 SDK 公开面 → 已全部导出（C3b-1）。

**C3b-2a 落地时的两处细节**
* `ensureDiscoveryActive()` 的 UI 告警（上游 `index.ts:532` 的 `ctx.ui.notify`）→ 本包无 UI，
  **改为返回该警告文本**，一次性语义由 `warned_unreachable` 保留。这是「真实判定 + 去掉呈现」，
  判定本身完整计算并可测；不是桩。
* 上游的判定是 `reachable.includes(name)`（**已经可达就不告警**）。若 `codemode` 已在活跃集合里
  （例如用户用 `defaultTools` 启用过），即使 `autoEnableCodemode` 为 false 也**不**告警 —— 有测试锁死。

**为什么不声明优于声明**：扩展若调用 `register_tool_renderer` 会拿到 `AttributeError`
（**响亮失败**），而"注册成功但什么都不发生"是静默失败。按「能被真实调用路径消费且有可观察效果」
的标准，无宿主时的渲染器注册即为桩。

**不要**为了"忠实镜像"去推翻这条：那需要引入本包无法满足的概念（渲染宿主 / TUI）。

#### C2b 的重连 / 续期 / 401 边界（上游 `runtime.ts:284-298` 写明，逐条对齐）

* **只读**请求在第**一次**尝试遇到**瞬时** HTTP 错误时退避重试一次（250ms）；**工具调用不重试** —— 它可能已执行过。
* `McpSessionExpiredError` 在第一次尝试时**重连一次**。依据是上游注释：「server 不再认识该 session，因此它**没有执行**这个请求」。旧 client **脱钩但不关闭** —— 关掉会让它其它在途调用一起失败，而那些调用会拿到同样的 404 并各自按同样方式重试。有测试断言旧 client 未被 close。
* 需重新登录（`McpOAuthAuthorizationRequiredError`，或**配了 auth provider 时**的 `McpAuthRequiredError`）→ 丢 client、进 `needs-auth`、抛带指引的消息，**不再重放**。没配 auth provider 时 `McpAuthRequiredError` 原样抛出（不误判为需要登录）。
* 其它错误原样传播，不做归类（有测试断言状态未被改动）。
* `providerToken` 每次请求都读，因此 provider 的刷新会生效；MCP 侧不存副本。
* `is_transient_error()`：`McpHttpError` 的 408 / 429 / ≥500（**501 除外**）算瞬时；Python 侧网络层失败是 `OSError` 家族（上游判 `TypeError`，本端按等价语义调整，已记）。

| **C3** | `oauth.ts`(558) + `index.ts`(1,347) | ✅ **已完成** —— C3a(`oauth.py`) + C3b-1/2a/2b-β；仅 `/mcp` Manager 与 CLI 命令不做 |
| — | `cli.ts`(594) / `cli.lazy.ts` / `ui.ts`(259) | ➖ 不做（CLI/TUI，沿用 `recon/04` §5） |

**顺序调整**：`runtime.ts` 从 `oauth.ts` 导入 `createMcpAuthProvider`（`runtime.ts:36-41`），
且该调用在 `McpServerConnection` 构造函数内（`runtime.ts:187-195`）——故实际依赖顺序是
**C3a(oauth) → C2b(runtime) → C3b(index)**。

C3a 的跨包形状差异（E1–E6 检查表落地）：
* **E3**：`McpOAuthState` / `OAuthTokens` / `AuthorizationServerMetadata` 都是 TypedDict → 全文件下标访问
* **E6**：`OAuthFlowOptions` / `McpOAuthProviderOptions` / `OAuthCallbackServerOptions` 是 dataclass → 关键字 + snake_case
* **E5**：`wait_for_callback()` 同步取 Future 再 await
* **E2**：`McpFetch` 是 `httpx.AsyncClient` 别名 → `_timed_client()` 返回**带超时的客户端**（上游包装的是 fetch 函数，此为本端偏离，已记）
* **E4**：`can_save_client_information()` 由 pi-mcp 内部使用，本层不探测
* 另：`UnauthorizedContext` 已解析好挑战 → 读 `context.www_authenticate`（上游读 headers）

C2a 的一处实现映射：上游 `Promise.allSettled` → `asyncio.gather(..., return_exceptions=True)`；
上游按名字排序用 `localeCompare`，本端用 `sorted(key=name)`（对 MCP server 名的实际取值等价）。

**`create_mcp_extension` 已导出**（C3b-2b-β）。此前不导出是正确的：那时它是空壳。
两条旧守卫（`test_extensions.py` / `test_extensions_foundation.py` 里的
`test_no_placeholder_*`）已同步改为「`create_codemode_extension` 仍须不存在，
而 `create_mcp_extension` 必须可导入且**是工厂**」。

上游对应测试（完成后逐条对应）：`test/mcp-extension.test.ts`(845)、
`test/mcp-oauth-store.test.ts`(55)、`test/mcp-oauth-refresh.test.ts`(265)、
`test/mcp-conformance/`。

#### β-1 的消费路径判定（为什么只移植 `reportProblems`）

`index.ts:643–783` 的 7 个 server 动作，按「看依赖与消费路径」逐条查调用点（`grep -n`）：

| 动作 | 上游调用点 | 消费者 |
|---|---|---|
| `reportProblems` | `1135` / `1143` / `1249` | **6 个钩子** ✅ 已移植 |
| `saveConfig` | `743` / `769` | 仅 `setEnabled` / `setExposure`（内部） |
| `signIn` | `927` / `1091` | Manager(787–1011) + `/mcp login` |
| `signOut` | `977` / `1307` | Manager + `/mcp logout` |
| `reconnect` | `1324`（定义于 `720`） | 仅 `/mcp reconnect` |
| `setEnabled` | `990` | 仅 Manager |
| `setExposure` | `917` | 仅 Manager |

**结论**：7 个动作里只有 `reportProblems` 被钩子消费；其余 6 个的**全部**调用点都在
Manager 与 `/mcp` 命令内。本包不移植 Manager/CLI → 移植它们只会得到**没有消费者**的死代码。
故只做 `reportProblems`，并在 `factory.py` 顶部逐条登记。

#### β-2 的消费路径判定（为什么 1016–1113 也不做）

| 符号 | 上游调用点 | 判定 |
|---|---|---|
| `formatStatus` | `1289`（`/mcp` 命令 `ctx.ui.notify`） | ➖ CLI |
| `pickServer` | `1300` / `1305` / `1317`（命令） | ➖ CLI，且需 `ctx.ui.select` |
| `loginCommand` | `1301`（命令） | ➖ CLI，且需 `showMcpManager`(TUI) |
| `usesOAuth` / `oauthPick` | `1273`（命令补全）+ `1300`/`1305` | ➖ CLI |

**这一节虽然夹在 TUI Manager 之后、且**上游自标 `Subcommands and plain status (no TUI)`**，
但它的四个符号的消费点**全部**落在 `/mcp` 命令处理器内**，而该命令在 1260–1346（➖ 不做）。
再加之 `pickServer` / `loginCommand` 需要 `ctx.ui.select` / `showMcpManager` —— 本包
`has_ui` 恒为 `False`、`ui` 恒为 `None`。按同一口径，这一节**不做**。

> 这是「上游自标 no TUI」与「本包该不该做」**不是一回事**的实例：判定依据始终是
> **依赖与消费路径**，不是上游的分节注释。若将来宿主补上 UI，这一节可以整体接回来
> —— 它的判定逻辑（`formatStatus` 的文本、`pickServer` 的候选筛选）本身是完整的。

#### C3b-2b-β 的钩子签名偏离（**失效类**，照抄会 `TypeError`）

上游 `pi.on(name, (event, ctx) => …)` 是**两个**参数；本仓宿主的
:meth:`ToolRegistry.emit` 只传**一个** payload（`handler(payload)`）。

照抄两参数签名 → 派发时直接 `TypeError`。本端的等价改写：

* `session_start` / `turn_start` / `mcp_servers_change` / `session_shutdown`：payload 就是 `ctx`。
* `before_agent_start`：:class:`AgentStartPayload`（`system_prompt_options` + `ctx`）。
* `tool_call`：:class:`ToolCallPayload`（`tool_name` + `input` + `ctx`）。

`reportProblems` 的 ``ctx.ui.notify``（`index.ts:651`）按既有口径**去掉呈现、保留判定**：
改为**返回**文本（`None` 表示无问题），判定与 `config:` / `needs-auth` / `failed` 的分类完整保留。

#### C1 的偏离与集成要点

1. **`createMcpToolRenderers()` 不移植**：上游 `tools.ts:354-390` 依赖 `pi-tui`
   （`Text`/`Container`/`VisualLinePreview`/`keyHint`）与 `render-utils` 的 TUI 辅助，
   本包无渲染宿主 → **不导出**（有测试断言 `not hasattr(module, "create_mcp_tool_renderers")`）。
2. **`_to_pi_content()`：pi-mcp 与 pi-ai 的「同形不同类」适配**。`pi_mcp.to_llm_content`
   返回的是 `LlmTextContent`/`LlmImageContent` —— 与 pi-ai **字段同形但是 dict**
   （`{"type":"text","text":…}` / `{"type":"image","data":…,"mimeType":…}`）。
   而 `AgentToolResult.content` 与下游（agent 循环、`_text_of`）按**属性**访问 `.type`/`.text`，
   因此必须规范成 pi-ai 的 `TextContent`/`ImageContent` 对象。上游无此步骤（TS 侧本就是对象）。
3. **`save_to_temp_file()` 取代上游 `utils/output-files.ts: writeOutputFile`**（该模块未移植）：
   随机名 + 指定扩展名 + **`0o600`**（对齐上游 v1.0.3 起「输出文件仅本人可读」）。

#### 施工注记〈工具会骗你的几种方式〉
### 判据：**测试第一次红时，先问「我的夹具是不是在测一个不存在的东西」**

这已经是**第三次**「测试夹具错误伪装成实现错误」：

| # | 夹具的错 | 看起来像 |
|---|---|---|
| 1 | 假设 `format_size` 需要移植（其实本仓早有） | 「实现缺失」 |
| 2 | 假设 `get_models_of_type` 同步（本仓是 async） | 「实现没 await」 |
| 3 | abort spy 在 `__init__` 抓 `abort_event`（实际传给 `execute()`） | 「abort 静默丢失」 |
| 4 | 假设 `reaches_tools` 只取部分词（上游是 `match(/\w+/g)` 取**全部**） | 「实现多返回了词」 |

**共同形态**：**测试红了 → 看起来像被测代码错了 → 实际是夹具错了**。

> **判据**：测试第一次红时，先问「**我的夹具是不是在测一个不存在的东西**」——
> 尤其当失败信息指向「某个值没传到位 / 多出来了」时，先确认**夹具真的抓在传值的那条路径上**。

落地做法：**夹具陷阱写成测试里的注释**（例如
`"""abort_event 是传给 execute() 的，不是构造参数 —— 别抓错地方。"""`），
留给下一个写这个 fixture 的人，比留在汇报里有价值。

与〈工具会骗你的几种方式〉同族：**工具/测试本身在骗你**。

这一节记录的不是「注意点」，是**已经骗过我们三次的具体机制** —— 每一条都有实例，
因为「写它的原因」+「它救过一次」两个记录比只有前者可信得多。

| # | 它怎么骗你 | 真实实例 | 怎么防 |
|---|---|---|---|
| 1 | **命令没报错 ≠ 文件改了** | 用 Python heredoc 打补丁时正文里含**三引号** → 整段脚本 `SyntaxError` → **文件完全没被改动**，但命令尾部输出看起来「跑过了」（后面还有别的命令接着跑）。另一次是字符串里含**全角引号**却包在双引号里，同样整段失效 | 改动走**临时脚本文件**（不在 shell 里拼引号）；每处替换 `assert src.count(old) == 1` 锚定；**末尾回读校验**（`grep -c`） |
| 2 | **源码扫描会命中 docstring / 注释** | 断言「MCP 源码里不得出现 `mkstemp`/`0o600`」，被**自己写的 docstring** 绊倒 —— 那段文字为说明历史提到了 `0o600` | 只看**代码体**：以 docstring 定界符 split 成至多 3 段取第 3 段，再断言 |
| 4 | **「我以为它在别的包」** | 判断「改 `nested_tool_calls.py` 超出我的写入范围」，实际它就在 `packages/pi-coding-agent/src/pi_coding_agent/` 下 —— **`find` 一下就能证伪的假设，却被当成了结论** | 说「超出范围」之前先 `find`/`ls` 核实路径 |
| 3 | **`ruff format packages/` 会写到别人的文件** | 全仓格式化会把队友正在改的包一起重排 —— 边界不是「我的包」而是「当前写入范围」 | **只格式化自己的包**（`ruff format packages/<mine>`）；全仓 `--check` 只读，可以跑 |

三条的共同点：**工具都「成功返回」了，但做的事不是你以为的那件**。
所以关键改动一律**回读校验**，不要用「命令退出码 0」当证据。

做法：改动走**临时脚本文件**（不在 shell 里拼引号），并对每处替换做
``assert src.count(old) == 1`` 锚定，最后**回读校验**（例如 ``grep -c "关键字"``）确认真的生效。

#### 偏离的两类：**选择** vs **照抄会导致功能失效**

登记偏离时请区分这两类，因为对后来者的意义完全不同：

* **选择类**：本端主动不做/换做法，代价已知且可接受。
  例：不回环回调服务器（直接复用 ``pi-mcp/oauth`` 的 ``OAuthCallbackServer``）；
  ``_bounded_output()`` 内联在 ``tools.py`` 而非移植 ``OutputAccumulator``。
* **失效类**：**逐字照抄上游会让功能静默失效**，必须按等价语义改写。
  例：``runtime.py: is_transient_error()`` —— 上游判网络层失败用 ``instanceof TypeError``，
  而 Python 侧 httpx 抛 ``OSError`` 家族，**照抄会永远返回 false**（重试静默失效）；
  ``tools.py: _to_pi_content()`` —— ``pi_mcp.to_llm_content`` 返回与 pi-ai **同形但不同类**的
  对象，不转换则下游按属性访问时 ``AttributeError``。

失效类偏离**必须**在报告与本文里写明「为什么照抄不行」，否则后人会以为是可以随意抹平的差异。

#### 越界判断是**双向**的（判定依据，记此以免后人误用）

既**不把 TUI 的东西划进来**，也**不把非 TUI 的东西划出去**。

正例：`extensions/mcp/log.ts`（69 行）——名字像日志渲染，实为 `mcp.log` 的文件追加 +
5 MiB 轮转，**零 TUI 依赖**，因此在范围内，已完整移植。
反例：`extensions/mcp/ui.ts`（259 行）—— 依赖 `pi-tui`，不在范围。

判定只看**依赖与消费路径**，不看文件名或所在目录。

#### 登记在案的偏离（逐条，不静默）

1. **`registerToolRenderer()` 不声明**。上游该方法注册 TUI 渲染器
   （`ToolRendererResolver` / `ToolRenderers` 都依赖 `pi-tui`）。本包无渲染宿主，
   注册了也无处消费——因此**名字不存在**，而非留一个空实现。有测试断言其不存在。
2. **无 UI 宿主**：`ExtensionContext.ui` 恒为 `None`、`has_ui` 恒为 `False`。
   这是如实反映「本包没有 UI」，需要 UI 的扩展按上游惯例检查 `ctx.has_ui` 后走无 UI 分支。
3. **`model_registry.py` 仅声明形状**：上游这些方法的参数/返回类型来自 `pi-ai` 的
   多模型类型面（`ModelType` / `AnyModel` / `ClassifierModel` / `ImageModel` /
   `ClassifierContext` / `ClassifierResult` / `ImagesContext` / `AssistantImages`），
   实测 `pi-py` 的 `pi-ai` **一个都没有**。故以 `Any` 占位、只保留方法名/参数顺序/异步性。
   这是 `Protocol`（无实现，不可实例化），**不是桩**；等 `pi-ai` 补齐后替换类型即可。
4. **`auth_storage.py` 是窄接口**：只做 `AuthStorageBackend`（2 方法）+ `FileAuthStorageBackend`。
   上游其余约 300 行（`AuthStorage` / `ReadOnlyAuthStorage` / `InMemoryAuthStorageBackend` /
   `readStoredCredential`，依赖 pi-ai 的 `Credential` / `CredentialStore`）**不移植、不导出**，
   有测试断言这 4 个名字不存在。理由：MCP 扩展只把它当「带锁的 JSON 文件读改写原语」用。
5. **`FileAuthStorageBackend` 的锁实现偏离**（上游用 npm `proper-lockfile`）：
   - 本端零新依赖，用 `os.mkdir` 的原子性实现 `<path>.lock` 排他锁
   - **上游的 `compromised` 回调（mtime 心跳）未实现** —— 本端无心跳线程，锁在临界区内不会被
     主动判失效，**只有冲突方才会回收过期锁**
   - **过期锁回收**：同主机且 `owner.json` 里的 pid 已不存在 → 立刻回收；否则锁目录 mtime
     超过 `STALE_LOCK_SECONDS`（10 秒）→ 判失效。**这消掉了「崩溃后需人工清理锁文件」的后果**
     （上游靠心跳达到同一目的，机制不同、效果等价）
   - 临界区是「读一个小 JSON + 写回」（毫秒级），10 秒阈值有 3–4 个数量级余量
   - 回收判定按「pid 不存在」时**不会**误抢活锁；`PermissionError`（pid 属他人）按存活处理
6. **`resolve_config_value.py` 的 Windows shell**：上游 Windows 优先用
   `utils/shell.ts: getShellConfig()` 解析出的 shell（Cygwin / git-bash，支持 stdin 传命令）；
   本包未移植该模块，**Windows 一律走默认 shell**，POSIX 上两者等价。

#### `!command` 行为面（安全相关，照上游保留）

以 `!` 开头的配置值是**要执行的 shell 命令**，stdout 被当作配置值（如 API key）。
命令只来自**用户自己写在配置里**的内容（`mcp.json` / `models.json` 的 `headers` / `env` /
`clientSecret` 等）。执行超时 10 秒、stderr 丢弃、**失败结果同样进缓存**（进程生命周期内）。
本端不做削弱，也不额外放宽。

## Tier-B④：codemode 扩展（✅ **已完成**）

对齐依据：task-40。上游 `src/extensions/codemode/`（6 文件 1,396 行）。

**开工前的消费面盘点**（task-40 的前提「前置已就绪」**不成立**，实测包内 `core/` 依赖
≈**4,026 行**未移植，而扩展本体 SDK 面只有 1,240 行）。按「消费面多大就移多大」逐个量：

| 上游前置 | 行数 | codemode 实际用到 | 处置 |
|---|---:|---|---|
| `core/session-manager.ts` | 2,013 | **只有 `SessionEntry` 类型**（`execute.ts:29` 是 `import type`） | **类型专用切分**（④-2 时落地） |
| `core/settings-manager.ts` | 1,443 | — （**只是 re-export**） | 不涉及 |
| `core/settings-schema.ts` | — | **只有 `CodemodeMode` 类型** | **类型专用切分** ✅ ④-0 |
| `core/tools/truncate.ts` | 314 | **只有 `formatSize`**（`execute.ts:366`） | **部分移植**（④-2 时落地） |
| `core/usage-totals.ts` | 97 | **只有 `combineUsage`**（`execute.ts:459`） | **部分移植**（④-2 时落地） |
| `core/settings-defaults.ts` | 67 | `SETTINGS_DEFAULTS.codemode` 两个键 | ✅ **完整移植** ④-0 |
| `core/tools/tool-definition-wrapper.ts` | 58 | `wrapToolDefinition`（`tool.ts:404` 唯一调用点） | ➖ **判定不做**（见下） |
| `utils/output-files.ts` | 34 | `writeOutputFile` | ➖ 复用 MCP 先例（`save_to_temp_file()` 替代） |
| `extensions/codemode/renderer.ts` | 156 | `tool.ts:37,391` 的 `...codemodeRenderers` | ➖ **判定不做**（TUI） |

### Tier-B④ 逐块表

| 块 | 上游 | 状态 |
|---|---|---|
| **④-0 前置** | `settings-defaults.ts`(67) + `settings-schema.ts` 的 `CodemodeMode` | ✅ **已完成**（`settings_defaults.py` / `settings_schema.py`，6 条测试） |
| **④-1a** | `codemode/tool.ts` 的纯函数（声明/描述/目录选择） | ✅ **已完成**（`extensions/codemode/tool.py`，16 条测试）—— `to_codemode_declaration` / `get_codemode_callable_tools` / `create_codemode_description`（含 `render_tool_section` / `select_catalog` / `describe_fields` / `describe_output` / `describe_script_call` / `describe_globals`）+ 常量族 |
| **④-1b-1** | `tool.ts` 的 loadout 与可达性（`prepareCodemodeLoadout` / `getScriptReach` / `CodemodeToolOptions`） | ✅ **已完成**（同上文件，共 24 条测试） |
| **④-1b-2** | `tool.ts` 的 `createCodemodeToolDefinition` / `createCodemodeTool` | ⛔ **被 ④-2 阻塞**（见下「④-1b-2 的前向依赖」） |
| **④-0b 缺口一** | `config.ts` 的 `getDocsPath()` + `docs/codemode.md` | ✅ **已完成**（`config.py` + 文档入库，4 条测试 + wheel 验证） |
| **④-2a** | `execute.ts` 的四个前置小件 | ✅ **已完成**（`usage_totals.py` / `output_files.py` / `session_manager.py`，16 条测试） |
| **④-2a-2** | `execute.ts` 的纯逻辑（47–439） | ✅ **已完成**（`extensions/codemode/execute.py`，43 条测试）—— 预览 / `to_model_type`·`to_provider`·`to_model_info`(丢 headers) / `describe_value` / **`check_classifier_context`·`check_images_context`**（形状校验带文档路径）/ `create_limiter` / `is_store_entry_data`·`read_codemode_store` / `format_output`·`join_adjacent_text` / `format_call_summary`·`format_error` / `spill_output`·`save_images`·`truncate_output` / `to_script_value` |
| **④-2b** | `execute.ts` 的 `executeCodemode`(440–579) + globals(580–772) | ⬜ 未做（**之后才是 ④-1b-2**） |
| — | `worker.ts`(6) + `execute.lazy.ts`(2) | ➖ **判定不做**（JS 运行时矩阵，见上「④-2 的 wasm/worker 轴」） |
| **④-3** | `codemode/index.ts`(49) + 导出 `create_codemode_extension` | ⬜ 未做（**只有 ④-1/④-2 完成后才导出**） |
| **④-2b-2a** | `execute.ts` 591–657 `createDiscoveryGlobals` + `isNamespaceName` | ✅ **已完成**（`execute.py`，9 条测试）—— `searchTools`（BM25 排序 + `limit`/`namespace` 过滤 + 参数校验）/ `describeTool`（原名或脚本标识符）/ `describeNamespace`（`name`/`description`/`instructions`/`tools`，**缺席的键不写**）；三个全局均 `spread=True` |
| **④-2b-2b** | `execute.ts` 664–772 `createModelGlobals` | ⬜ 未做 —— **待确认 pi-ai 结果字段**（见下） |
| **④-2b-2b** | `execute.ts` 664–772 `createModelGlobals` | ✅ **已完成**（同文件，13 条测试）—— `models.getModelsOfType` / `getAvailableOfType` / `getModelOfType` / `classify` / `generateImages`，5 个全局均 `spread=True`；目录条目**丢 `headers`**；分类/图像调用落**嵌套调用行**（只显示模型，不含 prompt/图像数据）；用量进 `add_usage`、`record.cost`；三态 `stop`→`ok` / `aborted`→`cancelled` / 其余→`error` **不合并** |
| **④-2b-1** | `execute.ts` 440–574 `executeCodemode` | ✅ **已完成**（`execute.py`，11 条测试）—— 装配（`get_codemode_callable_tools` / `render_tool_sample` 样本 / 嵌套调用行 `toScriptValue`）/ `parse_codemode_source`（失败即拒绝）/ 配额（**抛错不排队**）/ **executor + `abort_event` 桥接** / 结果收敛（store 落条目、返回值当文本、`Script error:`、未展示图像提示、截断 + 落盘、`Script completed|failed` 头、`details.calls`、`usage` 合并、`is_error`） |
| **④-1b-2** | `tool.ts` 371–410 `createCodemodeToolDefinition` / `createCodemodeTool` | ✅ **已完成**（`tool.py`，7 条测试）—— 局部 `ToolDefinition` 子类（照 **MCP 同目录先例**）：`exposure="model-only"`、`reaches_tools`（脚本源码→可达词）、`prepare_loadout`（接 ④-1b-1）、`constrained_sampling`（`CODEMODE_SOURCE_GRAMMAR`）、`execute` **懒加载** `.execute`；`create_codemode_tool` 只换 `description`，**不包层** |
| **④-3** | `index.ts` 1–49 工厂 | ✅ **已完成**（`index.py`，10 条测试）—— `create_codemode_extension` **已从包入口导出**；`read_mode` / `read_inline_budget`；**不预填 description**（上游注册的就是空列表版本，真正的描述由 `prepare_loadout` 在活跃时算） |
| — | `codemode/renderer.ts`(156) | ➖ **判定不做**（TUI，见下） |

#### ④-1b-2 的前向依赖（为什么这一半现在做不了）

上游 `createCodemodeToolDefinition` 的 `execute` 是：

```ts
execute: async (toolCallId, params, signal, onUpdate, ctx) =>
    (await loadCodemodeExecutor()).executeCodemode(toolCallId, params, signal, onUpdate, ctx, options),
```

而 `loadCodemodeExecutor = () => import("./execute.ts")` —— **`execute.ts` 就是 ④-2**。

本包的 `ToolDefinition` 是 **ABC**（`execute` 为 `@abstractmethod`）⇒ **没有 `execute` 就构造不出实例**。
这不是「暂时留个桩」的选择问题，是**根本建不出来**（想留桩也会在实例化时 `TypeError`）。
因此 `create_codemode_tool_definition` / `create_codemode_tool` **留到 ④-2 之后**，此处不导出。

### 偏离索引（三类分开，勿混）

**（甲）部分移植** —— 只移被消费的那部分，**模块其余部分未移植**：

| 本包 | 上游 | 只移了 | 状态 |
|---|---|---|---|
| `config.py` | `config.ts`（约 460 行） | 只有 `get_docs_path()` | ✅ ④-0b |
| `truncate.py`（**原本就有**） | `core/tools/truncate.ts`(314) | `format_size` **早已存在**，无需移植 | ✅ 已核对等价 |
| `usage_totals.py` | `core/usage-totals.ts`(97) | 只有 `combine_usage` | ✅ ④-2a |

**`config.py` 的 `get_docs_path()` 与 `<packageDir>` 的 Python 对应物**：上游 ``<packageDir>``
是 npm 包根，语义是「这个包自己携带的目录」；Python 侧对应**可导入包目录**
（``src/pi_coding_agent/``）。放包内还有一个实际好处 —— ``hatchling`` 会**自动**把
``src/pi_coding_agent/**`` 打进 wheel，于是**源码树与 wheel 用同一段解析代码都能找到文档**；
若照搬 npm 的目录层级（``packages/pi-coding-agent/docs/``），wheel 里要 ``force-include``
到别处、运行期还要双路径查找。

**`docs/codemode.md` 资产**（230 行 / 14,450 字节，**逐字节入库**，sha256
``a0672912…ddc0``）：

* **出处**：`earendil-works/pi` **v1.3.0** `packages/coding-agent/docs/codemode.md`，MIT
  （与本次移植同源同许可；**非第三方资产**，故未另立 `THIRD_PARTY_NOTICES.md`
  —— 那份先例针对的是 `pi-codemode` 里来自别的项目的 `quickjs.wasm`）。
  若你认为同源文档也要单独立notice，我可以补。
* **为什么要入库**：它不是文案 —— 上游 release notes 说明描述里指向的这份文档是
  **模型实际阅读的 `models.*` API 参考**。指向不存在的文件 = `models` 能力缺失，而不是少一个常量。
* **打包验证**：`uv build --wheel` → wheel 内 ``pi_coding_agent/docs/codemode.md`` 存在
  （33 个条目，**无需 force-include**）；再从**解包后的 wheel** 里 import 验证
  ``get_docs_path()`` 解析到 14,450 字节、首行 ``# Codemode``。
* **已知遗留**：文档内有 3 处交叉引用指向**未移植**的同级文档
  （``cli.md`` / ``models.md`` / ``settings.md``）。保持**逐字忠实**，不删链接；
  模型若点进去会读不到 —— 待这些文档入库时自然解决。

**（乙）类型专用切分** —— **只取类型，模块其余部分未移植**（沿用 `pi-mcp/auth_provider.py` 先例）：

| 本包 | 上游出处 | 取了什么 | 状态 |
|---|---|---|---|
| `settings_schema.py` | **`core/settings-schema.ts:148-150`**（schema 实体）+ **`:469`**（`Static<…>`） | 只有 `CodemodeMode`（`Literal["on","only"]`） | ✅ ④-0 |
| `session_manager.py` | `core/session-manager.ts`（2,013 行） | 只有 `SessionEntry` 联合（11 变体），**无运行期行为** | ✅ ④-2a |

> **出处勘误**：`CodemodeMode` **不在** `core/settings-manager.ts` —— 那里（`:32`）只是
> **re-export**。按 `settings-manager.ts` 去找会白费一轮；定义处是 `settings-schema.ts`。

**（丙）判定不做** —— 按「依赖与消费路径」判**不该做**（非欠账）：

| 上游 | 判据 |
|---|---|
| `codemode/renderer.ts`(156) | import `pi-tui` + `render-utils` + `keybinding-hints` + `visual-truncate` + `theme` —— 纯 TUI，本包无渲染宿主 |
| `core/tools/tool-definition-wrapper.ts`(58) | **纯转换层**：唯一调用点 `tool.ts:404`，**不传 `ctxFactory`**、不做 schema 处理，`Object.assign` 在包装**之后**才贴 `description`/`promptSnippet` —— **不产出任何 pi-py 拿不到的东西**。且 pi-py 已登记偏离「工具**直接作为 `AgentTool` 使用而不再包一层**」（`extensions/types.py:155-157`），`ToolDefinition.execute` **已有第 5 个参数 `ctx`**，与包一层之后同形 → 该层为 **no-op** |
| `utils/output-files.ts`(34) | 复用 MCP 轮的先例：`save_to_temp_file()` 替代，登记为选择类偏离 |

**连带结论**：`ExtensionToolContext`（上游 `extensions/types.ts:383-395`）在本包**不存在**，
但它只被上面那三件套的签名需要 → **随「判定不做」自然消失**，不补、不开任务
（补了就是「没有消费者的类型」）。

### 共享模块提取：`utils/output-files.ts` → `output_files.py`（④-2a）

上游 ``writeOutputFile`` 同时被 **MCP 工具**与 **codemode 的 `execute.ts`** 使用。本仓此前只有
MCP 一个消费者，实现内联在 ``extensions/mcp/tools.py``；现在 codemode 是第二个消费者，于是提到
``output_files.py``，**MCP 的那份改为一行委托**（保留自己的 ``pi-mcp-`` 前缀，行为不变，27 条测试照过）。

**为什么不直接复制一份**：两份实现会各自漂移 —— 上游 v1.0.3 起「输出文件仅本人可读」的 ``0o600``
只会改到一处。这与本轮判「不做」`getQuickJSWasmPath` 是**同一个理由**：**避免制造将来才暴露的不一致**。

### `SessionEntry` 的一处 Python 侧形状偏离（④-2a 登记）

上游 ``SessionEntryBase`` 含 ``type: string``，各变体把它收窄成 ``Literal[...]``。
**Python 的 TypedDict 不允许收窄父类字段**（mypy：*Definition of field "type" incompatible with
base class*），因此本端的基类**不含** ``type``，由 11 个变体各自声明。
运行期形状与上游一致，只有静态继承关系差一层。有测试断言「每个变体都自带判别字段」。

### `to_model_info` 丢掉 `headers`：**有意的安全取舍**（不是偏离）

上游 `toModelInfo` 的注释：*`headers` is dropped because models.json headers can carry credentials.*
本端照做（`execute.py: to_model_info`）—— 脚本拿到的模型目录条目里**没有** `headers`。

**这不是「偏离上游」，是上游自己的决定**；单列出来是因为它有可见后果：

* **好处**：脚本（模型写的代码）无法读到 `models.json` 里的自定义 header，也就读不到里面的 key。
* **代价**：若某个模型**依赖自定义 header 做路由**（自建网关、灰度标记），脚本侧看不到它，
  因而无法据此筛选模型。目前 `models.*` 只按类型/provider 选、不按 header，故无实际影响；
  若将来要按 header 选，需要另开一条**不泄露值**的通道（例如只暴露 header 名）。

有测试断言 `to_model_info({'id': 'm', 'headers': {...}}) == {'id': 'm'}`。

### `AgentToolCallOutcome` 无对应物（④-2a-2 登记）

上游 `toScriptValue(tool, outcome)` 的第二参是 `AgentToolCallOutcome` = `{ result, isError }`
—— 结果与错误标志**分两个字段**。本仓的 `AgentToolResult` **自带 `is_error`**，
没有外层包装类型（`pi_agent_core` 里没有 `AgentToolCallOutcome`）。
故 `to_script_value` 直接收结果对象读 `is_error`。运行期语义一致，只是**少一层包装**；
有测试断言「`is_error` 时抛，且优先用工具的错误文本」。

### 源码扫描类断言要**排除 docstring / 注释**

一次真实的自摆乌龙：`test_mcp_saver_delegates_to_the_shared_module` 断言「MCP 源码里不得出现
`mkstemp` / `0o600`」，结果被**我自己写的 docstring** 绊倒 —— 那段文字为说明历史提到了 `0o600`。

> **规则**：扫源码的断言只看**代码体**：先用 split 以 docstring 的定界符切成至多 3 段，
> 取第 3 段（即代码体）再断言。否则「说明它已经没有它」的注释会被当成「它还在」。

> 本轮还复现了〈施工注记〉里那条：用 heredoc 打补丁时，正文里的三引号把整段脚本变成 SyntaxError，
> **文件完全没被改动**（幸好 `assert` 与回读校验把这一点暴露了）。

### `nested_tool_calls`：宿主返回值被包成 `ToolCallOutcome`（**(d)**，下游可见变更）

**背景**：上游 `execute.ts:488-489` 读 `outcome.toolCall.id` / `outcome.isError`，而本仓
`NestedToolCallRunner.execute` 原本**原样透传**宿主 `run_tool_call` 的返回值
（一个带 `.is_error` / `.result` / `.duration_ms` 的对象）—— 形状已接近上游
`AgentToolCallOutcome`，**只缺 `toolCall` 字段**。

**改动**：`NestedToolCallOutcome` → 新增 `ToolCallOutcome(dict)`（**属性可访问的映射**，
形状同 `pi-mcp` 的 `LlmContentBlock`）。`execute` 返回它，并补上 **runner 自己生成**的
`tool_call`（`<caller id>/<n>`，序号**从 1 起**，与上游 `nested-tool-calls.ts:184` 的 `nextId: 1` 一致）。

* 仍是 `dict`：`outcome["tool_call"]["id"]` / `outcome["is_error"]` 可用；
* **属性访问不变**：`outcome.is_error` / `.result` / `.duration_ms` 照旧 → 既有读法不坏；
* `duration_ms` 缺席时**不写键**；
* **`is_error` 的来源不变**：仍读宿主的 `outcome.is_error`，**不是** `result.is_error`。

> ⚠️ **为什么 `is_error` 不改读 `result.is_error`**：两者不等价。宿主在**工具被拦截/校验失败**时
> 会置 `is_error=True` 而 `result` 可能是 `None`；改读 `result` 会让这些情况**静默**从「错误」
> 变成「正常」。这是一次「上级要求本身会造成缺陷、被证据顶回来」的记录 —— 判据是代码。

**下游可见变更**（单列，勿与上面的内部细节混淆）：

> 宿主 `run_tool_call` 的返回值**现在会被包成** `ToolCallOutcome`。
> **属性访问不变**，但 `返回对象 is 宿主原对象` **不再成立**。
> 仓库内**无消费者**（`grep src/ tests/` 全空），故破坏面为零；下游宿主若依赖同一性需自查。

**测试**（`tests/test_nested_tool_calls_outcome.py`，6 条）：

* 形状：`dict` + 属性访问双通；`duration_ms` 缺席不写键；
* **`is_error` 语义零变更**：断言「宿主标了错、`result` 为 `None`」时 `outcome.is_error is True`；
* **关联性**（这条才是重点）：跑一次真实嵌套调用，收集 `tool_execution_start` 事件，断言
  `outcome["tool_call"]["id"] == 事件的 toolCallId` 且 `toolCallId == f"{parentToolCallId}/1"`；
* 第二跳序号递增且**各自**与事件对得上（防止「只有第一跳对」）；
* `end` 事件与 `start` 事件、与 outcome **三者同一个 id**。

### 取消路径下的配额：**名额拴在「线程真正结束」上**，不是 `await` 的 `finally`

上一节说 `to_thread`/executor **不可取消**（取消外层任务不会中断线程）。它的反面后果是：
**若把配额释放放在 `await` 的 `finally` 里，外层被取消时 `finally` 会立刻执行，而线程还在跑**
—— 于是「已被放弃的脚本」继续占着执行资源，但名额已经还回去了，
**实际并发会短暂超过上限**。

**本端的做法**（`execute.py: execute_codemode`）：把**名额释放与沙箱关闭都挂在 executor future 的
`done_callback` 上**，await 用 `asyncio.shield` 包住：

```python
future = loop.run_in_executor(None, partial(sandbox.execute, code, store=store, abort_event=abort_event))
future.add_done_callback(_finish)     # ← 线程真正结束时才 close() + release()
result = await asyncio.shield(future) # 外层被取消时，future 继续跑，回调仍会执行
```

**⇒ 取消路径下不再有「名额已还、线程还在跑」的窗口**，因为贷款与还款都锚在同一个事件（线程结束）上。

**仍未消除的差异**（上一节已登记）：本端只能「调用前 acquire」，配额窗口 = **装配 + 执行期**
（含建引擎 / 编译 wasm），比上游的「worker 存活期」**更宽** —— 即**可能更早开始拒绝**。
这条是接口缺 `acquireWorker` 回调导致的，与取消路径无关。

### 机制约束：**中断 codemode 脚本只能靠 `abort_event`，不能靠取消 `to_thread` 的任务**

`CodemodeSandbox.execute`（`pi-codemode`）是**同步**阻塞调用，本端在 `asyncio.to_thread(...)` 里跑它
（上游 `await sandbox.execute(...)` 是 Promise）。由此产生一条**机制层面的判据**：

> **`asyncio.to_thread` 不可取消**：取消外层任务会抛 `CancelledError`，但**线程本身继续跑到底**。
> 所以「用 `task.cancel()` 中断正在执行的 codemode 脚本」是**白费力气** —— 脚本会跑完，
> 只是结果没人要了（而沙箱仍占着配额窗口）。

**本端走的路**：把 `asyncio.Event`（本仓约定）桥接成 `threading.Event` 传给
`CodemodeSandbox.execute(..., abort_event=...)`，由沙箱内的看门狗真正中断执行。
**桥接的时序是关键**：`abort` 转发晚一拍就会**静默丢失**（事件设了、脚本没停）。
因此验收要求是「断言脚本**在预期时间内结束**、且结果是**被中断的形态**（`kind="aborted"`）」，
而**不是**断言 `event.is_set()`。

#### 通则：**修「静默失效」类 bug 时，判据不能是返回值**

静默失效的**定义**就是「返回值正常」。所以用返回值验证，等于用「症状消失了」证明「病好了」——
而这类 bug 的症状本来就不出现在返回值上。

> **要盯副通道**（stderr / 日志 / 线程状态 / 副作用），**并把那个副通道做成断言**。

实例（`pi-codemode` 的 `timeout_ms=inf`）：`OverflowError` 抛在**看门狗线程内部**，主路径照常
返回 `ok=True`。修完第一处看门狗后返回值已正常，**但 stderr 仍有 traceback** ——
只修了一处时，「看返回值」会判定为已修。当时是靠**继续盯 stderr** 才找到第二处
（`sandbox.py` 的另一个看门狗）。最终固化成测试：

```python
captured = capsys.readouterr()
assert "OverflowError" not in captured.err
assert "timestamp out of range" not in captured.err
```

配套的两条经验：

* **修完一处不要收工**：同一机制常在多处实现（那次是 `run.py` 与 `sandbox.py` 各一个看门狗）。
  修完第一个后**重新观察副通道**，而不是重新观察返回值。
* **反面守护**：修 bug 时最容易的过度修法是**把整个机制停掉**（例如为了 `inf` 直接不设超时）。
  所以要有一条测试锁住「机制在正常路径上仍然生效」（`timeout_ms=1.0` + `while(true){}`
  仍须产出 `kind="timeout"`）。

### 失效类偏离的**两个子类**（危险程度不同，别混为一谈）

「照抄上游会出问题」其实分两种，**静默的那种危险得多**：

| 子类 | 表现 | 例子 | 为什么更危险 |
|---|---|---|---|
| **静默失效** | 代码**跑得通**，但功能不再生效 | `runtime.py: is_transient_error()` —— 上游判 `instanceof TypeError`，Python 侧 httpx 抛 **`OSError`** 家族 → **照抄永远返回 false，重试静默失效** | 没有报错、没有日志，只有"重试怎么不работает"；可能几个月后才被发现 |
| **直接报错** | 一跑就抛，立刻可见 | `loadout` 上游是**方法**（`getExposure(name)` / `getNamespace(name)` / `getPromptGuidelines(name)`），本包 `ToolLoadout` 是**映射**（`exposures[name]` / `namespaces[name]` / `prompt_guidelines[name]`）→ **照抄 `AttributeError`**（④-1b 落地时登记） | 第一次运行就暴露，修复成本低 |

**三类并列**（第三种是本轮新增的）：

| 子类 | 表现 | 本地信号 | 例子 |
|---|---|---|---|
| **静默失效** | 跑得通，功能不再生效 | 无 | `is_transient_error()` 的 `TypeError`→`OSError` |
| **直接报错** | 立刻抛 | 有（第一次运行） | `loadout` 方法→映射的 `AttributeError` |
| **本机看不见的直接报错** | 在**别的 Python 版本/平台**才抛 | **零** | f-string 表达式里的反斜杠（3.12+ 才合法，本仓 `requires-python >= 3.11`） |

第三类**最需要文档**，因为本地信号为零。**可操作判据**：

> **凡是用到新版本语法 / 标准库行为的地方，都要核对 `requires-python` 与 CI 矩阵。**
> 具体动作：① 看 `pyproject.toml` 的 `requires-python`；② 看 CI 的 Python 版本矩阵；
> ③ 相信 `ruff` 的 `target-version`（本仓 `py311`）——它报 `invalid-syntax` 时**不要忽略**，
> 那往往就是第三类。

**登记要求**：静默类必须写清「为什么照抄不行 + 等价语义是什么」；报错类写清「等价改写即可」；
第三类写清「在哪个版本/平台上才炸 + 本机为何看不见」。
三类都要登记，但**静默类与第三类要在报告里显式点名**。

## Tier-B④ codemode：批次总结（2026-10-18）

### 一、三类分开列（**已完成 / 判定不做 / 欠账**）

**✅ 已完成**（`create_codemode_extension` 已从包入口导出）

| 块 | 上游 | 本端 |
|---|---|---|
| ④-0 | `settings-defaults.ts`(67) + `settings-schema.ts` 的 `CodemodeMode` | `settings_defaults.py`（完整移植）/ `settings_schema.py`（**类型专用切分**） |
| ④-1a | `tool.ts` 的纯函数 | `tool.py` 的声明渲染 / 描述生成 / 目录预算 |
| ④-1b-1 | `prepareCodemodeLoadout` / `getScriptReach` | `prepare_codemode_loadout` / `get_script_reach` / `CodemodeToolOptions` |
| ④-2a | `execute.ts` 的四个前置 | `usage_totals.py`（**部分移植**）/ `output_files.py`（**共享模块提取**）/ `session_manager.py`（**类型专用切分**）/ `format_size`（**本仓早有**） |
| ④-2a-2 | `execute.ts` 47–439 纯逻辑 | `execute.py` 的 18 个纯函数 |
| ④-2b-2 | `execute.ts` 591–772 两个 globals 工厂 | `create_discovery_globals` / `create_model_globals` |
| ④-2b-1 | `execute.ts` 440–574 `executeCodemode` | `execute_codemode` |
| ④-1b-2 | `tool.ts` 371–410 工具定义 | `create_codemode_tool_definition` / `create_codemode_tool` |
| ④-3 | `index.ts` 1–49 工厂 | `index.py` + `extensions/codemode/__init__.py` + 包入口导出 |

**➖ 判定不做**（按「依赖与消费路径」判定**不该做**，**非欠账**）

| 上游 | 判据 |
|---|---|
| `codemode/renderer.ts`(156) | import `pi-tui` + `render-utils` + `keybinding-hints` + `visual-truncate` + `theme` —— 纯 TUI，本包无渲染宿主 |
| `core/tools/tool-definition-wrapper.ts`(58) | **纯转换层**：唯一调用点 `tool.ts:404`，不传 `ctxFactory`、不做 schema 处理 → 不产出任何本端拿不到的东西 |
| `utils/output-files.ts`(34) | 复用 MCP 先例：提取为 `output_files.py` **共享模块**（不是不做，是**合并**了） |
| `getQuickJSWasmPath` | 本端 `load_quickjs_wasm()` 自带资产解析 → 再写一个是**第二套路径解析**（将来版本漂移） |
| `getCodemodeWorkerSpecifier` + `worker.ts` | JS 打包器/Bun/`data:` URL 矩阵，Python 无对应物；进程隔离由 `SubprocessCodemodeSandbox` 自有 |
| `codemode/cli.ts` / `ui.ts` | CLI/TUI |

**⬜ 欠账**（该做、还没做）

* **无。** codemode 的 SDK 面已全部落地。

### 二、偏离索引（**四类分开**）

**（甲）部分移植** —— 只移被消费的那部分，**模块其余部分未移植**：
`config.py`（`config.ts` ~460 行 → 只有 `get_docs_path`）、`usage_totals.py`（97 → `combine_usage`）、
`core/tools/truncate.ts`(314) 的 `formatSize`（**本仓 `truncate.py` 早有**，无需移植）。
④-2 的 `SessionEntry` 与 `CodemodeMode` 见（乙）。

**（乙）类型专用切分** —— **只取类型，模块其余部分未移植**：
`settings_schema.py`（`CodemodeMode`，出处 `settings-schema.ts:148-150/469`）、
`session_manager.py`（`SessionEntry` 11 变体，无运行期行为）。

**（丙）判定不做**：见上表。

**（丁）失效类** —— 照抄会出问题，**再分三子类**：

| 子类 | 本批实例 |
|---|---|
| **静默失效**（跑得通、功能不再生效） | `is_transient_error` 的 `TypeError`→`OSError`；globals `spread` 被硬编码（**已修**，跨包）；`timeout_ms=inf`（**已修**，跨包） |
| **直接报错** | `loadout` 上游是**方法**、本端是**映射**（`exposures[name]`）→ 照抄 `AttributeError`；钩子签名两参数→一参数 |
| **本机看不见的直接报错** | f-string 表达式里的反斜杠（3.12+ 才合法，本仓 `requires-python >= 3.11`） |

### 三、两次跨包修复（**均为有界授权，授权已用毕**）

| # | 缺口 | 触发 | 影响 | 改动 |
|---|---|---|---|---|
| 1 | `timeout_ms=inf` → 看门狗 `wait(inf)` 抛 `OverflowError`，**抛在线程内、主路径照常返回** | 有消费者传 `inf`（上游用 `POSITIVE_INFINITY` 表示无期限） | 超时保护**静默失效** | `_deadline_seconds()`：`None` 与所有非有限值一视同仁；**两处**看门狗 |
| 2 | globals 的 `spread` 被硬编码 `False` | 有多参全局的消费者（`searchTools` / `models.*`） | 多参全局**静默丢参** | `CodemodeTool.spread` + `_globals_json()` 取字段 |

两次都印证同一条判据：**骨架阶段实现的机制，在第一个真实消费者到来时要重新核对「参数传递 / 边界 / 可选值」是否完整 —— 不只是「能不能跑通」**（两次都是「跑得通，但结果不对」）。

### 四、避免返工的数据

**开 ④ 之前先做依赖勘察**，避免了以下无效移植：

| 若不勘察就会做的事 | 实际省下 |
|---|---|
| 写 `get_quickjs_wasm_path()`（第二套 wasm 路径解析） | ~15 行 + **一个将来才会暴露的版本漂移** |
| 移植 `getCodemodeWorkerSpecifier` + `worker.ts`（JS 运行时矩阵） | ~40 行（且无法忠实） |
| 移植 `tool-definition-wrapper.ts` 三件套 | 58 行 + `ExtensionToolContext` 类型（**只被这三件套需要**，随之自然消失） |
| 移植整套 `config.ts`(~460) / `settings-manager.ts`(1,443) / `session-manager.ts`(2,013) | **≈3,916 行**（按消费面只取 1 函数 / 1 类型 / 1 类型） |
| 移植 `truncate.ts`(314) 全模块 | 313 行（`format_size` 本仓早有） |
| 复制一份 output-file 写入 | 34 行重复 → 提取为共享模块 |
| 为 `spread` 写「单参包装」绕开跨包修复 | 会把**静默失效**变成**文档与实现不符** |
| 给 `pi-codemode` 加 `acquireWorker` 回调 | 配额改为扩展侧 `try/finally`（成本 0） |

**合计避免的无效移植 ≈ 4,300 行**，其中两项（`spread`、`inf`）反过来是**发现的真 bug**。

### 阻塞项（未解决，需决策）

**codemode**（1,396 行，其中 SDK 面约 1,232）：

1. `tool.ts` 在模块顶层 import `@earendil-works/pi-codemode/declarations` 的
   `renderToolOutputType` / `renderToolSample` 与 `/source` 的 `CODEMODE_SOURCE_GRAMMAR`
   —— 而 `pi_codemode` **没有** declarations / source 子模块
   （上游 `packages/codemode/src/declarations.ts` 340 行 + `source.ts` 125 行）。
2. `execute.ts` 依赖 `ctx.executeTool()` 的嵌套工具调用（本包无该基建）、
   `ModelRegistry`（上游 244 行，未移植）、`core/settings-defaults.ts`（未移植）。
3. `renderer.ts` 依赖 TUI（不做）。

**mcp** —— **已解除**（C3b 全部完成，`create_mcp_extension` 已导出）。以下为历史记录：

1. `core/mcp-servers.ts`（346 行，未移植）：`mcpNamespace`、`McpServerConfig`、
   `getMcpToolExposure`、`McpServerRegistry`、资源工具常量等。
2. `core/auth-storage.ts`（507 行，未移植，`mcp/oauth.ts` 需要）、
   `core/resolve-config-value.ts`（287 行，未移植，`mcp/runtime.ts` 需要）。
3. `pi-mcp` 的 `oauth` 子路径**尚未交付**（其 PORTING.md 标注为「第二期」）。
4. `ExtensionAPI` 还缺 7 个成员，`ExtensionContext` 缺 `cwd` / `hasUI` /
   `isProjectTrusted` / `mode` / `modelRegistry` / `signal` / `ui`。

## 有意偏离上游

1. **无 TUI / CLI / session 树**：见上表。
2. **grep / find 不依赖 ripgrep**：上游 `spawn(rg)` + 运行时下载（`utils/tools-manager.ts`）；
   本包用纯 Python `re` + 自研 `.gitignore`（`tools.py: _load_gitignore`，**不递归父目录**）。
   这是**架构选择**，不是版本落后 —— 上游这两个文件在 v0.85.1→v1.3.0 之间一字未改。
3. **`sdk.py` 的 `CodingAgent` 是自研精简版**：上游 `createAgentSession` 返回 `AgentSession`；
   本包返回封装了 `pi_agent_core.Agent` + 7 工具 + 一行默认 prompt 的 `CodingAgent`。
   对齐上游语义是**破坏性变更**，需先定兼容策略。
4. **`render_utils.py` 只含 1 个函数**：上游同文件其余部分是 TUI 渲染辅助（主题着色、
   计时器、`formatToolCallWithArgs`），依赖 `pi-tui`，不复刻。
5. **`DEFAULT_SYSTEM_PROMPT` 为本包自拟**：上游无此常量（其 system prompt 由
   `core/system-prompt.ts` 按工具/上下文动态构建，本包未移植）。
6. **`_bounded_output()` 内联在 `tools.py`**：上游该逻辑在
   `core/tools/output-accumulator.ts: readFullOutput()`，属于 `OutputAccumulator` 流式架构
   （A7 未移植）。本包直接用内存中的完整输出实现同样的截断形状。
7. **`extensions/types.py` 不含渲染字段**：上游 `ToolDefinition` 还有 `renderShell` /
   `renderCall` / `renderResult`（TUI），本包不复刻。
8. **`ToolRegistry` 不做 `--tools` 白名单过滤**：上游 `_isActivatable` 里与 CLI
   `--tools` / `--exclude-tools` 相关的分支属于 CLI 装载逻辑，不在本包范围。

## 未纳入（需用户拍板）

来自 `recon/04-upstream-coding-agent-delta.md` §5 的越界判断，**尚未决策**：

- `src/extensions/codemode`、`src/extensions/mcp`（本次已给出阻塞项清单，见「Tier-B」）
- `core/cache-warmer.ts`（453 LOC，成本优化；前置缺 pi-ai `promptCache`）
- `core/bug-report*.ts` + `core/crash-log.ts`（582 LOC，依赖上游自有 Radius 服务）
- `core/export-html/`、`core/keybindings*.ts`、`extensions/jiti*`

## cherry-pick

（暂无）

## 待办

- [x] `core/tools`（bash / read / edit / write / grep / find / ls）
- [x] v1.3.0 Tier-A 对齐（上表 A1–A6）
- [ ] **版本号与依赖上界**：本包 `__version__` / `pyproject.toml` 仍是 `0.85.1`，依赖写死
      `pi-py-ai>=0.85.1,<0.86` / `pi-py-agent-core>=0.85.1,<0.86`，而 `pi-py-ai` 已升到
      `1.3.0` —— **上界已失效**。需统一决定是否随仓库整体升到 1.3.0
- [ ] `core/agent-session`（AgentSession、session 树、retry、扩展宿主）
- [ ] `core/session-manager`、`core/settings-manager`、`core/resource-loader`
- [ ] `core/compaction`
- [x] `core/extensions` 的**声明面**（`extensions/types.py`）+ 装载语义（`extensions/registry.py`）
- [ ] `core/extensions` 的**宿主**（runner / loader / 事件派发 / boundary / UI 上下文）
- [x] `src/extensions/tool-search/`
- [x] 扩展基建：`ExtensionAPI` 补成员 + `ExtensionContext` + `nested_tool_calls` + `mcp_servers` + `resolve_config_value` + `auth_storage`（窄）+ `model_registry`（仅形状）
- [ ] `src/extensions/codemode/` —— `pi-codemode` 的 declarations/source **已落地**；剩余阻塞见 `model_registry` 跨包项
- [x] `src/extensions/mcp/` —— SDK 面全部完成（`create_mcp_extension` 已导出）。Manager 与 `/mcp` 命令**判定不做**（非欠账，见「Tier-B③」）
- [ ] `model_registry` 具体类型 —— 需 `pi-ai` 提供 `ModelType` / `AnyModel` / `ClassifierModel` / `ImageModel` / `ClassifierContext` / `ClassifierResult` / `ImagesContext` / `AssistantImages`
- [ ] `modes/rpc/`

## 同步历史（本包）

| 日期 | 上游版本 | 说明 |
|---|---|---|
| 2026-07-22 | 0.81.1 | 推翻重建，建立新基线 |
| 2026-07-27 | 0.82.1 | 受约束工具采样、可取消 provider 重试、摘要请求隔离 |
| 2026-07-30 | 0.83.0 | pending/raw stop reason、HTTP client 注入、OpenAI 工具增量修复 |
| 2026-08-12 | 0.84.1 | 破例同步 patch：泛型采样参数、thinking_token_budget 等 |
| 2026-09-07 | 0.85.1 | minor 0.85 + patch：bash 信号退出码、strict schema 转换、并行工具取消等 |
| 2026-10-18 | 1.3.0（Tier-A） | 部分对齐：constrainedSampling 默认开启、bash 退出码/结构化输出、read 结构化输出、`truncate_middle`、`format_read_line_range`；**修正本文件裁剪表** |
| 2026-10-18 | 1.3.0（Tier-B） | 内置扩展 SDK 面：`tool-search` 完整移植（BM25 + `create_tool_search_extension`）、扩展声明面（`extensions/types.py`）与参考宿主（`ToolRegistry`）；codemode/mcp 给出阻塞项清单 |
| 2026-10-18 | 1.3.0（Tier-B④-3 · **批次收尾**） | `create_codemode_extension` **导出**（`index.py` + `extensions/codemode/__init__.py` + 包入口）+ 10 条测试；**两条守卫加严**为「必须可导入且是工厂」；Tier-B④ 收口 + 批次总结（三类分开 / 四类偏离 / 两次跨包修复 / 避免返工 ≈4,300 行） |
| 2026-10-18 | 1.3.0（Tier-B④-1b-2） | `create_codemode_tool_definition` / `create_codemode_tool`（`tool.ts` 371–410）+ 7 条测试：局部 `ToolDefinition` 子类（MCP 先例）、`exposure="model-only"`、`reaches_tools`、`prepare_loadout`、grammar、懒加载 `execute`；`create_codemode_tool` 只换 description**不包层**；新增判据〈测试第一次红时先问夹具是不是在测一个不存在的东西〉（第 3、4 次夹具错误实例） |
| 2026-10-18 | 1.3.0（Tier-B④-2b-1） | `execute_codemode`（`execute.ts` 440–574）+ 11 条测试（含**真实触发 abort** 与桥接断言）；配额**拴在线程结束**上（`shield` + `done_callback`）以消除取消路径下的超限窗口；`run_in_executor` + `abort_event` 桥接；登记「窗口更宽」的既有差异与取消路径的处理 |
| 2026-10-18 | 1.3.0（Tier-B④-2b-2b） | `create_model_globals`（`execute.ts` 664–772）+ 13 条测试；**字段确认走分支 1**（pi-ai 两个结果都有 `stop_reason`/`error_message`，且都含 `stop`/`error`/`aborted` 三态）→ 照上游写、无需偏离；登记 `get_models_of_type` 本仓为 async 的形状偏离，与「中断只能靠 `abort_event`，`to_thread` 不可取消」的机制判据 |
| 2026-10-18 | 1.3.0（Tier-B④-2b-2a） | `create_discovery_globals` + `is_namespace_name`（`execute.ts` 591–657）+ 9 条测试；三个全局 `spread=True`（依赖刚落地的 `pi-codemode` spread 修复）；`searchTools` 的 `limit`/`namespace` 过滤与参数校验、`describe_namespace` 缺席键不写 |
| 2026-10-18 | 1.3.0（Tier-B④-2b-(d)） | `nested_tool_calls`：宿主返回值包成 **`ToolCallOutcome`**（属性可访问映射，复用 pi-mcp 先例）并补上 runner 自己生成的 `tool_call`；**`is_error` 来源不变**（顶回了会造成静默行为变更的改动）；6 条测试含**关联性**断言（outcome id ↔ `tool_execution_start` 事件 ↔ parent）；登记下游可见变更（同一性不再成立）与〈工具会骗你的几种方式〉第 4 条（先 `find` 再判范围） |
| 2026-10-18 | 1.3.0（Tier-B④-2b 前置） | 新增通则〈**修「静默失效」类 bug 时判据不能是返回值**〉：盯副通道（stderr/线程状态/副作用）并做成断言；含 `pi-codemode` 两处看门狗的真实实例、「修完一处不要收工」与「反面守护」两条配套经验 |
| 2026-10-18 | 1.3.0（Tier-B④-2a-2 补记） | 登记 `to_model_info` 丢 `headers` 为**有意的安全取舍**（含代价：按 header 路由的模型在脚本侧不可见）；把〈施工注记〉扩成〈**工具会骗你的几种方式**〉，三条并列（heredoc 三引号 / 源码扫描命中 docstring / 全仓格式化越界），各带真实实例 |
| 2026-10-18 | 1.3.0（Tier-B④-2a-2） | `extensions/codemode/execute.py` 的纯逻辑（`execute.ts` 47–439）+ 43 条测试：预览/校验/格式化/store/图像落盘/输出截断；登记 `AgentToolCallOutcome` 无对应物（`is_error` 落在结果上）与**源码扫描断言要排除 docstring** |
| 2026-10-18 | 1.3.0（Tier-B④-2a） | `execute.ts` 的四个前置：`usage_totals.py`（部分移植 `combine_usage`，可选键「保持缺席」）+ `output_files.py`（**共享模块提取**，MCP 改一行委托）+ `session_manager.py`（类型专用切分 `SessionEntry` 11 变体）+ `format_size` **已存在无需移植**（行为等价已核对）+ 16 条测试；登记 TypedDict 不能收窄父类字段的偏离 |
| 2026-10-18 | 1.3.0（Tier-B④-1b-1） | codemode loadout：`prepare_codemode_loadout` / `get_script_reach` / `CodemodeToolOptions`（共 24 条测试）；**缺口二正式登记为「直接报错」类**（`loadout` 方法→映射）；新增**失效类第三种子类「本机看不见的直接报错」**（f-string 反斜杠在 3.11 非法）并给出可操作判据；查明 **④-1b-2 被 ④-2 阻塞**（`execute` 需要 `execute.ts`，而 `ToolDefinition` 是 ABC） |
| 2026-10-18 | 1.3.0（Tier-B④-1a） | codemode 纯函数：`extensions/codemode/tool.py` 的声明渲染 / 描述生成 / 目录预算选择 + 常量族，16 条测试；`describe_globals` 的 `models` 行有**断言文档存在且非空**的守护（防退回缺口一 (b)）；登记**语法版本静默失效**这一施工坑（f-string 反斜杠在 3.11 非法） |
| 2026-10-18 | 1.3.0（Tier-B④-0b 缺口一） | codemode 缺口一：窄 `config.py`（**部分移植** `getDocsPath`，登记 `<packageDir>` 的 Python 对应物）+ `docs/codemode.md` **逐字节入库**（230 行/14,450 字节，sha256 `a0672912…ddc0`，出处 v1.3.0/MIT）+ 4 条测试（含**「文件存在且非空」**）+ **wheel 自包含验证**（`uv build` 后从解包 wheel import 验证解析成功）；登记**失效类的两个子类**（静默失效 vs 直接报错）与 3 处指向未移植文档的交叉引用 |
| 2026-10-18 | 1.3.0（Tier-B④-0） | codemode 前置：`settings_defaults.py`（忠实移植 67 行，`maxAgentDelayMs` 取自 pi-ai）+ `settings_schema.py` 的 `CodemodeMode` **类型专用切分**（出处 `settings-schema.ts:148-150/469`，勘误：不在 `settings-manager.ts`）+ 6 条测试；登记 **≈4,026 行未移植前置的消费面盘点**、**三类偏离索引（部分移植/类型专用切分/判定不做）**，以及 `tool-definition-wrapper` 的 no-op 判定与 `ExtensionToolContext` 的自然消失 |
| 2026-10-18 | 1.3.0（Tier-B③ C3b-2b-β） | MCP 扩展**收尾**：`factory.py` 含 6 个 `pi.on` 钩子（`index.ts` 1114–1259）+ `reportProblems` + **导出 `create_mcp_extension`** + 20 条测试；两条旧「不留占位桩」守卫改为「codemode 仍不存在 / mcp 必须可导入且是工厂」；登记 β-1/β-2 的**消费路径判定**（均为 Manager/CLI 消费者 → 不做）与钩子签名的失效类偏离 |
| 2026-10-18 | 1.3.0（Tier-B③ C3b-2a） | MCP 扩展 C3b-2a：`registration.py`(258→300) 含工具注册/loadout/资源工具同步/`ensure_discovery_active` + 21 条测试；**删除 `index.ts:382` 的 `registerToolRenderer` 调用**；`ensureDiscoveryActive` 的 UI 告警改为返回文本（无 UI 宿主） |
| 2026-10-18 | 1.3.0（Tier-B③ C3b-1） | MCP 扩展 C3b-1：`index.py`(≈280→320) 含扩展选项、`render_servers_section` 与两个常量、状态判定族、`default_load_config` + 21 条测试；C3b 按 3 刀切（C3b-1/2/3），`/mcp` 命令不做 |
| 2026-10-18 | 1.3.0（Tier-B③ C2b） | MCP 扩展 C2b：`runtime.py`(454→600) 完整移植 + 22 条测试；连接/传输/重连/续期/401 升级全接起来，前置用 C3a 的 `createMcpAuthProvider` |
| 2026-10-18 | 1.3.0（Tier-B③ C3a） | MCP 扩展 C3a：`oauth.py`(558→700) 完整移植 + 30 条测试；复用 pi-mcp 的 `OAuthCallbackServer`（S1–S12）与 pi-ai 的 `oauth_page`，不另起回调服务器；`McpSignInPrompt` 用 Protocol 而非抛 `NotImplementedError` 的类 |
| 2026-10-18 | 1.3.0（Tier-B③ C2a） | MCP 扩展 C2a：`resources.py`(339→400) 完整移植 + 18 条测试；三个资源工具、MCP App 过滤、`_meta`/icons 剥离、`Promise.allSettled`→`gather(return_exceptions=True)` |
| 2026-10-18 | 1.3.0（Tier-B③ C1） | MCP 扩展 C1：`tools.py`(391→438) 完整移植 + 27 条测试；新增 `_to_pi_content` 适配 pi-mcp dict 与 pi-ai 对象的形状差异；C2–C3 未做 |
| 2026-10-18 | 1.3.0（Tier-B③ 基础层） | MCP 扩展：`config.py`(242→305) 与 `log.py`(69→85) 完整移植 + 19 条测试；C1–C3 未做，`create_mcp_extension` 未导出；登记「越界判断双向」判定依据 |
| 2026-10-18 | 1.3.0（Tier-B②） | 扩展基建：`ExtensionAPI` 6 个新成员 + `ExtensionContext`、`nested_tool_calls`(263) 与 `mcp_servers`(346) 与 `resolve_config_value`(287) 完整移植、`auth_storage` 窄接口（含过期锁回收）、`model_registry` 仅形状（pi-ai 跨包阻塞）；偏离逐条登记 |

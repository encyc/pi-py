# 侦察 D：上游 `@earendil-works/pi-coding-agent` 的 SDK(core) 面增量

**范围**：v0.85.1（2026-09-05）→ v1.3.0（2026-10-15）
**对象**：上游 `packages/coding-agent/src/core/`，对照 pi-py `packages/pi-coding-agent/`
**作者**：teammate `upstream-coding-agent`（task-4）
**证据基线**（只读）：
- `/tmp/pi-up/v0.85.1/packages/coding-agent/`
- `/tmp/pi-up/v1.3.0/packages/coding-agent/`
- `/Users/mac/pi-py/packages/pi-coding-agent/`
- 包级 CHANGELOG：`/tmp/pi-up/v1.3.0/packages/coding-agent/CHANGELOG.md`

> 本报告所有结论都附 `文件:行号` 或 `CHANGELOG 版本号`。凡未能回到源码验证的，一律标注「未验证」。

---

## 0. 结论摘要（先看这 10 条）

1. **pi-py 的 `pi-coding-agent` 不是「core 的部分移植」，而是一个独立的精简 SDK。** 实测源码只有 **4 个 .py / 1263 LOC**（`__init__.py` 73 + `sdk.py` 139 + `tools.py` 847 + `truncate.py` 204），对应上游 core 的 **29717 LOC TS**——覆盖率约 **4%**。因此「追赶」的主问题不是「diff 已移植文件」，而是**决定 core 里哪些能力值得补**。
2. **`PORTING.md` 的裁剪表与代码严重不一致**：表格声称 `src/extensions/`、`src/utils/`、`src/modes/rpc/` 均已复刻（`PORTING.md:12-14`），且「待办」清单（`PORTING.md:44-51`）里 6 项有 5 项未做；实际代码里这些目录**一行都没有**。参见 §2.3。
3. **core/ 权威增量（`diff -rq`）**：89 → 103 个文件；**added 14 / removed 0 / modified 54 / identical 35**。零删除是本轮最省心的结论。参见 §3.1。
4. **最大的新增面不是 core，而是 `src/extensions/`**：codemode(1396) + mcp(4257) + tool-search(262) 三个内置扩展在 0.99.0 新增，让 `src/extensions/` 从 1457 → 7506 LOC，并且**它们被 re-export 进了 SDK 包入口 `src/index.ts`**。这意味着 v1.3.0 的「SDK 面」已经越出 `src/core/` 边界——只盯 core 会漏掉本轮最大的能力增量。
5. **SDK 包入口 `src/index.ts` 的导出符号：383 → 461，新增 78 个 / 删除 0 个**（集合差实测）。对 SDK 消费者最重的是：`CacheWarmingDecision/Status`、`ContextEditEntry`/`SessionProjection`/`buildSessionProjection`、5 个新扩展事件、`ToolExposure`/`ToolLoadout`/`executeTool()`、`VirtualModel*`、`createCodemodeExtension`/`createMcpExtension`/`createToolSearchExtension`。**零删除意味着从包入口看这是一次纯增量升级**——破坏性全部落在行为与子路径上（见 §4.2）。
6. **CHANGELOG 在窗口内只有 4 个 `### Breaking Changes` 节**：1.2.0（4 条）、1.0.3（1 条）、0.87.0（5 条）、0.86.0（3 条），共 **13 条**；其中真正落到 core 的 **10 条**，纯 CLI/TUI 的 **1 条**，继承自 pi-ai/pi-agent-core 的 **2 条**。参见 §4.1。
7. **版本号有跳跃**：`0.87.1` → `0.99.0`，`packages/ai/CHANGELOG.md` 同样跳过 0.88–0.98。已确认全仓无 0.88–0.98 记录（`grep -c '^## \[0\.9[0-8]' CHANGELOG.md` = 0），**不是数据缺失，是编号跳跃**。
8. **pi-py 的 7 个工具全部落后 1–2 个语义变更**：最值得注意的是上游把内置工具的 `constrainedSampling` 从 `PI_EXPERIMENTAL` 门控改成**默认开启** `{type:"json_schema",strict:"prefer"}`，并删除了 `getExperimentalToolSampling()`。这直接改变模型侧看到的工具 schema。参见 §5 Tier-A。
9. **过半新增 core 模块依赖 pi-py 目前不存在的前置能力**，最硬的三个是：`SystemMessage.sections`（多段 system prompt）、`AgentTool.outputSchema`/`structuredContent`、`Agent.finishTurn`/`prepareRequest`。这些都在 pi-py 的 **pi-ai / pi-agent-core** 包，不在 coding-agent 包内——本报告把它们列为**跨包阻塞项**，不擅自扩范围。参见 §5.4。
10. **`pi_coding_agent.CodingAgent` 是 `pi_server` 的运行时依赖**（`packages/pi-server/src/pi_server/supervisor.py:23,92`）。本包任何公开 API 变更都必须考虑 pi-server 的兼容性。

---

## 1. 方法与证据

| 步骤 | 命令 / 文件 | 用途 |
|---|---|---|
| 1 | `read packages/pi-coding-agent/{PORTING.md,README.md,pyproject.toml}` + 4 个源文件 + 3 个测试文件 | 确定**实际**已移植面（不采信 README） |
| 2 | `diff -rq v0.85.1/.../src/core v1.3.0/.../src/core` | core/ added/removed/modified 权威清单 |
| 3 | 逐文件 `diff -u` + 行数统计 | modified 分档与改动量 |
| 4 | `awk` 切 `CHANGELOG.md` 的 `## [版本]` / `### Breaking Changes` 节 | 定位窗口内 Breaking（仅 4 节：line 59 / 194 / 483 / 545） |
| 5 | 回到 `v1.3.0/.../src/**` 逐个核对 | Breaking 的 before/after 与迁移动作 |
| 6 | 提符号：`export (interface\|type\|const\|function\|class)` × 两版 × core 全树，做集合差 | 「新增 API」不漏、不臆造 |
| 7 | 对照 `src/index.ts`（包入口）diff | 区分「模块内 export」与「SDK 公开 API」 |

**关于包入口**：`package.json` 的 `"exports": {".": "./dist/index.js"}`，所以 **SDK 公开面 = `src/index.ts`**，不是 `src/core/index.ts`。两者不同（`src/core/index.ts` 只导出 97 行、以 sessions/extensions 为主）。

**上游版本标签确认**：
```
v0.85.1/packages/coding-agent/package.json:  "version": "0.85.1"
v1.3.0/packages/coding-agent/package.json:   "version": "1.3.0"
```

---

## 2. pi-py 已移植面（基于源码）

### 2.1 实际存在的文件与规模

```
packages/pi-coding-agent/
├── PORTING.md                 51 行   移植注记（部分内容与代码不符，见 §2.3）
├── README.md                  42 行
├── pyproject.toml             38 行   deps: pi-py-ai, pi-py-agent-core, pydantic, pyyaml
├── src/pi_coding_agent/
│   ├── __init__.py            73 行   公开导出面
│   ├── sdk.py                139 行   CodingAgent / create_agent_session / DEFAULT_SYSTEM_PROMPT
│   ├── tools.py              847 行   7 个工具
│   └── truncate.py           204 行   截断函数
└── tests/{test_tools.py 352, test_integration.py 124, test_smoke.py 8}
```
合计源码 **1263 LOC**（`wc -l` 实测）。

版本自述：`src/pi_coding_agent/__init__.py:15-16`
```python
__version__ = "0.85.1"
__upstream_ref__ = "earendil-works/pi@v0.85.1"
```

### 2.2 逐文件 → 上游路径映射

| pi-py | LOC | 上游对应 | 覆盖程度 |
|---|---:|---|---|
| `tools.py:BashTool` (59-171) | 113 | `core/tools/bash.ts`（401 → 442 行） | **部分**。`execution_mode="sequential"`（`tools.py:64`）对齐；信号退出码 → `128+信号` 对齐（见 §5 A4）；**无** `outputSchema`/`structuredContent`/`constrainedSampling`/`OutputAccumulator` |
| `tools.py:ReadTool` (185-292) | 108 | `core/tools/read.ts`（198 → 224 行） | **部分**。文本+图片、offset/limit、head 截断、图片扩展名硬编码白名单（`tools.py:182`）；**无** `inputLimits.images.resize`、无 `outputSchema` |
| `tools.py:EditTool` (311-412) | 102 | `core/tools/edit.ts`（221 → 220 行） | **部分**。exact-text replace + 唯一性检查；**实现自研**（上游用 `edit-diff.ts` 的 `applyEditsToNormalizedContent`） |
| `tools.py:WriteTool` (421-464) | 44 | `core/tools/write.ts`（98 → 97 行） | **部分**。**无** `withFileMutationQueue`（上游 `tools/write.ts:9` 用它串行化写） |
| `tools.py:GrepTool` (494-634) | 141 | `core/tools/grep.ts`（323 行，**两版未变**） | **架构性偏离**。上游 `spawn(rg)` + `ensureTool("rg")`（`grep.ts:4,119-121`）；pi-py 用纯 Python `re` + 自研 `.gitignore`（`tools.py:475-491`，**不递归父目录**，file docstring 自认「简单版」） |
| `tools.py:FindTool` (646-723) | 78 | `core/tools/find.ts`（318 行，**两版未变**） | **架构性偏离**。上游 `spawn(rg)`；pi-py `os.walk` + 硬编码目录黑名单（`tools.py:685`：`.git/node_modules/__pycache__/.venv`） |
| `tools.py:LsTool` (735-810) | 76 | `core/tools/ls.ts`（175 行，**两版未变**） | **较完整**（上游本就是纯 fs） |
| `truncate.py` | 204 | `core/tools/truncate.ts`（276 → 314 行） | **部分**。`DEFAULT_MAX_LINES=2000`/`DEFAULT_MAX_BYTES=50*1024`/`GREP_MAX_LINE_LENGTH=500` 对齐（`truncate.py:15-17`）；**缺** v1.3.0 新增的 `truncateMiddle()`（见 §3.2/Tier-A A3） |
| `sdk.py` | 139 | `core/sdk.ts`（410 → 487 行）+ `core/agent-session.ts`（3524 → 4428 行） | **大幅精简**。`create_agent_session`（`sdk.py:126-136`）不是上游 `createAgentSession` 的移植，而是把 `pi_agent_core.Agent` + 7 工具 + 一行默认 prompt 拼起来；`DEFAULT_SYSTEM_PROMPT`（`sdk.py:23-27`）是 pi-py 自拟，**上游无此常量** |
| `__init__.py` | 73 | `src/index.ts`（461 个导出符号） | 公开 **23** 个符号（`__all__`，`__init__.py:46-73`），其中上游同名只有 `createAgentSession` 的语义对应物 |

**测试覆盖**（作为「已移植面」的旁证）：
`tests/test_tools.py` 覆盖 7 个工具 + 4 个截断函数 + 信号退出码映射（`test_bash_signal_exit_code_mapped`，line 346）；`tests/test_integration.py` 4 个真实 LLM 用例（read/write/edit/bash+ls，默认 `-m integration` 跳过）。

### 2.3 ⚠️ `PORTING.md` 与代码的不一致（重要发现，影响排期）

`PORTING.md:9-19` 的裁剪表声称：

| 上游部分 | 表格结论 | **代码事实** |
|---|---|---|
| `src/core/`（~27k） | ✅ 复刻 | ⚠️ 仅 4%/1263 LOC，且是自研精简版 |
| `src/modes/rpc/`（~1.6k） | ✅ 复刻 | ❌ **未复刻**。`packages/pi-server`（646 LOC）是自研 supervisor+IPC，不是 `modes/rpc` 的移植 |
| `src/extensions/`（~3k） | ✅ 复刻 | ❌ **未复刻**（0 行） |
| `src/utils/`（~3k） | ✅ 复刻 | ❌ **未复刻**（0 行） |

`PORTING.md:44-51` 的「待办」6 项中，**只有 core/tools 一项算完成**，其余 5 项（agent-session / extensions / session-manager+settings-manager+resource-loader / compaction / modes/rpc）均未开始。

> **给 Lead 的建议**：`PORTING.md` 的表格读起来像「进度快照」，但实际是「原始计划」。追赶排期前应先修这张表，否则会严重低估工作量。

### 2.4 跨包前置能力缺口（阻塞项，非本包范围）

上游 core 的多数新能力依赖 `@earendil-works/pi-ai` / `@earendil-works/pi-agent-core`。实测 pi-py 对应包**没有**这些设施：

| 上游能力 | 上游证据 | pi-py 现状 | 影响 |
|---|---|---|---|
| `SystemMessage` + `sections`（多段 system prompt） | `core/compaction/compaction.ts` 新增 `case "system"`；`core/system-prompt.ts:57-63` `SystemPromptSections`；`src/index.ts` 导出 `SystemMessage` | `pi-ai/src/pi_ai/types.py:318` 注释原文：「system prompt 是普通字符串字段（**无 SystemMessage**）」；`pi_agent_core/types.py:109-114` `AgentContext.system_prompt: str` | 阻塞 `context_with_system`、`ContextEditEntry`、`buildSessionProjection`、per-model 图片限制的 prompt 侧 |
| `AgentTool.outputSchema` / `structuredContent` / `isError` / `usage` | `core/tools/bash.ts` 新增 `bashOutputSchema`；`core/tools/read.ts` 新增 `readOutputSchema`；`core/tools/tool-definition-wrapper.ts:17` 传 `outputSchema` | `pi_agent_core/types.py:74-92` `AgentTool` 只有 `name/description/parameters/label/execution_mode`；`AgentToolResult`(`types.py:56-67`) 只有 `content/details/added_tool_names/terminate` | 阻塞 codemode 结构化返回、nested-call usage 统计 |
| `Agent.finishTurn` / `prepareRequest` / `nestedCalls` | CHANGELOG 0.87.0 Breaking；`core/agent-session.ts` 新增 `this.agent.prepareRequest` / `this.agent.finishTurn` | `pi_agent_core/agent.py` 无 `finish_turn`/`prepare_request`（`grep -rn "finishTurn\|prepare_request"` 无命中） | 阻塞 boundary 事件（`turn_end` result、`agent_before_settle`） |
| `TranscriptContext` / `normalizeContext` / `getCurrentSystemMessage` / `getCurrentTools` | CHANGELOG 0.86.0 Breaking；`core/compaction/compaction.ts:19-22` | `pi-ai` 无对应（`grep -rn "normalize_context\|TranscriptContext"` 无命中） | 阻塞 provider 侧 context 规范化 |
| `model.promptCache` / `calculateCost` / `getProviderEnvValue` | `core/cache-warmer.ts:1-11, 40-52` | pi-ai 仅 provider 侧出现 `prompt_cache`（`pi-ai/src/pi_ai/providers/openai_provider.py:297-299`），未见 `Model.promptCache` 字段（**未逐字段核对**，标「未验证」） | 阻塞 cache-warmer |
| `lazyStream` / `isModelType` / `ModelType` | `core/virtual-models.ts:14-27` | `grep` 无命中 | 阻塞 virtual-models |

> 这些属于 pi-ai / pi-agent-core 两条线（其他 task），本报告只登记**作为 coding-agent core 追赶的前置条件**，不做工作量估算。

---

## 3. core/ 模块级增量（`diff -rq` 权威清单）

### 3.1 总览

```bash
$ diff -rq /tmp/pi-up/v0.85.1/packages/coding-agent/src/core \
           /tmp/pi-up/v1.3.0/packages/coding-agent/src/core
```

| 类别 | 数量 |
|---|---:|
| v0.85.1 文件总数 | 89 |
| **identical** | **35** |
| **modified** | **54** |
| **added（`Only in v1.3.0`）** | **14** |
| **removed（`Only in v0.85.1`）** | **0** |
| v1.3.0 文件总数 | 103 |
| core TS LOC | 29,717 → **35,514**（+5,797，+19.5%） |

### 3.2 ADDED —— 14 个新模块

| # | 文件 | LOC | 职责（源码依据） | SDK 面判定 |
|---|---|---:|---|---|
| N1 | `cache-warmer.ts` | 453 | 用「1 token 输出上限」重放请求保活 prompt cache；含成本模型（`CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS=0.05`）、`CacheWarmer` 类、`getPromptCacheTtlMs()`、`isReplayable()`、`formatCacheWarmingStatus()` | ✅ **SDK**（成本优化；被 `core/sdk.ts` 直接 new） |
| N2 | `nested-tool-calls.ts` | 263 | `ctx.executeTool()` 的宿主：`NestedCallRecorder`（限额 `maxCalls=256`/`maxArgumentBytesPerCall=8KiB`）、`NestedToolCallRunner`、`NestedToolExecutionEvent`（带 `parentToolCallId`） | ✅ **SDK**（codemode 的基础设施） |
| N3 | `mcp-servers.ts` | 346 | **只做**配置校验与注册：`validateMcpServerConfig()`、`McpServerRegistry`、`McpExposure`、`createToolNameMatcher()`、`isMcpToolName()`、三个 resource 工具常量。**连接逻辑不在这里**（`src/extensions/mcp`） | ⚠️ **半 SDK**（单独移植无消费者） |
| N4 | `virtual-models.ts` | 238 | 虚拟模型：`VIRTUAL_MODEL_API="pi-virtual"`、`ModelRouteRequest/ModelRoute`、`createVirtualModel()`、`withVirtualModels()`、`getBranchSelection()`、`VIRTUAL_MODEL_STATE_ENTRY="pi.virtual-model-state"` | ✅ **SDK** |
| N5 | `bug-report.ts` | 375 | `/bug` 的数据收集 + 脱敏（`redactUrl`/`redactJsonValue`/`SENSITIVE_KEY` 正则）+ zip 打包（`writeBugReportArchive`） | ❌ **越界**（产品运营功能，依赖 Radius 私有服务） |
| N6 | `bug-report-upload.ts` | 37 | multipart 上传到 `getRadiusGatewayUrl() + /v1/bug-reports`（`bug-report-upload.ts:20`） | ❌ **越界** |
| N7 | `crash-log.ts` | 170 | `~/.pi/agent/crashes.json` 读写、最多 5 条 / 7 天、`findExtensionStackMatches()`（从 stack 反查扩展） | ⚠️ **边缘**（本地诊断，无 /bug 则无消费者） |
| N8 | `settings-schema.ts` | 482 | `Settings` 的 TypeBox JSON Schema（发布到 `pi.dev/schemas/`）；`Settings` 接口从 `settings-manager.ts` **搬迁到这里** | ✅ **SDK**（settings 契约 + 编辑器校验） |
| N9 | `settings-defaults.ts` | 67 | `SETTINGS_DEFAULTS` 常量表，供 accessor 与 JSON Schema 共用 | ✅ **SDK** |
| N10 | `keybindings-schema.ts` | 21 | 由 `KEYBINDINGS` 生成 keybindings.json schema | ❌ **越界**（TUI） |
| N11 | `key-id-schema.ts` | 21 | 键位 id 正则 + `KeybindingValueSchema`，依赖 `@earendil-works/pi-tui` 的 `Key` | ❌ **越界**（TUI） |
| N12 | `extensions/virtual-modules.ts` | 38 | `VIRTUAL_MODULES`：给扩展注入 `typebox`/`pi-ai`/`pi-agent-core`/`pi-tui`/`pi-coding-agent` 等模块 | ❌ **越界**（Node/Bun 打包机制，Python 无对应） |
| N13 | `extensions/jiti-loader.ts` | 3 | `export { createJiti } from "jiti";` | ❌ **越界**（TS 运行时转译） |
| N14 | `extensions/jiti-static-loader.ts` | 3 | `export { createJiti } from "jiti/static";`（Bun/SEA 打包用） | ❌ **越界** |

**统计**：14 个新增模块合计 **2,517 LOC**。按 §5 判定分类：
- ✅ SDK 明确：**5 个 / 1,503 LOC**（N1 cache-warmer、N2 nested-tool-calls、N4 virtual-models、N8 settings-schema、N9 settings-defaults）
- ❌ 越界：**7 个 / 498 LOC**（N5、N6、N10、N11、N12、N13、N14）
- ⚠️ 半 SDK / 边缘：**2 个 / 516 LOC**（N3 mcp-servers、N7 crash-log）

### 3.3 REMOVED —— **0 个**

`diff -rq` 无任何 `Only in v0.85.1` 行。**core/ 没有删除任何文件**。
（唯一的「符号级删除」是两个**模块内 export**，不在包公开面：`experimental.ts` 的 `getExperimentalToolSampling()`，`model-resolver.ts` 的 `parseModelPattern()`/`ParsedModelResult`。见 §4.2。）

### 3.4 MODIFIED —— 54 个文件（按改动量排序）

**分档 A：与 pi-py 已移植面直接相关（10 个）**

| 文件 | ±行 | 关键变化 |
|---|---:|---|
| `tools/render-utils.ts` | 100 | 新增 `formatReadLineRange()`(:38)、`formatToolCallWithArgs()`(:97)、`formatToolDuration()`(:118)、`startToolTimer()`(:138)、`toolTimerText()`；**其中只有 `formatReadLineRange()` 被 `src/index.ts` 导出**，其余 4 个仅模块内 export（渲染层） |
| `bash-executor.ts` | 100 | 抽出 `appendText()`/`flushOutput()`；**修复跨 chunk ANSI 转义被截断**（`splitIncompleteAnsiSuffix`，CHANGELOG 1.1.0 #10504）；改用 `createOutputFileStream` |
| `tools/bash.ts` | 55 | 新增 `bashOutputSchema`/`BashToolOutput`（`output/truncated/full_output_path/exit_code/wall_time_seconds`）；`constrainedSampling` 默认开启；`exitCode===null` → `isError` + 文案；`wall_time_seconds` |
| `tools/output-accumulator.ts` | 53 | 新增 `readFullOutput(maxBytes)`（保留首尾各 maxBytes/2，中间插省略标记）；改用 `createOutputFileStream` |
| `tools/truncate.ts` | 38 | 新增 `truncateMiddle()` + `MiddleTruncationResult`（`…N chars truncated…` 标记，按 UTF-8 字符边界切） |
| `tools/read.ts` | 38 | 新增 `readOutputSchema`/`ReadToolOutput`；`ReadToolOptions.resizeOptions`；`ctx.model.inputLimits.images.resize ?? fallbackResizeOptions`；返回 `structuredContent` |
| `tools/tool-definition-wrapper.ts` | 21 | `ctxFactory` 签名 `() => ExtensionContext` → `(toolCallId, signal) => ExtensionToolContext`；透传 `outputSchema` |
| `experimental.ts` | 6 | **删除** `getExperimentalToolSampling()`（9 → 3 行），只留 `areExperimentalFeaturesEnabled()` |
| `tools/write.ts` | 3 | 仅 `constrainedSampling` 默认值 |
| `tools/edit.ts` | 3 | 仅 `constrainedSampling` 默认值 |

**分档 B：TUI/HTML 导出/CLI 相邻（8 个，判定越界）**

`tools/renderers/bash.ts`(96)、`export-html/template.js`(127)、`keybindings.ts`(116)、`tools/renderers/edit.ts`(20)、`tools/renderers/read.ts`(18)、`export-html/tool-renderer.ts`(14)、`export-html/template.css`(10)、`tools/renderers/index.ts`(4)

> `tools/renderers/index.ts` 的 4 行 diff 只是把本地 `export type ToolRenderers = Pick<ToolDefinition,...>` 改为 `export type { ToolRenderers }` 从 `extensions/types.ts` 转发（`extensions/types.ts:672` 现为 `Pick<AnyToolDefinition,"renderShell"|"renderCall"|"renderResult">`，多了 `renderShell`）。**不是删除**。

**分档 C：pi-py 完全未移植的 core 面（36 个）**

| 子系统 | 文件（±行） | 一句话 |
|---|---|---|
| AgentSession | `agent-session.ts`(1962)、`agent-session-runtime.ts`(6)、`agent-session-services.ts`(12) | 最大改动：`prepareRequest`/`finishTurn`/`_runBeforeSettleBoundary`/`refreshContext()`/`setCacheWarmingMode()`/`getCallableToolNames()`/`summarizeForBugReport()` |
| Session | `session-manager.ts`(567)、`session-export.ts`(43)、`messages.ts`(1) | `ContextEditEntry` + `buildSessionProjection` + `appendUsage`/`appendContextEdit`；`messages.ts` 只是新增 `case "system"` |
| Settings | `settings-manager.ts`(438)、`auth-storage.ts`(37) | `Settings` 类型迁出；加 `cacheWarming`/`sessionShareDestination`/`deviceId`/`compaction.modelOverrides`/`quietStartup:"header"`/`retry.maxAgentDelayMs` |
| 扩展系统 | `extensions/types.ts`(653)、`extensions/runner.ts`(449)、`extensions/loader.ts`(190)、`extensions/index.ts`(32)、`extensions/wrapper.ts`(26) | 见 §4.2；`wrapper.ts` 改为经 `runner.createToolContext(toolCallId, signal)` |
| 模型 | `model-resolver.ts`(750)、`model-runtime.ts`(360)、`model-config.ts`(210)、`model-registry.ts`(87)、`provider-composer.ts`(287)、`remote-catalog-provider.ts`(193)、`radius.ts`(10) | `resolveModelReference()` 取代 `parseModelPattern`；`ModelRuntime` 加 `generateImages()`/`classify()`/`getAllModels()`；多模型类型（chat/image/classifier） |
| 资源/包管理 | `resource-loader.ts`(290)、`package-manager.ts`(97)、`prompt-templates.ts`(116)、`skills.ts`(9)、`slash-commands.ts`(3)、`source-info.ts`(17) | `isBuiltinExtension()`、`builtin:` 前缀（`BUILTIN_PATH_PREFIX`）、`/bug` slash 命令、`formatSkillsForPrompt` 支持 `"indirect"` |
| 压缩 | `compaction/compaction.ts`(295)、`compaction/utils.ts`(55)、`compaction/branch-summarization.ts`(4) | `estimateProjectedContextTokens()`；`serializeConversation()` **不再包含 thinking**；`extractFileOpsFromMessage` 读 `nestedCalls` |
| SDK/系统提示 | `sdk.ts`(209)、`system-prompt.ts`(295) | 见 §5 Tier-A/B |
| 其他 | `usage-totals.ts`(29)、`cache-stats.ts`(12)、`trust-manager.ts`(1)、`project-trust.ts`(3)、`index.ts`(17) | `combineUsage()`；`usage` 条目参与 cache 统计；`mcp.json` 加入需信任资源 |

**分档 D：完全未变（35 个，节选——与工具最相关）**

`tools/grep.ts`、`tools/find.ts`、`tools/ls.ts`、`tools/path-utils.ts`、`tools/index.ts`、`tools/edit-diff.ts`、`tools/file-mutation-queue.ts`、`tools/powershell.ts`、`tools/renderers/{find,grep,ls,write}.ts`、`export-html/index.ts`、`models-store.ts`、`http-dispatcher.ts`、`output-guard.ts`、`exec.ts`、`provider-attribution.ts`、`session-cwd.ts`、`runtime-credentials.ts`、`timings.ts`、`telemetry.ts`、`event-bus.ts`、`diagnostics.ts`、`defaults.ts`、`compaction/index.ts`、`footer-data-provider.ts`、`resolve-config-value.ts` 等。

> **对 pi-py 的直接含义**：grep/find/ls 三个工具的上游实现**两个版本一字未改**（`cmp` 确认）。pi-py 在这三个工具上的所有偏离（纯 Python 替代 ripgrep、自研 gitignore）都**不是版本落后**，而是**主动架构选择**——追赶排期不应把它们算作「落后」。

---

## 4. SDK 相关破坏性变更

### 4.1 CHANGELOG `### Breaking Changes` 全量（窗口内 4 节 / 13 条）

窗口内只有 4 个 `### Breaking Changes` 节（`awk` 定位：line 59=1.2.0、194=1.0.3、483=0.87.0、545=0.86.0）。

#### 4.1.1 `[1.2.0] - 2026-10-12`（4 条，CHANGELOG:59-64）

| # | 原文摘要 | 面 | 判定 |
|---|---|---|---|
| B1 | 发布生成式配置 schema 到 `https://pi.dev/schemas/*.schema.json`；theme schema 移到 `theme.schema.json`；启动期加载的 theme 也接受 schema 校验。要求：更新 theme 的 `$schema`、把可复用颜色放到 `vars`、移除不支持的元数据 | core + TUI | **core 部分相关** → 落到 N8/N9/N10/N11。pi-py 无 theme 概念，只有 `settings.json` 契约可考虑对齐 |
| B2 | `before_agent_start` 改为按 `messageType` 的联合类型：`"user"` 事件带 `prompt`/`images`；`"custom"` 事件带 `message`、**无 `prompt`**。只处理用户 prompt 的 handler 必须判 `event.messageType === "user"` | core/extensions | **相关**（若移植扩展系统）。源码：`extensions/types.ts` 新增 `BeforeAgentStartUserEvent`/`BeforeAgentStartCustomEvent`（`src/index.ts` 已导出） |
| B3 | 会话在 run 启动期间即算 busy（含 `before_agent_start` handler 执行期间）：`ctx.isIdle()` 返回 false；`pi.sendMessage()` 改为**排队进正在启动的 run**（而不是起第二个 run 并报「Agent is already processing」）；`pi.sendUserMessage()` **必须带 `deliverAs`**；此期间请求 abort 会在 run 启动后中止它 | core/agent-session + extensions | **相关**（行为性破坏，无类型报错，最易漏） |
| B4 | `pi mcp list --json` 对提供 resources 的服务器报 `resources: true`，不再报 `resources`/`resourceTemplates` 计数；连接时不再列举 resources | **CLI** | **越界**（唯一纯 CLI 条） |

#### 4.1.2 `[1.0.3] - 2026-10-05`（1 条，CHANGELOG:194-196）

| # | 原文摘要 | 面 | 判定 |
|---|---|---|---|
| B5 | Azure provider 从 `azure-openai-responses` **重命名为 `azure`**。需改 `auth.json` 的 provider key（或重跑 `/login`）、`models.json`、`settings.json`（`defaultProvider`、`enabledModels` 模式、`modelThinkingLevels` 键）。**用旧 provider 的会话恢复时回退到别的模型，prompt cache 不复用**。`AZURE_OPENAI_*` 环境变量不变 | **core** | **相关**。源码确认：`core/model-resolver.ts:25` 的 `defaultModelPerProvider` 键已从 `"azure-openai-responses"` 改为 `azure`（值仍是 `gpt-5.4`） |

#### 4.1.3 `[0.87.0] - 2026-09-21`（5 条，CHANGELOG:483-489）——**本轮最重的 SDK 破坏**

| # | 原文摘要 | 面 | before → after / 迁移动作 |
|---|---|---|---|
| B6 | **删除继承的 `shouldStopAfterTurn` agent 选项**。改用 `finishTurn` 并返回 `{ action: "end" }`。`finishTurn` 在 `turn_end` 前运行但决策在之后生效，且**也会收到 error 与 aborted 响应**；迁移普通响应判定时应对这两类硬退出返回 `undefined` | pi-agent-core（继承）+ core | `shouldStopAfterTurn` → `finishTurn`。源码：`core/agent-session.ts` 新增 `this.agent.finishTurn = async (turn, signal) => {...}` + `_dispatchTurnEndBoundary()` |
| B7 | **`ContextEditEntry` 加入导出的 `SessionEntry` 联合**。做穷尽 switch 的 TS 消费者必须处理 `context_edit`；`replacement: null` 表示省略（omission），否则是内容替换 | **core** | 源码：`core/session-manager.ts` 新增 `ContextEditEntry`/`ContextEditableContent`/`ProjectedSessionEntry`/`SessionProjection`，新增 `appendContextEdit(targetId, replacement)`；`compaction` 改为走 `buildSessionProjection()` |
| B8 | **`SessionManager` 成为 `AgentSession` provider context 的唯一权威**。直接赋值 `session.agent.state.messages` **不再**影响后续请求历史。恢复方式：`SessionManager.inMemory(cwd, { id }, entries)` / `session.navigateTree()` / 通过 `session.sessionManager` 追加后调 `session.refreshContext()` | **core** | 源码：`core/agent-session.ts` 新增 `refreshContext()`；`core/sdk.ts` 改为把 `existingSession.messages` 放进 `new Agent({initialState:{messages}})` 而非事后赋值；`core/agent-session-runtime.ts:256` 用 `this.session.refreshContext()` 取代 `agent.state.messages = ...` |
| B9 | **`TurnEndEvent` 扩展出必填 boundary 字段**，并新增 `AgentBeforeSettleEvent` 到导出的 `ExtensionEvent` 联合。构造事件或对 `ExtensionEvent` 穷尽 switch 的消费者必须处理新形状。**`ExtensionRunner.emit()` 不再接受 `turn_end`**；宿主集成改用 `emitBoundary(baseEvent, buildContext)` | **core/extensions** | 源码：`extensions/types.ts` 新增 `BoundaryContextPreview`/`BoundaryState`/`BoundaryResult`/`SessionBoundaryDraft`/`CompactionEntryDraft`/`CustomEntryDraft`/`CustomMessageEntryDraft`/`ContextEditEntryDraft`/`TurnEndEventResult`/`AgentBeforeSettleEvent(Result)`；`extensions/runner.ts` 新增 `BeforeAgentStartTrigger` |
| B10 | **`agent_settled` handler 里请求的延迟 run 推迟到所有 settled handler 完成后**。handler 仍看到 `ctx.isIdle() === true`，但不再在同一次通知派发中看到重入的 `agent_start` | **core/agent-session** | 源码：`core/agent-session.ts` 新增 `_deferredSettledActions` 队列 + `_runBeforeSettleBoundary()` |

#### 4.1.4 `[0.86.0] - 2026-09-19`（3 条，CHANGELOG:545-549）

| # | 原文摘要 | 面 | 判定 |
|---|---|---|---|
| B11 | **继承的 pi-ai provider stream 输入从 `Context` 改为规范化后的 `TranscriptContext`**。自定义 provider 必须用 `getCurrentSystemPrompt()` / `getCurrentTools()` 从 `context.messages` 读 system prompt 与工具声明 | **pi-ai（继承）** | **越界到 pi-ai 线**。源码证据：`core/compaction/compaction.ts` 改用 `normalizeContext()` + `getCurrentSystemMessage()`；`core/compaction/branch-summarization.ts:351` 同样改用 `normalizeContext({...})` |
| B12 | 继承的 `ToolCall.arguments` 与 `ToolResultMessage.details` 收紧为 JSON 兼容值；`ToolResultMessage` 变为条件类型；`JsonValue` 数组变 readonly | **pi-ai/pi-agent-core（继承）** | **越界到下游包线** |
| B13 | **`user_bash` 改为 fail-closed**：错误或无效的 defined 结果会**中止命令**，不调用后续 handler、也不本地执行。返回 `undefined` 继续传播，否则返回 `{ operations }` 或 `{ result }` | **core/extensions + bash-executor** | **相关**。pi-py 目前无 `user_bash` 概念 |

#### 4.1.5 分类小结

| 判定 | 条数 | 编号 |
|---|---:|---|
| **core 相关**（影响 core 公开 API/行为） | **10** | B1(部分)、B2、B3、B5、B6、B7、B8、B9、B10、B13 |
| **纯 CLI/TUI** | **1** | B4 |
| **继承自 pi-ai/pi-agent-core**（不在 coding-agent 包内） | **2** | B11、B12 |

### 4.2 非 Breaking 但**确实破坏 SDK 消费面**的变更

CHANGELOG 里这些写在 `### Added`/`### Removed`/`### Changed` 下，但对 SDK 消费者同样是破坏性的：

| # | 版本 | 原文 / 源码 | 为什么是破坏 |
|---|---|---|---|
| S1 | **1.2.0 Removed**（CHANGELOG:109-110） | 移除未发布的实验性 server/client/plugin 栈（`src/experimental`、`src/cli/experimental`）与其 source-only 导出 `./client`、`./experimental/plugin`；`@earendil-works/pi-client`/`pi-protocol`/`pi-server` 包被移除并不再发布 | **package.json `exports` 键被删**。0.85.1 有 `"./client"`、`"./experimental/plugin"`（`v0.85.1/.../package.json` exports），1.3.0 **只剩 `.` 与 `./rpc-entry`**。原文称「none of it was part of the published package」——因为那两个键只有 `"source"` 条件、不进 dist，所以对 **npm 安装者**不破坏，对 **源码消费者**破坏 |
| S2 | **1.2.0 Removed**（CHANGELOG:110） | 移除对 `@earendil-works/chord` 的依赖 | 0.85.1 `dependencies` 有 `"@earendil-works/chord": "^0.85.1"`；1.3.0 无 |
| S3 | **1.2.0 Added**（CHANGELOG:61） | 生成式配置 schema 发布；theme schema 路径变更 | 见 B1 |
| S4 | **0.86.0 Added**（CHANGELOG:560） | **`pi.on()` 现在返回 unsubscribe 函数**；派发期间新增/移除的 handler 只对后续派发生效 | 若 SDK 消费者假设 `on()` 返回 void，类型层面非破坏；但**「派发期间改动只影响后续」是行为性变更** |
| S5 | **0.86.0 Changed**（CHANGELOG:573） | 内置 `read`/`bash`/`powershell`/`edit`/`write` 工具**默认**启用 strict-prefer JSON-schema 采样，不再需要 `PI_EXPERIMENTAL`；扩展可用 `constrainedSampling: false` 覆盖 | 源码：`tools/{bash,read,edit,write}.ts` 的 `constrainedSampling` 从 `getExperimentalToolSampling()` 改为字面量 `{ type: "json_schema", strict: "prefer" }`；`experimental.ts` 删掉 `getExperimentalToolSampling`。**改变模型收到的工具 schema** |
| S6 | **1.3.0 Fixed**（CHANGELOG:26） | 恢复会话时保留会话期间激活的工具 | 源码：`core/sdk.ts` 新增 `usesDefaultTools` / `defaultToolModifiers` / `allowedToolNames` 组合逻辑 |
| S7 | **1.0.4 Added**（CHANGELOG:173-174, 181） | `--tools`/`--exclude-tools` 接受 `*` 模式；`--tools` 默认保留 MCP 工具，除非条目以 `mcp__` 开头；新增 `--no-mcp` | 源码：`core/mcp-servers.ts:21` `createToolNameMatcher()`（精确名 + `*` 模式），被 `core/sdk.ts` 用于 `excludeTools` |
| S8 | **1.1.0 Added**（CHANGELOG:124, 126, 130） | `--tools` 支持 `+name`/`-name`（改默认集而非替换）；工具渲染上下文与 `tool_execution_end` 事件新增 `durationMs`；渲染上下文新增 `outputPad`；`agent_settled` 新增 `aborted`（`AgentActivityOutcome = "completed"\|"aborted"\|"error"`） | 源码：`settings-manager.ts` 新增 `isToolModifier()`/`applyToolModifiers()`/`getToolListError()`/`DEFAULT_TOOL_NAMES`；`sdk.ts` 用它们；`settings-manager.ts` 新增 `mergeDefaultTools()` 让 `+`/`-` 跨 settings 层叠加 |
| S9 | **1.3.0 Fixed**（CHANGELOG:29） | `--model` 在多 provider 提供同一匹配时不再按 catalog 顺序选；优先有凭据的 provider，否则报歧义 | 源码：`core/model-resolver.ts` **删除** `parseModelPattern`/`ParsedModelResult`，新增 `resolveModelReference()`/`ModelReferenceRequest`/`ModelReferenceResult`/`ModelReferenceTarget`。`src/index.ts` 仍只导出 `resolveCliModel`/`resolveModelScopeWithDiagnostics`（签名未变），**故对包消费者是行为变更而非类型破坏** |
| S10 | **1.0.4 Fixed**（CHANGELOG:183） | `ToolLoadout` 新增 `getPromptGuidelines()`；系统提示规则与 skills 提示不再点名被 `prepareLoadout` 隐藏的工具 | 源码：`extensions/types.ts` 的 `ToolLoadout` 含 `getPromptGuidelines(name)`；`core/system-prompt.ts` 新增 `hiddenTools`/`toolGuidelines` 选项 |
| S11 | **1.3.0 Fixed**（CHANGELOG:33, 45） | 空引号参数不再丢（`""`/`''`）；工具调用参数含非法 `\u` 转义（如 `\usepackage`）时不再静默丢字段，改为校验失败并把原始参数交给模型 | 归属 **未验证**（可能在 `src/utils/` 或 pi-ai；grep 未定位）。登记为待查 |
| S12 | **1.2.0 Changed**（CHANGELOG:79） | `serializeConversation()`（compaction / branch summary / bug report 共用）**不再包含 thinking 块** | 源码：`core/compaction/utils.ts:114-143`（注释原文：「Thinking is omitted. Providers return summaries of the real reasoning, and replayed thinking is often not part of the model's live context ... Including it can make the summarization request larger than the context it summarizes.」） |

### 4.3 SDK 包入口（`src/index.ts`）新增导出符号

**实测集合差：`src/index.ts` 导出符号 383 → 461，新增 78 个，删除 0 个。**

按模块归并（`src/index.ts` diff + core 全树 export 集合差）：

| 模块 | 新增导出（按模块归并；包入口总计 **78 新增 / 0 删除**） |
|---|---|
| `core/cache-warmer.ts` | `CacheWarmingDecision`、`CacheWarmingStatus` |
| `core/mcp-servers.ts` | `RegisteredMcpServer` |
| `core/virtual-models.ts` | `ModelRoute`、`ModelRouteReason`、`ModelRouteRequest`、`VirtualModelDefinition`、`VirtualModelStateData`、`VIRTUAL_MODEL_STATE_ENTRY` |
| `core/session-manager.ts` | `ContextEditEntry`、`ContextEditableContent`、`ProjectedSessionEntry`、`SessionProjection`、`buildSessionProjection` |
| `core/settings-manager.ts` | `CacheWarmingMode`、`CompactionModelOverride`、`QuietStartup` |
| `core/tools/render-utils.ts` | `formatReadLineRange` |
| `core/extensions/types.ts`（经 index） | `AfterProviderResponseEvent`、`AgentActivityOutcome`、`AgentBeforeSettleEvent(Result)`、`BeforeAgentStartCustomEvent`、`BeforeAgentStartUserEvent`、`BoundaryContextPreview`、`BoundaryResult`、`BoundaryState`、`CacheWarmingDecisionEvent(Result)`、`CompactionEntryDraft`、`ContextEditEntryDraft`、`ContextEventResult`、`ContextWithSystemEvent`、`CustomEntryDraft`、`CustomMessageEntryDraft`、`ExecuteToolOptions`、`ExtensionToolContext`、`ExtensionVirtualModel`、`McpServersChangeEvent`、`MessageEndEventResult`、`ModelSelectEvent`、`NormalizedBuildSystemPromptOptions`、`ProviderStreamEvent`、`ResourcesDiscoverEvent`、`ResourcesDiscoverResult`、`SessionBefore*Result`、`SessionBoundaryDraft`、`SessionCompactFailedEvent`、`ThinkingLevelSelectEvent`、`ToolAnnotations`、`ToolExposure`、`ToolLoadout`、`ToolLoadoutChanges`、`ToolNamespace`、`ToolRendererResolver`、`ToolRenderers`、`ToolResultEventResult`、`TurnEndEventResult` |
| `core/extensions` 新事件（5 个） | `agent_before_settle`、`cache_warming_decision`、`context_with_system`、`mcp_servers_change`、`provider_stream_event` |
| **`src/extensions/`（本次 core 范围外）** | `createCodemodeExtension`、`createMcpExtension`、`createToolSearchExtension` + 相关类型 |

`src/index.ts` 尾部新增注释（原文）：
> `// Built-in extensions. The CLI loads them; SDK sessions add them to their extension factories.`

**这是本轮最重要的结构性发现**：v1.3.0 的 SDK 公开面已经**不再等于 `src/core/`**。

---

## 5. 越界判断（需要用户拍板）

判定标准：**「编码 agent SDK（bash/read/edit/write/grep/find/ls）」是否直接需要它**（依据 pi-py `README.md:3` 与 `__init__.py:1-11` 的自述定位），以及**是否引入 Python 侧不存在的运行时机制**。

| 模块 | LOC | 判定 | 依据 |
|---|---:|---|---|
| `cache-warmer.ts` | 453 | 🟡 **边缘·可延后** | 纯 SDK 逻辑、无 TUI 依赖，实现质量高；但价值只在「长工具链 + 贵模型」场景，且依赖 pi-ai 的 `model.promptCache`/`calculateCost`（pi-py 缺，§2.4） |
| `bug-report.ts` + `bug-report-upload.ts` | 412 | ❌ **建议排除** | 依赖上游**自有商业服务** `getRadiusGatewayUrl()/v1/bug-reports`（`bug-report-upload.ts:20`）；依赖 `../utils/zip.ts`、`../utils/pi-user-agent.ts`；收集 `settings.trackingId`/`deviceId`。这是产品运营功能，不是编码能力；且 Python 侧无法对上上游服务 |
| `crash-log.ts` | 170 | 🟡 **边缘** | 纯本地、无网络，逻辑干净；但**唯一消费者是 `/bug`**（`crash-log.ts` 被 `bug-report.ts` 引用）。排除 bug-report 后应一并排除 |
| `mcp-servers.ts` | 346 | ⚠️ **不能单独拍板** | core 部分**只做校验与注册**（文件头注释原文：「The core only validates and stores registrations. The MCP extension ... connects them」）。真正价值在 `src/extensions/mcp/`（**4,257 LOC**，core 范围外）。**要么整条 MCP 线纳入，要么只保留 `createToolNameMatcher()`（`mcp-servers.ts:21`，被 `sdk.ts` 的 `excludeTools` 直接使用）** |
| `virtual-models.ts` | 238 | ✅ **属 SDK，但前置缺失** | 扩展路由模型是纯 SDK 能力；但依赖 `lazyStream`/`isModelType`（pi-ai）与 assistant message 的物理模型语义 |
| `nested-tool-calls.ts` | 263 | ✅ **属 SDK，但前置缺失** | `ctx.executeTool()` 是 codemode 的基础设施；依赖 `AgentToolCallOutcome`、`AgentToolResult.usage`（pi-py 缺） |
| `settings-schema.ts` + `settings-defaults.ts` | 549 | ✅ **属 SDK** | settings.json 是 SDK 契约；`SETTINGS_DEFAULTS` 消除散落的魔法数字。**但**：TypeBox → Python 需换 pydantic/jsonschema 实现 |
| `keybindings-schema.ts` + `key-id-schema.ts` | 42 | ❌ **排除** | 依赖 `@earendil-works/pi-tui` 的 `Key` 枚举；键位是 TUI 概念 |
| `extensions/{jiti-loader,jiti-static-loader,virtual-modules}.ts` | 44 | ❌ **排除** | Node/Bun/SEA 的 TS 转译与打包机制，Python 侧无对应物。功能上应对应 Python 的 `importlib` 扩展加载，但**设计完全不同，硬翻译无价值**（与 `PORTING.md:15` 对 TUI 的判断同理） |
| `export-html/*`（本次仅 modified） | — | ❌ **排除** | HTML 会话导出，TUI/CLI 面 |
| `keybindings.ts`、`tools/renderers/*`、`footer-data-provider.ts` | — | ❌ **排除** | 渲染层 |
| `src/extensions/{codemode,mcp,tool-search}/` | 5,915 | ⚠️ **需单独决策** | 0.99.0 新增；**被 re-export 进 SDK 入口**；但位于 `src/core/` 之外，与本任务「core 面」边界不符。`codemode` 还额外需要 `quickjs-wasi` 依赖（1.3.0 `package.json`）与 JS 沙箱——Python 侧需换实现（如 `quickjs` 绑定或子进程 node） |

**建议给用户的两个问题**（见 §8）：
- Q1：`src/extensions/` 是否纳入 pi-py `pi-coding-agent` 的目标范围？（决定 MCP / codemode / tool-search 整条线的取舍）
- Q2：成本与诊断类功能（cache-warmer / crash-log / bug-report）是否纳入？

---

## 6. 工作量估算（MUST-PORT 分级）

分级定义：**S** = ≤1 天 / 纯函数或常量级；**M** = 2–5 天 / 单模块重写；**L** = ≥1 周 / 跨模块重构或引入新子系统。

### Tier-A：与**已移植面**直接对应的改动（建议 MUST-PORT，共 ~9 项）

| # | 项 | 上游 | 分级 | 说明 |
|---|---|---|---|---|
| A1 | 内置工具 `constrainedSampling` 默认开启 `{type:"json_schema",strict:"prefer"}` | `tools/{bash,read,edit,write}.ts`（各 3 行 diff） | **S** | **管线已通，只差默认值**：`pi-ai/src/pi_ai/types.py:306-307` 已有 `constrained_sampling` 字段；`pi-agent-core/src/pi_agent_core/agent_loop.py:394` 用 `getattr(t, "constrained_sampling", None)` 透传（`:391-396` 组装 `Tool`）；`pi-ai/src/pi_ai/constrained_sampling.py:226` 消费。pi-py 7 个工具类**都没定义该属性** → 只需在 4 个类上加类属性。**注意**：这会改变模型侧行为，需实测 provider |
| A2 | 删除 `getExperimentalToolSampling()` 语义 | `experimental.ts`（9→3 行） | **S** | pi-py 未移植该文件；只需确认无对应代码 |
| A3 | `truncateMiddle()` + `MiddleTruncationResult` | `tools/truncate.ts:278-314` | **S** | 纯函数，~40 行 Python，可直接移植（UTF-8 边界处理要照抄 `isBoundary` 逻辑） |
| A4 | bash 信号/无退出码语义 | `tools/bash.ts:152-158`（信号 → `128+signal`）、`:387`（`"Command terminated without an exit code"`）、`:405-412`（`isError: true`） | **S** | pi-py 已对齐 `128+signal`（`tools.py:131-137`）；仍需补：`exitCode === null` 时**不再当作成功**，改为 `isError` + 文案 |
| A5 | `ReadTool` 图片 resize 档案 + `ReadToolOutput` | `tools/read.ts:24-38, 61-84, 133-137, 216` | **M** | 需引入 `inputLimits.images.resize` 概念（pi-ai + models.json）。若暂不做 resize，至少可先做 `note` 语义（图片不可处理时的文字说明） |
| A6 | `formatReadLineRange()` 行号标签修复 | `tools/render-utils.ts:38`；CHANGELOG 1.3.0 #9887 | **S** | 纯函数（处理 `null`/数字字符串）；修的是 read 结果行号标签。pi-py 目前在 `tools.py:268-286` 自行拼 `[Showing lines X-Y of Z...]` / `Use offset=N to continue`，可对齐 |
| A7 | 跨 chunk ANSI 转义修复 + `readFullOutput()` | `bash-executor.ts:108`（`splitIncompleteAnsiSuffix`）、`:66`（`createOutputFileStream`）；`tools/output-accumulator.ts:149`（`readFullOutput`） | **M→可选** | **A7 对 pi-py 的价值取决于它是否走 `bash_executor`**——pi-py 的 BashTool 直接 `asyncio.create_subprocess_shell` + `truncate_tail`（`tools.py:82-171`），**没有** `bash-executor.ts` / `OutputAccumulator` 对应物。因此这是「补齐上游架构」而非「修 bug」→ 建议**降级为可选** |
| A8 | `usage-totals.ts: combineUsage()` | `usage-totals.ts:30-52` | **S** | 纯函数；仅在 pi-py 要统计 nested/usage 时才需要 → **依赖 A9 决策** |
| A9 | `extensions/types.ts` 的 `ToolDefinition` 工具能力扩展（`outputSchema`/`exposure`/`namespace`/`annotations`/`reachesTools`/`defaultActive`/`prepareLoadout`） | `extensions/types.ts`（7 个新字段） | **L** | **被 §2.4 的 pi-agent-core 前置缺口阻塞**。建议先做下游包，再做这里 |

**Tier-A 合计**：A1–A4、A6、A8 = **S×6**；A5 = **M×1**；A9 = **L×1**（且被跨包前置阻塞）；A7 降级为可选 → **不含 A9 约 1–2 人周**。

### Tier-B：新 core 子系统（**取决于 §5 拍板**）

| # | 项 | 上游 LOC | 分级 | 前置 |
|---|---|---:|---|---|
| B1 | `system-prompt.ts` sections 模型（`SystemPromptSections` / `normalizeBuildSystemPromptOptions` / `buildSystemPromptSections` / `diffSystemPromptSections`） | 295(改) | **L** | `SystemMessage.sections`（pi-ai） |
| B2 | `session-manager.ts` 的 `ContextEditEntry` + `SessionProjection` + `buildSessionProjection` | 567(改) | **L** | B1 |
| B3 | `settings-schema.ts` + `settings-defaults.ts`（TypeBox → pydantic） | 549(新) | **M** | 无 |
| B4 | `cache-warmer.ts` | 453(新) | **L** | `model.promptCache`/`calculateCost`（pi-ai） |
| B5 | `nested-tool-calls.ts` + `executeTool()` | 263(新) | **L** | `AgentToolResult.{structuredContent,usage,isError}`、`AgentTool.outputSchema` |
| B6 | `virtual-models.ts` | 238(新) | **M** | `lazyStream`/`isModelType`（pi-ai） |
| B7 | `sdk.ts` 的 `tools`/`excludeTools` 模式匹配 + `+name`/`-name` + `defaultTools` 分层合并 | 209(改) | **M** | `mcp-servers.ts:createToolNameMatcher()`、`settings-manager` 的 `applyToolModifiers()` |
| B8 | AgentSession 的 boundary 事件（`finishTurn`/`agent_before_settle`/`emitBoundary`） | part of 1962 | **L** | `Agent.finishTurn`/`prepareRequest`（pi-agent-core） |
| B9 | MCP 整条线（core 注册 + `src/extensions/mcp`） | 346 + 4257 | **L** | 用户拍板；`@earendil-works/pi-mcp` 语义 |
| B10 | codemode 整条线 | 1396 + quickjs | **L** | 用户拍板；Python JS 沙箱方案未定 |
| B11 | `estimateProjectedContextTokens()` + compaction 修复 | 295(改) | **M** | B2 |

> **Tier-B 全做 = 数人月量级**。建议 Lead 不要把它当「同步」，而是当「功能路线图」立项。

### Tier-C：明确不做（越界）

**新增模块**：`bug-report*`(412) + `crash-log`(170) + `keybindings-schema*`(42) + `extensions/jiti*`+`virtual-modules`(44) = **668 LOC 的「新增 core 文件里不属于 SDK 的部分」**。
**另有 modified 但越界的**：`export-html/*`（4 个文件）、`tools/renderers/*`（5 个）、`keybindings.ts`。

这正是本任务要防的「把 TUI 条目混进追赶清单」——**14 个新增文件里有 7 个、约 20% 的新增 LOC 应当直接划掉**。

---

## 7. 与 pi-py 现有偏离项的关系（`PORTING.md` 对照）

| `PORTING.md` 记录 | 本次侦察结论 |
|---|---|
| `:27-29` bash 信号退出码 → `128+信号` | ✅ **仍成立且已对齐**，且上游 v1.3.0 进一步加强（`exitCode===null` → 失败）。补 A4 即可 |
| `:30-31` v0.85.1「上游本轮 core 改动（subagent session config 继承、可配置默认工具、远程 plugin/worker 体系、模型目录刷新）位于本端裁剪的 CLI/TUI/扩展/目录范围」 | ⚠️ **部分不成立**：「可配置默认工具」在 v1.3.0 已进入 core（`settings-manager.ts` 的 `DEFAULT_TOOL_NAMES`/`applyToolModifiers`，被 `sdk.ts` 消费）→ 应在下一版同步说明中修正 |
| `:35` v0.84.1「`*SystemPromptContribution` 结构不映射到本端精简 tools.py」 | ⚠️ **需重新评估**：v1.3.0 把 `promptSnippet`/`promptGuidelines` 与 `prepareLoadout`/`hiddenTools` 打通（`system-prompt.ts` 新增 `toolGuidelines`/`hiddenTools`，CHANGELOG 1.0.4 #10343），偏离成本上升 |
| `:21-23` cherry-pick 小节为空 | ✅ 无需清理 |
| `:44-51` 待办 | ❌ 需重写（见 §2.3） |

---

## 8. 开放问题

**Q1（阻塞排期，最高优先）**：`src/extensions/`（codemode 1396 + mcp 4257 + tool-search 262 = **5,915 LOC**）是否纳入 pi-py 的目标范围？
- 依据：v1.3.0 已把 `createCodemodeExtension`/`createMcpExtension`/`createToolSearchExtension` **re-export 进 SDK 包入口**（`src/index.ts` 尾部注释原文：「Built-in extensions. The CLI loads them; SDK sessions add them to their extension factories.」）。
- 若「不纳入」，则 v1.3.0 的 SDK 面实际只承接了 §5 里被判 ✅/🟡 的部分，**追赶清单会小一个数量级**；若「纳入」，codemode 需要先定 Python 侧 JS 沙箱方案（上游用 `quickjs-wasi`）。

**Q2**：成本/诊断类功能（`cache-warmer` 453 + `crash-log` 170 + `bug-report*` 412）是否纳入？
- 我的建议：cache-warmer 可延后（前置缺 pi-ai `promptCache`）；crash-log + bug-report **排除**（依赖上游自有 Radius 服务）。

**Q3**：`bug-report.ts` 依赖的 `../utils/zip.ts`、`../utils/pi-user-agent.ts` 在 `src/utils/`（3,775 LOC），而 `PORTING.md:14` 声称 `src/utils/` 已复刻但实际 0 行。**`src/utils/` 的真实定位是什么？**（`tools/bash.ts` 依赖 `utils/shell.ts`，`tools/grep.ts` 依赖 `utils/tools-manager.ts`——即上游工具层**本身就依赖 utils**，pi-py 是把这部分内联进 `tools.py` 了。）

**Q4**：跨包前置（§2.4）由谁负责？`SystemMessage.sections`、`AgentTool.outputSchema`/`structuredContent`、`Agent.finishTurn`/`prepareRequest` 全在 pi-ai / pi-agent-core 两条线。**如果那两条线不同步，Tier-B 的 B1/B2/B5/B8/B11 全部无法启动**。需要 Lead 指定衔接方式。

**Q5**：pi-py `pi-coding-agent` 的 `CodingAgent` 是 `pi_server` 的运行时依赖（`packages/pi-server/src/pi_server/supervisor.py:23,92`）。**未来对齐上游 `createAgentSession` 语义（返回 `AgentSession` 而非 `CodingAgent`）时，是否允许破坏 `CodingAgent` API？** 建议在 `PORTING.md` 明确一个兼容策略。

**Q6**：上游 `resolveModelReference()` 的歧义处理（S9）改变了 `--model` 行为，pi-py 的模型线是否有对应位置？（未在 pi-py 侧验证，标「未验证」）

**Q7**：`PORTING.md` 的裁剪表要由谁修？（它是当前唯一给外部读者看的「进度快照」，与代码偏差很大。）

---

## 附录 A：`diff -rq` 完整 modified 清单（54 个，含 ±行数）

```
 1962  agent-session.ts            750  model-resolver.ts
  653  extensions/types.ts         567  session-manager.ts
  449  extensions/runner.ts        438  settings-manager.ts
  360  model-runtime.ts            295  system-prompt.ts
  295  compaction/compaction.ts    290  resource-loader.ts
  287  provider-composer.ts        210  model-config.ts
  209  sdk.ts                      193  remote-catalog-provider.ts
  190  extensions/loader.ts        127  export-html/template.js
  116  prompt-templates.ts         116  keybindings.ts
  100  tools/render-utils.ts       100  bash-executor.ts
   97  package-manager.ts           96  tools/renderers/bash.ts
   87  model-registry.ts            55  tools/bash.ts
   55  compaction/utils.ts          53  tools/output-accumulator.ts
   43  session-export.ts            38  tools/truncate.ts
   38  tools/read.ts                37  auth-storage.ts
   32  extensions/index.ts          29  usage-totals.ts
   26  extensions/wrapper.ts        21  tools/tool-definition-wrapper.ts
   20  tools/renderers/edit.ts      18  tools/renderers/read.ts
   17  source-info.ts               17  index.ts
   14  export-html/tool-renderer.ts 12  cache-stats.ts
   12  agent-session-services.ts    10  radius.ts
   10  export-html/template.css     9  skills.ts
    6  experimental.ts               6  agent-session-runtime.ts
    4  tools/renderers/index.ts      4  compaction/branch-summarization.ts
    3  tools/write.ts                3  tools/edit.ts
    3  slash-commands.ts             3  project-trust.ts
    1  trust-manager.ts              1  messages.ts
```

## 附录 B：CHANGELOG 节定位（窗口内）

| 版本 | 日期 | 起始行 | 有 Breaking？ | 有 Removed？ |
|---|---|---:|---|---|
| 1.3.0 | 2026-10-15 | 3 | ✗ | ✗ |
| 1.2.0 | 2026-10-12 | 49 | **✓（4 条，line 59）** | ✓（line 107） |
| 1.1.0 | 2026-10-07 | 112 | ✗ | ✗ |
| 1.0.4 | 2026-10-05 | 164 | ✗ | ✗ |
| 1.0.3 | 2026-10-05 | 187 | **✓（1 条，line 194）** | ✗ |
| 1.0.2 | 2026-10-04 | 214 | ✗ | ✗ |
| 1.0.1 | 2026-10-03 | 224 | ✗ | ✓（line 264） |
| 1.0.0 | 2026-10-01 | 268 | ✗ | ✗ |
| 0.99.2 | 2026-09-30 | 314 | ✗ | ✗ |
| 0.99.1 | 2026-09-29 | 354 | ✗ | ✗ |
| 0.99.0 | 2026-09-29 | 372 | ✗ | ✗ |
| 0.87.1 | 2026-09-22 | 451 | ✗ | ✗ |
| 0.87.0 | 2026-09-21 | 475 | **✓（5 条，line 483）** | ✗ |
| 0.86.1 | 2026-09-20 | 513 | ✗ | ✗ |
| 0.86.0 | 2026-09-19 | 535 | **✓（3 条，line 545）** | ✓（line 618） |
| 0.85.1 | 2026-09-05 | 622 | ✗（基线版本） | ✗ |

> **0.88–0.98 不存在**：`grep -c '^## \[0\.9[0-8]' CHANGELOG.md` = 0；`packages/ai/CHANGELOG.md` 同样跳过。经确认这是**版本编号跳跃**（0.87.1 → 0.99.0），不是数据缺失。

## 附录 C：未验证项（诚实标注）

1. §2.4 表格中 `model.promptCache`/`calculateCost` 在 pi-py `pi-ai` 的缺失——基于 `grep -rn "prompt_cache"` 只命中 provider 实现，**未逐字段核对** `pi_ai/models.py`。
2. S11（空引号参数 / `\u` 转义参数解析）的上游归属——grep 未在 core/ 定位到，标「待查」。
3. Q6（pi-py 模型线是否有 `resolveModelReference` 对应位置）——未检查 pi-py 模型解析代码。
4. `bug-report.ts` 中 `writeZipArchive`（来自 `../utils/zip.ts`）的行为——未读该文件（超出 core 范围）。
5. `src/extensions/{codemode,mcp,tool-search}` 的**逐文件**职责——只做了 LOC 与入口导出核对，未逐文件读（它们不在本任务 core 边界内）。

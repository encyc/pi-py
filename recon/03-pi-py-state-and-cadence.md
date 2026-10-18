# 侦察 C：pi-py 现状盘点 + 上游发布节奏分析

- 盘点对象：`/Users/mac/pi-py`（本地 workspace，只读）
- 上游：`earendil-works/pi`（GitHub API，`gh` 已登录 encyc）
- 盘点时间：2026-10-18（Asia/Shanghai）
- 工具环境：Python 3.13.3 / uv 0.11.24 / pytest 9.1.1 / ruff 0.15.22 / mypy 2.3.0

**日期口径说明（重要）**：任务描述里的「v0.85.1（2026-09-07）」是**本仓同步日**（`SYNC.md` 同步历史、`CHANGELOG.md` 标题都写 2026-09-07），上游 GitHub Release 的 `published_at` 实际是 **2026-09-05T12:29:01Z**。下文表格一律用 GitHub API 的 UTC 时间，凡涉及「同步日」另行注明。

---

## 结论速览（TL;DR）

| 维度 | 结论 |
|---|---|
| 代码健康度 | 单测 262 passed / 16 deselected；ruff check + format + mypy（文档/CI 命令）全绿；集成测试本机可跑（DeepSeek，11 passed / 5 skipped） |
| 唯一失败项 | `uv run mypy packages` 报 `Duplicate module named "test_integration"` —— 是 **CLI 参数覆盖配置**导致（把 tests 也纳入检查），不是代码类型缺陷；CI/README 用的 `uv run mypy` 通过 |
| 发布欠账 | **v0.85.1 没有发到 PyPI**：tag 已推、GitHub Release 未创建 → `publish.yml` 从未触发。PyPI 五个包最新仍是 **0.84.1**（2026-08-18） |
| 上游节奏 | 0.x 尾段 2.21 release/周 → 1.x **4.00 release/周**（约 1.8 倍）；显式 Breaking 条目密度略降（1.00 → 0.62 条/ release），但 patch 里也开始出现 breaking 与 feature |
| 版本跳跃 | v0.87.1 → v0.99.0 之间 **0.88~0.98 从未存在过任何 tag / release / changelog 条目**，是发布脚本显式指定版本的人为跳号；**为什么选 0.99.0，公开数据无说明，无法确定**（证据见 §7） |
| 对 SYNC.md | 「只在 `0.x.0` minor 同步」的两个前提都已失效：不存在 0.x 了，且 1.x 的 minor 不再等于破坏性变更、patch 里也会塞 breaking/feature |

---

# 第一部分：pi-py 现状盘点

## 1. 逐包清单

仓库共 5 个包，均在 `/Users/mac/pi-py/packages/` 下，uv workspace 管理，按依赖自底向上：`pi-ai → pi-agent-core → pi-storage-sqlite / pi-coding-agent → pi-server`。

### 1.1 汇总表

行数与用例数全部由命令算出（`wc -l`、`pytest --collect-only`），非目测。

| 包 | 源码文件 | 源码行数 | 测试文件 | 测试行数 | 收集用例 | 其中默认跑 | 其中 integration | 对应上游包（v0.85.1） |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| pi-ai | 15 | 3885 | 9 | 2419 | 120 | 113 | 7（2 跑 / 5 skip） | `@earendil-works/pi-ai`（`packages/ai`） |
| pi-agent-core | 9 | 2660 | 7 | 1535 | 86 | 81 | 5（全跑通） | `@earendil-works/pi-agent-core`（`packages/agent`） |
| pi-coding-agent | 4 | 1263 | 3 | 484 | 37 | 33 | 4（全跑通） | `@earendil-works/pi-coding-agent`（`packages/coding-agent`） |
| pi-server | 5 | 646 | 2 | 256 | 17 | 17 | 0 | `@earendil-works/pi-server`（`packages/server`） |
| pi-storage-sqlite | 3 | 472 | 2 | 310 | 18 | 18 | 0 | **`@earendil-works/pi-session-backend-sqlite-node`**（`packages/session-backends/sqlite-node`；本仓文档仍写旧名，见 §2.3） |
| **合计** | **36** | **8926** | **23** | **5004** | **278** | **262** | **16（11 跑通 / 5 skip）** | |

> 用例数说明：全仓收集 278 条 = 默认跑的 262 条 + integration 16 条。代码里 `def test_*` 函数共 252 个，少于收集数是因为存在 `@pytest.mark.parametrize` 展开。mypy 实际检查的源码文件数 36（见 §3）。

### 1.2 各包模块与职责

**pi-ai（15 文件 / 3885 行）** —— 统一 LLM API

| 模块 | 行数 | 职责 |
|---|---:|---|
| `types.py` | 472 | 核心类型系统（TextContent/ThinkingContent/ToolCall/Usage/Model/Context…） |
| `providers/openai_provider.py` | 956 | OpenAI Chat Completions 适配器（含 reasoning_details、strict schema、grammar 采样） |
| `providers/anthropic_provider.py` | 813 | Anthropic Messages 适配器（thinking、fallback、mid-convo effort） |
| `constrained_sampling.py` | 316 | strict JSON Schema 转换 + Lark/regex grammar 约束采样 |
| `retry.py` | 220 | assistant 调用重试（可重试错误分类） |
| `__init__.py` | 202 | 公共导出面 |
| `providers/faux.py` | 185 | 测试用 mock provider |
| `models.py` | 160 | 模型注册表 + api provider 分发 |
| `events.py` | 158 | 流式事件类型（12 变体 AssistantMessageEvent） |
| `provider_retry.py` | 123 | provider SDK 请求的可取消重试包装 |
| `exceptions.py` | 103 | 异常体系 |
| `event_stream.py` | 89 | `EventStream`（asyncio.Queue） |
| `stream.py` | 69 | `stream` / `stream_simple` / `complete` 入口 |
| `user_agent.py` | 18 | pi User-Agent 构造 |
| `providers/__init__.py` | 1 | 子包 |

README 声称覆盖：OpenAI Chat Completions（兼容 DeepSeek/OpenRouter）、Anthropic Messages（thinking）、Faux mock。
PORTING 进度表：google/mistral/bedrock、`auth/*`、`images*` 三项标 🟡 后续。

**pi-agent-core（9 文件 / 2660 行）** —— agent 运行时 + harness

| 模块 | 行数 | 职责 |
|---|---:|---|
| `agent_loop.py` | 697 | 无状态循环引擎（双循环：follow-up + tool calls + steering） |
| `harness/session.py` | 439 | 会话持久化与树结构（SessionEntry/InMemory/Jsonl/Session） |
| `harness/skills.py` | 383 | 技能加载（frontmatter 校验、递归扫描、prompt 格式化） |
| `agent.py` | 381 | 有状态 `Agent` 封装（prompt/steer/follow_up/abort） |
| `types.py` | 306 | 核心类型与事件（AgentTool/AgentState/AgentLoopConfig/事件联合） |
| `harness/compaction.py` | 263 | 上下文压缩（估算 token、切点、摘要生成） |
| `__init__.py` | 164 | 公共导出面 |
| `event_stream.py` | 22 | agent 事件流工厂 |
| `harness/__init__.py` | 5 | 子包 |

README 声称覆盖：双循环引擎、有状态 Agent 类、harness（skills/会话持久化/压缩）。

**pi-coding-agent（4 文件 / 1263 行）** —— 编码 agent SDK（无 TUI）

| 模块 | 行数 | 职责 |
|---|---:|---|
| `tools.py` | 847 | 7 个编码工具：bash/read/edit/write/grep/find/ls（纯 Python grep，无 ripgrep 依赖） |
| `truncate.py` | 204 | 输出截断（head/tail/line、字节计量、格式化） |
| `sdk.py` | 139 | `create_agent_session` + `CodingAgent` 封装 |
| `__init__.py` | 73 | 公共导出面 |

README 声称覆盖：上表 7 工具。PORTING 明确「只覆盖上游约 35/54k LOC 的逻辑部分，不复刻 `src/modes/interactive/`（Ink TUI ~16.5k）与 `cli.ts`/`main.ts`（~31k）」。

**pi-server（5 文件 / 646 行）** —— agent 服务化（可选包）

| 模块 | 行数 | 职责 |
|---|---:|---|
| `ipc.py` | 227 | IPC 服务端：Unix socket + JSONL 协议（serve/send_request/rpc_stream） |
| `supervisor.py` | 219 | 实例管理器（AgentInstance/Supervisor） |
| `protocol.py` | 109 | RPC 协议编解码（encode/parse/ok/error） |
| `config.py` | 49 | 路径与配置（server dir / socket path / instances path） |
| `__init__.py` | 42 | 公共导出面 |

README 声称覆盖：spawn/list/status/stop/rpc/rpc_stream 命令。PORTING 自注「本包为**可选**……最后评估是否值得实现」。

**pi-storage-sqlite（3 文件 / 472 行）** —— SQLite 会话存储

| 模块 | 行数 | 职责 |
|---|---:|---|
| `storage.py` | 316 | `SqliteSessionStorage` / `SqliteSessionRepo` |
| `database.py` | 131 | `SqliteDatabase` 封装 + migration 执行 |
| `__init__.py` | 25 | 公共导出面 |

README 声称覆盖：`SqliteSessionRepo` → `Session` 的创建/追加消息/关闭。

### 1.3 上游对应关系的现实（追赶前必须知道）

上游 v0.85.1 的 `packages/` 下已有 **11 个包**（agent/ai/chord/client/coding-agent/evals/protocol/server/session-backends/telemetry/tui），pi-py 只映射其中 5 个。到 **v1.3.0 上游结构大幅漂移**：

| pi-py 包 | 上游 v0.85.1 | 上游 v1.3.0 现状（实测） |
|---|---|---|
| pi-ai | `packages/ai` | 仍存在，TS 体积 2.08 → 2.49 MiB |
| pi-agent-core | `packages/agent` | 仍存在但被**掏空**：218 文件/1.61 MiB TS → 20 文件/0.21 MiB TS；harness v3/pico3/durable drive 移出到 `durable`（201 文件/2.46 MiB TS）；v1.3.0 依赖只剩 `pi-ai` + `typebox` |
| pi-storage-sqlite | `packages/session-backends/sqlite-node`（v0.84.1 起；v0.83.0 时是 `packages/storage/sqlite-node`） | 包已不存在；sqlite 实现落在 `packages/durable`（`test/sqlite-*.test.ts`） |
| pi-coding-agent | `packages/coding-agent` | 仍存在，TS 4.55 → 5.34 MiB；新增依赖 `pi-codemode`/`pi-mcp`/`pi-tui`/`quickjs-wasi` |
| pi-server | `packages/server` | **包已被移除**：`server`/`client`/`protocol` 三个包在 v1.1.0 还在，v1.2.0 起消失 |

另：v0.99.0 新增 `codemode`、`mcp`；v1.1.0 新增 `env`。用 `gh api repos/earendil-works/pi/compare/v0.85.1...v1.3.0` 实测 **676 个 commit**（API 只返回前 250 条 commit、300 个文件，`total_commits`/`ahead_by` 字段可信）。

> 含义：追到 1.3.0 不是「补 1.15 个 minor」的线性任务，而是一次**重新对齐基线**：上游拆包/并包后，pi-py 的 5 包映射有 2 个（storage、server）已失去上游对应物，pi-agent-core 的上游宿主换成了 durable/chord 体系。

## 2. PORTING.md 汇总

### 2.1 「有意偏离」总表

「是否仍然成立」列基于我本轮从上游 API 实测到的 v1.3.0 事实；标「未核实」的表示该点需要读上游源码才能判断，我没有证据不下结论。

| 包 | 上游做法 | 本端做法 | 原因（文档原文归纳） | 是否仍然成立（对 v1.3.0 核实） |
|---|---|---|---|---|
| pi-ai | typebox 类型 | Pydantic v2 BaseModel | Python 生态标准；既做运行时校验又做序列化 | **成立**：上游 v1.3.0 `ai` 仍依赖 `typebox 1.3.27` |
| pi-ai | Promise / ReadableStream | asyncio + AsyncGenerator | 对应 Python 异步模型 | **成立**（语言层面对等） |
| pi-ai | 各厂 TS SDK | 各厂 Python SDK（openai / anthropic） | 语言对等 | **成立**：上游 v1.3.0 `@anthropic-ai/sdk 0.129.0`、`openai 7.19.0` |
| pi-ai | `parseStreamingJson`（partial-json） | `json-repair` | Python 生态等价容错 JSON 解析 | **成立（偏离仍在）**：上游 v1.3.0 仍依赖 `partial-json 0.1.7` |
| pi-ai | EventStream（手写队列，上游 utils/event-stream.ts） | EventStream（asyncio.Queue），移植自旧版设计 | 沿用旧版优秀设计 | 未核实（需读上游源码） |
| pi-ai | 模型清单 `models.generated.ts` | 精简硬编码（gpt-4o 系列） | 后续按需扩充或数据化 | **成立**：上游 v1.3.0 仍有 `models.generated`，且模型目录已大幅扩表 |
| pi-agent-core | 标准日志 | stdlib `logging` | 不引入私有日志包 | 成立（无上游反证） |
| pi-agent-core | storage 直接耦合 | 通过抽象接口，storage 为可选后端 | 保持 agent-core 与存储解耦 | **成立**，且上游 v1.3.0 把 harness/storage 外移（durable/chord），解耦方向一致 |
| pi-coding-agent | 含 CLI + Ink TUI + extensions + 动态目录（约 54k LOC） | 仅核心逻辑（约 35k LOC），不做 CLI/TUI | TUI 硬翻译无价值，Python 端应另用 textual 等实现 | **成立**：v1.3.0 上游 TUI 已独立为 `packages/tui`，coding-agent 仍依赖它；0.99/1.0 新增的 codemode/MCP/fullscreen TUI 继续落在裁剪范围外 |
| pi-server | Unix socket + JSONL RPC；子进程 supervisor | asyncio + Unix socket + JSONL；asyncio 子进程 | Node ↔ Python 对等 | **上游对应物已消失**（v1.2.0 起 `server`/`client`/`protocol` 包被移除）→ 该偏离需重新定义，pi-server 正在变成「孤儿包」 |
| pi-storage-sqlite | node:sqlite（DatabaseSync） | stdlib `sqlite3` | 标准库对等 | **成立（偏离仍在）** |
| pi-storage-sqlite | SQL migration 文件 | Python migration 或同等 SQL | schema 逻辑保持一致 | **需复核**：上游 sqlite 实现已迁入 `durable`，本端无法再 1:1 对照 |

### 2.2 「cherry-pick」小节

| 包 | cherry-pick 条目 |
|---|---|
| pi-ai | （暂无） |
| pi-agent-core | （暂无） |
| pi-coding-agent | （暂无） |
| pi-server | （暂无） |
| pi-storage-sqlite | （暂无） |

**5/5 包均为「暂无」**——说明 `SYNC.md` 里那个「provider 兼容性 patch 可按需 cherry-pick」的口子**从未被使用过**（CHANGELOG 里 0.84.1 是「破例同步 patch」，走的是整体同步而非 cherry-pick）。追赶时若打算启用该流程，等于启用一条没有历史实践的路径。

### 2.3 各包 v0.85.1 同步说明里的「未移植（裁剪范围）」——追赶雷区清单

这些不在「有意偏离」表格里，但在 v0.85.1 同步说明里被显式点名未移植，追赶时最容易踩：

| 包 | 未移植项 | 是否仍然成立（对 v1.3.0） |
|---|---|---|
| pi-ai | Google/Bedrock/Mistral/Codex/Responses 适配器本轮变更、动态模型目录与 `models.generated`、OAuth（Copilot/Kimi 登录）、`assistant-message-frame.ts`（harness v3 帧 reducer）、`utils/validation.ts` 的 optional-null 归一化、`detectCompat` 的 baseUrl 探测（含 DeepSeek 大小写不敏感检测与 max_tokens 切换）、Anthropic `input_transformations` 诊断、`claude-code` stealth UA | **全部仍成立**；且上游 1.x 又新增 ChatGPT 登录、Radius、Reflection AI 等 provider/auth 变化 |
| pi-agent-core | harness v3 / durable drive：storage-backed sessions、branches/lanes/values、runtime2、invocation context 贯穿、`AgentTool.replay`、`BranchSummaryMessage.fromId` 可空化、conformance/benchmark 设施、session JSONL v4 与 legacy v3 导入 | **仍成立且差距扩大**：这套东西在 v1.3.0 已演进为独立的 `durable` 包（2.46 MiB TS）+ `chord` |
| pi-coding-agent | subagent session config 继承、可配置默认工具、远程 plugin/worker 体系、模型目录刷新 | 仍成立；上游还新增 codemode/MCP/虚拟模型/分类器模型等 |
| pi-server | socket 派生自 service ID、session metadata、worker 协调 | 上游对应包已被删除，这些变更已无意义 |
| pi-storage-sqlite | harness v3 session 存储（session-backends 迁回 agent 包内部、ownership 对齐 session workers） | 上游对应包已被删除，实现移入 durable |

**另有文档漂移（建议追赶时顺手修）**：`packages/pi-storage-sqlite/README.md` 与 `PORTING.md` 仍写上游是 `@earendil-works/pi-storage-sqlite-node`、路径 `packages/storage/sqlite-node`——那是 **v0.83.0 时代**的名字；v0.84.1 起上游已改名为 `@earendil-works/pi-session-backend-sqlite-node`、路径 `packages/session-backends/sqlite-node`（我用 `contents` API 对三个 tag 逐一核对过）。

## 3. 基线健康度（全部为实际运行结果）

先 `uv sync`（workspace 已是最新，无变更）：

```
Resolved 53 packages in 19ms
Checked 50 packages in 15ms
```

### 3.1 `uv run pytest`（默认跳过 integration）—— 通过

```
platform darwin -- Python 3.13.3, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/mac/pi-py
configfile: pyproject.toml
testpaths: packages
plugins: cov-7.1.0, asyncio-1.4.0, anyio-4.14.2
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 278 items / 16 deselected / 262 selected
...
====================== 262 passed, 16 deselected in 1.54s ======================
real	0m1.963s
```

分档（`uv run pytest packages/<pkg> -q`）：

```
pi-ai => 113 passed, 7 deselected in 0.72s
pi-agent-core => 81 passed, 5 deselected in 0.53s
pi-storage-sqlite => 18 passed in 0.52s
pi-coding-agent => 33 passed, 4 deselected in 0.52s
pi-server => 17 passed in 0.81s
```

### 3.2 `uv run ruff check` —— 通过

```
All checks passed!
```

CI 里还有一步 format 检查，一并跑了：

```
$ uv run ruff format --check packages/
59 files already formatted
```

### 3.3 `uv run mypy` —— 通过（36 个源码文件）

README 与 CI 用的都是不带参数的 `uv run mypy`：

```
Success: no issues found in 36 source files
real	0m0.495s
```

但**任务指定的 `uv run mypy packages` 会失败**，原始输出如下：

```
packages/pi-ai/tests/test_integration.py: error: Duplicate module named "test_integration" (also at "packages/pi-agent-core/tests/test_integration.py")
packages/pi-ai/tests/test_integration.py: note: See https://mypy.readthedocs.io/en/stable/running_mypy.html#mapping-file-paths-to-modules for more info
packages/pi-ai/tests/test_integration.py: note: Common resolutions include:
packages/pi-ai/tests/test_integration.py: note:     a) using `--exclude` to avoid checking one of them,
packages/pi-ai/tests/test_integration.py: note:     b) adding `__init__.py` somewhere,
packages/pi-ai/tests/test_integration.py: note:     c) using `--explicit-package-bases` or adjusting `MYPYPATH`
Found 1 error in 1 file (errors prevented further checking)

real	0m0.450s
EXIT:2
```

**判定：配置/命令交互问题，不是真实类型缺陷。** 依据：
1. `pyproject.toml [tool.mypy]` 显式设置了 `files = ["packages/*/src", ...]`，即「只严格检查 src」；命令行再传 `packages` 会覆盖该 `files`，于是把各包 `tests/` 也纳入了检查。
2. 多个包存在同名测试模块（`test_integration.py` 在 pi-ai / pi-agent-core / pi-coding-agent 各一份，另有 `test_compaction_integration.py`），pytest 侧用 `--import-mode=importlib` 规避，mypy 侧没有对应配置（`explicit_package_bases` 只作用于它收到的路径集合）。
3. 只检查 src 时全绿：`uv run mypy packages/*/src` → `Success: no issues found in 36 source files`；CI 的 `uv run mypy` 同样绿。

→ 建议（供参考，不属本任务改动）：若要支持 `mypy packages`，需在配置里加 `exclude` 或把测试目录排除；否则文档/CI 应统一使用 `uv run mypy`。

### 3.4 额外：integration 测试实际跑了一次（真实 API 调用）

默认 baseline 不跑 integration；我额外按包跑了（**消耗了真实额度**，用于判断环境是否可用）：

```
pi-ai => 2 passed, 5 skipped, 113 deselected in 2.85s
pi-agent-core => 5 passed, 81 deselected in 6.78s
pi-coding-agent => 4 passed, 33 deselected in 10.30s
pi-storage-sqlite => 18 deselected in 0.47s
pi-server => 17 deselected in 0.50s
```

- 16 条 integration：**11 跑通、5 skip**。skip 的是 OpenAI（3）与 OpenCode Zen（2）用例。
- 原因：测试文件自己用 `python-dotenv` 加载根目录 `.env`（`load_dotenv(Path(__file__).resolve().parents[3] / ".env")`）。实测 `.env` 里 `DEEPSEEK_API_KEY` 非空（35 字符）、`OPENAI_API_KEY` 为空值 → 所以 DeepSeek 用例真跑且通过，OpenAI/OpenCode Zen 跳过。`ANTHROPIC_API_KEY` 未配置。
- 附带发现：为了让测试能加载 `.env`，`python-dotenv>=1.0` 被声明成了 **`pi-py-ai` 的运行时依赖**（`packages/pi-ai/pyproject.toml` 的 `[project].dependencies` 内），而非 dev 依赖——所有安装 `pi-py-ai` 的用户都会被动装上它。属轻微依赖卫生问题，建议追赶时顺手改为 dev 依赖（仅测试使用）。
- 结论：**本机具备真实 LLM 端到端验证能力（DeepSeek 通道）**，5 个包的主链路（pi-ai 流式、agent 循环+工具、coding-agent 工具）都过了真实调用。这比「只能跑 mock」的假设要好，对追赶后的回归验证是可用资产。

## 4. 发布基建

### 4.1 工作流（`.github/workflows/`）

| 文件 | 触发 | 内容 |
|---|---|---|
| `ci.yml` | push/PR 到 `main` | 矩阵 Python 3.11 / 3.12 / 3.13；`uv sync`；`ruff check packages/`；`ruff format --check packages/`；`mypy`；`pytest -q` |
| `publish.yml` | `release: published` + `workflow_dispatch` | `uv build --package` 逐个构建 5 个包 → `pypa/gh-action-pypi-publish@release/v1`，带 `skip-existing: true`；认证目前用 `secrets.PYPI_API_TOKEN`（注释里给了 OIDC Trusted Publishing 的备选路径） |

其他发布相关基建：`.github/ISSUE_TEMPLATE/sync.md`（上游同步 issue 模板，含 8 项 checklist）、`.github/dependabot.yml`、`.pre-commit-config.yaml`。

### 4.2 版本 / tag 现状（本地 + GitHub 实测）

- 5 个包 `pyproject.toml` 版本全部为 **0.85.1**，内部依赖约束为 `>=0.85.1,<0.86`（lockstep）。
- 本地与 origin 上的 tag：`v0.83.0`、`v0.84.1`、`v0.85.1`（轻量 tag，均已推送）。
- GitHub Releases：只有 **v0.83.0（2026-08-04）** 与 **v0.84.1（2026-08-18）**。
- Actions 运行记录里，`Publish to PyPI` 最后一次成功是 **2026-08-18 的 v0.84.1**（release 事件）；之后只有 CI 与 dependabot（"pip in /. - Update"），**没有任何 Publish 运行**。

### 4.3 PyPI 现状 —— v0.85.1 缺发

查询 `https://pypi.org/pypi/<pkg>/json`（HTTP 200）：

| PyPI 包 | 已发布版本 | latest | 最新上传时间 |
|---|---|---|---|
| pi-py-ai | 0.83.0, 0.84.1 | **0.84.1** | 2026-08-18T06:06:04Z |
| pi-py-agent-core | 0.83.0, 0.84.1 | **0.84.1** | 2026-08-18T06:06:03Z |
| pi-py-storage-sqlite | 0.83.0, 0.84.1 | **0.84.1** | 2026-08-18T06:06:07Z |
| pi-py-coding-agent | 0.83.0, 0.84.1 | **0.84.1** | 2026-08-18T06:06:05Z |
| pi-py-server | 0.83.0, 0.84.1 | **0.84.1** | 2026-08-18T06:06:06Z |

**判定：`UPSTREAM_VERSION` / README / CHANGELOG 说已对齐 0.85.1，但 PyPI 上用户能装到的最新版是 0.84.1。** 根因链条：`publish.yml` 只在 GitHub Release `published` 时触发 → v0.85.1 只打了 tag、没建 Release → 工作流没跑 → PyPI 停在 0.84.1。这是**发布流程断点**，不是构建失败（构建/发布步骤在 0.84.1 上是成功的，且带 `skip-existing` 可安全重跑）。

### 4.4 release / tag 命名规则与「追赶后重新发版」的步骤

命名规则（与上游版本号 1:1）：
- tag：`vX.Y.Z`，与本仓 `UPSTREAM_VERSION` 及 5 个包版本一致。
- GitHub Release 标题 = tag；`CHANGELOG.md` 里 `## X.Y.Z (YYYY-MM-DD)`（日期为**同步日**，不是上游发布日——0.85.1 就因此写成了 2026-09-07）。
- 发版由 GitHub Release 触发 `publish.yml`，5 个包同版本一起发（lockstep）。

追赶后重新发版（按现状补全的步骤，含 0.85.1 的欠账）：
1. 把 5 个包 `pyproject.toml` 的 `version` 与内部依赖区间一起升到新版本（如 `>=1.3.0,<1.4`）。
2. 更新 `UPSTREAM_VERSION`、`SYNC.md` 同步历史、`CHANGELOG.md`、`README.md` 顶部进度表与路线图。
3. 本地跑 §3 的全部命令（pytest / ruff / ruff format --check / mypy）并留下输出。
4. 补建 **v0.85.1 的 GitHub Release**（或走 `workflow_dispatch` 手动触发 publish）——否则 PyPI 永远缺这一版；`skip-existing: true` 保证重跑安全。
5. 若要发新版：打 `vX.Y.Z` tag + 建 Release（两者都要，只打 tag 不会触发发布），确认 Workflow 成功。
6. 发版后校验：PyPI 五个包的 latest 版本一致、`pip install pi-py-<pkg>` 能解析内部依赖。

## 5. 已知欠账

### 5.1 README 路线图未完成项

```
- [ ] Google / Mistral / Bedrock provider
- [ ] OAuth 鉴权（auth/*）
- [ ] 扩展系统（extensions/）
```

（已勾选：5 包基线、OpenAI/DeepSeek 真实验证、Anthropic provider 纯逻辑测试。）

### 5.2 PORTING.md 待办清单（**注意：清单已过期**）

| 包 | 未勾选项 | 实现现状核实 |
|---|---|---|
| pi-ai | 更多 provider（google/mistral/bedrock）、OAuth | 确实未实现（真实欠账） |
| pi-agent-core | agent-loop / agent / harness / proxy 共 4 项 | **已实现但未勾选**：`agent_loop.py`、`agent.py`、`harness/`（skills/session/compaction）均存在 |
| pi-coding-agent | core/agent-session、core/tools、core/extensions、session-manager 等、compaction、modes/rpc 共 6 项 | **部分已实现未勾选**：`sdk.py`(agent-session)、`tools.py` 已存在；extensions/session-manager 确为欠账 |
| pi-server | ipc、supervisor、serve/spawn/status/stop/rpc 共 3 项 | **已实现但未勾选**：`ipc.py`、`supervisor.py`、`protocol.py` 均存在 |
| pi-storage-sqlite | SqliteDatabase 抽象、migrations、session/branch/entries repo 共 3 项 | **已实现但未勾选**：`database.py`（含 migration 表与执行）、`storage.py` 均存在 |

→ 追赶前应先修这份清单：否则会把「已完成」当「待做」，把 3~4 个包的工作量估高。

### 5.3 TODO / FIXME 统计

- `packages/**/*.py`：`grep -rn -E 'TODO|FIXME|XXX|HACK' --include='*.py' packages/` → **0 条**。
- 全仓 `*.md`（排除 .venv）：`TODO|FIXME` → **0 条**。
- 即代码/文档里没有留下 TODO 标记，欠账只能从 README 路线图、PORTING 待办、以及上游 diff 三处推断。

### 5.4 其他基建欠账

- **v0.85.1 未发 PyPI**（§4.3），且按现在规则，若不补 Release，追赶后一次性发新版会让 0.84.1 → 新版之间出现一个"文档说支持但装不到"的版本。
- mypy 的 `uv run mypy packages` 用法与配置冲突（§3.3）。
- pi-storage-sqlite 文档指向上游旧包名/路径（v0.83.0 时代，§2.3）。
- `docs/superpowers/` 下只有 0.83.0 / 0.84.1 两轮适配的设计与计划文档，0.85.1 没有对应的设计文档（只有 CHANGELOG + PORTING 说明）。

---

# 第二部分：上游发布节奏分析

## 6. 发布节奏表（v0.85.1 → v1.3.0，共 16 个 release，无遗漏）

口径：日期取 GitHub Release `published_at` 的 UTC 日期；「距上一个」按精确时间戳计算；类型按 semver（x.y.0=minor，x.0.0=major，其余 patch）；「Breaking 条目」= release notes 中 `### Breaking Changes` 段落下的一级 bullet 数（我用脚本按 heading 解析，不是关键词匹配）。

| # | tag | 发布日期 (UTC) | 距上一个 release | 类型 | Breaking 条目 | notes 条目 |
|---:|---|---|---:|---|---:|---:|
| 1 | `v0.85.1` | 2026-09-05 | +1.09 天 | patch | 0 | 7 |
| 2 | `v0.86.0` | 2026-09-19 | +14.45 天 | minor | 3 | 66 |
| 3 | `v0.86.1` | 2026-09-20 | +0.50 天 | patch | 0 | 8 |
| 4 | `v0.87.0` | 2026-09-21 | +1.30 天 | minor | 5 | 24 |
| 5 | `v0.87.1` | 2026-09-22 | +1.05 天 | patch | 0 | 10 |
| 6 | `v0.99.0` | 2026-09-29 | +6.90 天 | minor | 0 | 65 |
| 7 | `v0.99.1` | 2026-09-29 | +0.05 天 | patch | 0 | 4 |
| 8 | `v0.99.2` | 2026-09-30 | +1.04 天 | patch | 0 | 26 |
| 9 | `v1.0.0` | 2026-10-01 | +0.99 天 | major | 0 | 32 |
| 10 | `v1.0.1` | 2026-10-03 | +1.87 天 | patch | 0 | 27 |
| 11 | `v1.0.2` | 2026-10-04 | +0.36 天 | patch | 0 | 2 |
| 12 | `v1.0.3` | 2026-10-05 | +1.32 天 | patch | 1 | 10 |
| 13 | `v1.0.4` | 2026-10-05 | +0.56 天 | patch | 0 | 12 |
| 14 | `v1.1.0` | 2026-10-07 | +2.02 天 | minor | 0 | 38 |
| 15 | `v1.2.0` | 2026-10-12 | +5.03 天 | minor | 4 | 43 |
| 16 | `v1.3.0` | 2026-10-15 | +2.84 天 | minor | 0 | 32 |

- 16 行覆盖窗口内全部 release，日期与 tag 一一对应（`releases` API 全量 274 条中截取 v0.85.1..v1.3.0）。
- 窗口内 Breaking 条目合计 **13 条**，出现在 v0.86.0(3)、v0.87.0(5)、v1.0.3(1)、v1.2.0(4)。
- **注意 v1.0.0（major）Breaking 段为 0**，v0.99.0（minor，含 TypeScript 7 构建切换、主题体系变更、ChatGPT 登录、MCP/codemode）也没有 Breaking 段——见 §8 的告警。
- 窗口 40.28 天（2026-09-05T12:29:01Z → 2026-10-15T19:17:33Z）共 16 个 release：跨度/个数 = 2.52 天，**15 个间隔的平均值 = 2.69 天/个**；分布极不均匀（最短 0.05 天，最长 14.45 天）。

## 7. 异常点：v0.87.1 → v0.99.0 的版本号跳跃

### 7.1 事实（全部有命令证据）

1. **release 序列**：v0.87.1（2026-09-22T19:43:43Z）之后下一个 release 直接是 v0.99.0（2026-09-29T17:21:54Z），中间 **6.90 天、零 release**（`gh api repos/earendil-works/pi/releases`，全量 274 条）。
2. **tag 序列**：拉取全部 tags（4 页共 330 个，其中 329 个 semver）后核对，**不存在任何 v0.88.x ~ v0.98.x 的 tag**；semver tag 序列里 0.87.1 的下一个就是 0.99.0。
3. **逐个点名核实**（`git/ref/tags/<tag>`）：`v0.88.0`、`v0.90.0`、`v0.95.0`、`v0.98.0`、`v0.98.2` **全部 HTTP 404 Not Found**。
4. **changelog 序列**：`packages/agent/CHANGELOG.md` @ v0.99.0 里，`## [0.99.0] - 2026-09-29` 直接接在 `## [0.87.1] - 2026-09-22` 之后，没有 0.88~0.98 的任何条目。
5. **发布 commit**：`4b060d3a` "Release v0.99.0" 一次性把 39 个文件里的版本从 `0.87.1` 改成 `0.99.0`（含 `packages/agent/package.json`、`packages/ai/package.json`、lockfile、shrinkwrap 等），**没有任何中间版本痕迹**。
6. **工作量对不上版本号**：`compare/v0.87.1...v0.99.0` 显示只有 **87 个 commit**（2026-09-22 → 2026-09-29）。也就是说这次跳号并非「把 12 个 minor 的改动合并成一次发布」，而是直接改写了版本号。
7. **是人为显式指定**：上游 `scripts/release.mjs` 用法是 `node scripts/release.mjs <major|minor|patch|x.y.z>`（同时支持显式 `x.y.z`，正则 `^\d+\.\d+\.\d+$`）；而 `npm run version:minor` 走的是 `npm version minor --workspaces`，只会得到 0.88.0。实际结果是 0.99.0 → **发布者是显式指定了版本号**。

### 7.2 「0.88~0.98 是不是内部版本号？」

**不是。** 这些版本号在上游仓库里根本不存在：既无 tag、也无 release、也无 changelog 条目（`search/commits` 查询 `0.99.0` 只返回 3 个提交，全部属于这次发布本身；未检索到任何 0.88~0.98 的发布提交）。作为对比，历史上真正的「内部版本」长这样：v0.14.2 → v0.18.1 之间，**v0.15.0 / v0.16.0 / v0.17.0 的 tag 是存在的**，只是没写 release notes——那才叫「tag 有、release 没有」。0.88~0.98 连 tag 都没有。

### 7.3 为什么偏偏是 0.99.0？

**无法确定。** 我核查过以下所有公开位置，均无说明：
- v0.99.0 的 release notes（15,468 字符，只有 New Features / Added / Changed / Fixed 四段，无版本说明）；
- 各包 `CHANGELOG.md` 的 0.99.0 段；
- `AGENTS.md`（只有 changelog 段格式约定，无版本策略说明）；
- `.pi/skills/release.md`（版本策略原文："**Lockstep versioning**: all packages share one version… `patch` = fixes + additions, `minor` = breaking changes. **No major releases.**"）；
- 相关 commit message（`docs: audit changelogs for 0.99.0`、`Release v0.99.0` 均无正文说明）；
- GitHub issue / PR / commit 搜索（`0.99.0`、`0.88`、`why 0.99`、`version jump`、`1.0` 等关键词）：命中项都是无关的 bug/feature 报告，**没有解释版本号选择的条目**。

可以确认的**旁证**（不等于原因）：跳号后 **2 天（2026-10-01）就发布了 1.0.0**。这符合「用 0.99.0 作为 1.0 前最后一站」的常见做法，但**上游没有任何文字确认这一点，我不据此下结论**。

同时注意：上游自己的 release 策略写的是「No major releases」，而 0.99.0 之后立刻出了 1.0.0——**这份策略在 1.0 时已被实际打破**，这也说明拿它的版本语义去推「什么时候该同步」并不可靠。

### 7.4 另一个值得记的节奏异常

窗口内有一段 **14.45 天的空窗**（v0.85.1 → v0.86.0，2026-09-05 → 2026-09-19）。这段空窗横跨了本仓的同步日（2026-09-07）——即本仓在「上游刚静默两周、随后进入高频期」的节点上完成了上次同步，之后就没再追。

## 8. 节奏结论：进入 1.x 后频率升、显式破坏性变更密度降，但「minor=破坏性」契约失效

### 8.1 频率：1.x 明显更快

| 区间 | release 数 | 跨度 | release/周 | 间隔中位数 | 间隔均值 | 最大间隔 |
|---|---:|---:|---:|---:|---:|---:|
| W1：0.x 尾段 v0.85.1..v0.99.2 | 8 | 25.29 天 | **2.21** | 1.05 天 | 3.61 天 | 14.45 天 |
| W2：1.x v1.0.0..v1.3.0 | 8 | 14.00 天 | **4.00** | 1.87 天 | 2.00 天 | 5.03 天 |
| W3：1.0.0 之前最近 40 个 release | 40 | 114.18 天 | 2.45 | 1.39 天 | 2.93 天 | 14.45 天 |
| 全历史 v0.12.0..v1.3.0 | 274 | 317.33 天 | 6.04 | 0.49 天 | 1.16 天 | 14.45 天 |

- **1.x 的节奏比 0.x 尾段快约 1.8 倍**（4.00 vs 2.21 release/周），而且比 0.x 尾段**稳定**（均值 2.00 天 ≈ 中位 1.87 天，没有 14 天级空窗）。
- `SYNC.md` 里「上游平均 1.7 天发一个 release」这句话：**量级仍然对得上**（1.x 中位 1.87 天、均值 2.00 天；全历史均值 1.16 天），但它当初的推论「所以不追 patch，只追 minor」在 1.x 已不成立——见 8.3。
- 月度分布：2026-07 = 11 个、2026-08 = 5 个、2026-09 = 9 个、2026-10（截至 15 日）= 8 个。8 月是个低谷，9 月中之后重新加速。

### 8.2 破坏性变更密度：显式标注略降，但标注本身不可信

| 区间 | Breaking 条目合计 | 有 Breaking 段的 release | 条/release | notes 条/release |
|---|---:|---:|---:|---:|
| W1（0.x 尾段，8 个） | 8 | 2/8 | **1.00** | 26.2 |
| W2（1.x，8 个） | 5 | 2/8 | **0.62** | 24.5 |
| W3（0.x 最近 40 个） | 28 | 7/40 | 0.70 | 22.5 |
| 全历史 | 135 | 49/274 | 0.49 | 11.1 |

- 单看「显式标注的 Breaking」，1.x 比 0.x 尾段**略降**（0.62 vs 1.00 条/release），与 W3 的 0.70 基本同档。**不能得出「1.x 更安全」的结论**，原因见下。
- 每次 release 的 notes 体量几乎不变（26.2 → 24.5 条/release），但**频率翻了近一倍** → 单位时间内需要消化的上游变更量约翻倍。
- **标注不可信的三个反例**（都在窗口内）：
  1. `v0.99.0`：Breaking 段 0 条，但实际含「TypeScript native preview → TypeScript 7 / ES2024，tsx → Node type stripping」的构建体系更换、主题默认值与配色体系更换、`[Themes]` 段移除、内建扩展命名从 `<built-in:name>` 改为 `builtin:<name>` 等**明确会破坏使用方**的变化，还首次引入 MCP/codemode。这些都没进 Breaking 段。
  2. `v1.0.0`：major 版本，Breaking 段 0 条（全文只有 New Features/Added/Changed/Fixed）。
  3. `v1.0.3`：patch 版本，却带 1 条 Breaking（Azure provider 从 `azure-openai-responses` 改名为 `azure`，用户需改 `auth.json`/`models.json`/`settings.json`）。
- 反向也成立：`v1.1.0` / `v1.3.0` 是 minor，Breaking 段 0 条，主体是 feature（5 条 New Features）。按上游自己的规则「minor = breaking changes, patch = fixes + additions」，**这两类版本已经反着来了**。

### 8.3 对 SYNC.md「只在 0.x.0 minor 同步」的判断

该规则的两个前提逐一失效：

| 前提 | 现状 | 证据 |
|---|---|---|
| 上游存在 `0.x.0` 这种 minor 节奏 | **失效**：上游已进入 1.x（v1.3.0），且 1.x 的 minor 语义已漂移 | §8.2；v1.1.0/v1.3.0 minor 无 breaking |
| minor 之间通常不引入破坏性 API 变更，patch 可以不管 | **失效**：破坏性变更会出现在 patch（v1.0.3），feature 也会出现在 patch（v1.0.1/v1.0.4）；而「标了 Breaking 的 minor」只有 2/8 | §6 表格、§8.2 |
| 「追 patch 不现实」（1.7 天/release） | **仍成立，且更强**：1.x 实测中位 1.87 天、4.00 release/周 | §8.1 |

**结论：`SYNC.md` 的同步规则已经失效，需要重写。** 具体地：
- 「按 0.x.0 minor 触发」不再有可触发的对象；
- 「minor 之间通常不引入破坏性 API 变更」被上游自己的发布记录证伪（patch 里会出现 provider 改名级别的 breaking，minor 里也会出现纯 feature）；
- 唯一仍然成立的是「不能逐个 patch 追」——但这恰恰说明需要一个**与版本号脱钩的节拍机制**。

### 8.4 供 Lead 决策的选项（我不替 Lead 拍板）

| 方案 | 触发条件 | 优点 | 代价 |
|---|---|---|---|
| A. 固定节拍 | 每 N 周（如 2 周）一次集中同步到当时最新 tag | 可预测、可排期；不依赖上游版本语义 | 破坏性 patch 最长滞后 N 周 |
| B. 固定节拍 + breaking 例外 | A + 监控 release notes 的 Breaking 段与 `Changed/Removed` 段，命中即提前 cherry-pick/同步 | 兼顾节拍与兼容性风险 | 需要维护「哪些 patch 值得破例」的判断流程（目前 5 包 cherry-pick 历史为 0，无现成流程） |
| C. 事件驱动（跟随上游 minor + 关键 patch） | 每个 minor + 人工判定为 breaking 的 patch | 省事 | 上游 minor 语义已失效，实际退化为「看心情」，且 4 release/周下 patch 占比高 |

无论选哪个，建议同时做两件事（数据都支持）：
1. 把 `UPSTREAM_VERSION` 从「只记版本号」升级为「版本号 + 上游 commit sha」（本次跳号证明单靠版本号无法判断基线间距：0.87.1→0.99.0 只有 87 个 commit，而 0.85.1→1.3.0 有 676 个）。
2. 在规则里显式写明「以 ci.yml 的 4 条命令 + integration（DeepSeek 通道）作为同步完成判据」——本机已具备真实 API 验证能力（§3.4）。

---

## 附录 A：数据来源与复核命令

| 数据 | 命令 |
|---|---|
| 上游全量 release（274 条） | `gh api 'repos/earendil-works/pi/releases?per_page=100&page=N'`（1..3） |
| 上游全量 tag（330 个） | `gh api 'repos/earendil-works/pi/tags?per_page=100&page=N'`（1..4） |
| 版本跳跃点名核实 | `gh api repos/earendil-works/pi/git/ref/tags/v0.90.0`（404）等 |
| 0.87.1→0.99.0 提交数 | `gh api repos/earendil-works/pi/compare/v0.87.1...v0.99.0`（`total_commits=87`） |
| 0.85.1→1.3.0 提交数 | `gh api repos/earendil-works/pi/compare/v0.85.1...v1.3.0`（`total_commits=676`，列表被 API 截断到 250） |
| 发布 commit | `gh api repos/earendil-works/pi/commits/4b060d3a`（39 文件，0.87.1→0.99.0） |
| 上游包体积 | `gh api repos/earendil-works/pi/git/trees/<tag>?recursive=1` 后按 `packages/<pkg>` 汇总 blob size |
| 上游发布脚本/策略 | `gh api repos/earendil-works/pi/contents/scripts/release.mjs?ref=v0.99.0`、`.pi/skills/release.md`、`AGENTS.md` |
| 本仓行数 | `find packages/*/src -name '*.py' -exec cat {} + \| wc -l`；测试同理 |
| 本仓用例数 | `uv run pytest --collect-only -q \| tail -3`；分包 `uv run pytest packages/<pkg> -q` |
| PyPI 版本 | `curl -s https://pypi.org/pypi/<pkg>/json`（5 个包） |
| 本仓发布记录 | `gh api 'repos/encyc/pi-py/releases?per_page=20'`、`gh api 'repos/encyc/pi-py/actions/runs?per_page=20'`、`git ls-remote --tags origin` |

## 附录 B：口径与不确定性声明

1. **「Breaking 条目」是我的解析口径**：release notes 中 `### Breaking Changes` heading 之后、下一个 heading 之前的一级 bullet 数。上游不保证把所有破坏性变更都放进该段（§8.2 已举 3 个反例），所以该指标是**下界**，不代表真实破坏性变更规模。
2. **上游包体积**用 git tree blob `size` 汇总（含 docs/tests，非纯 LOC），只用于说明量级与结构漂移，不能当 LOC 用。
3. **compare API 会截断**：`v0.85.1...v1.3.0` 只返回前 250 条 commit、300 个文件，因此「变更文件数/增删行数」不可用；`total_commits`、`ahead_by` 字段可信（676）。
4. **无法确定项**：v0.87.1→v0.99.0 跳号「为什么是 0.99.0」——公开数据无说明，已在 §7.3 明示，未做推测性结论。
5. **未核实项**：PORTING 里少数纯实现细节（如 EventStream 设计来源、sqlite migration 对等性）需要读上游源码逐行比对，本轮只做 API 级核实，已在表格中标「未核实/需复核」。
6. 本报告只读盘点：除 `recon/03-pi-py-state-and-cadence.md` 外未改动仓库任何文件；`uv run pytest/ruff/mypy` 只写各自缓存目录（`.pytest_cache`/`.ruff_cache`/`.mypy_cache`，仓库本就忽略）。

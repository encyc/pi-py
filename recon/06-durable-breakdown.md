# 侦察报告 06：`@earendil-works/pi-durable` v1.3.0 模块拆分与最小可行集

- 编写：durable-scout（task-6），2026-10-18
- 上游基线：`earendil-works/pi` **v1.3.0**（2026-10-15），源码树 `/tmp/pi-up/v1.3.0/packages/durable/`（**只读**）
- 旧版对照：`/tmp/pi-up/v0.85.1/packages/agent/src/harness/`（21,851 行）+ `packages/session-backends/sqlite-node/`（4,126 行）
- 方法：命令算行数 → 实读 `package.json` / import 语句建图 → 用脚本算**运行时闭包**与**类型闭包** → 与上游自带的 `test/storage-runtime-boundary.test.ts` 交叉验证
- 结论口径：所有结论附 `文件:行号` 或命令；无法确定项集中在 §7

---

## 0. 结论速览（先看这段）

| 问题 | 结论 | 依据 |
|---|---|---|
| 总规模 | **24,989 行 / 82 个 `.ts`**（`src/`），另有 **38,793 行 / 112 个测试文件**、**5,352 行规范文档** | §1.1 |
| 最大单文件 | `harness/scheduler.ts` **1,953 行**；`testing/storage-conformance.ts` 1,663；`env/node.ts` 1,226 | §1.2 |
| 主要外部依赖 | **chord 68 个 import 点（44/82 文件）**、pi-ai 24 个、pi-codemode 5 个、typebox 7 个、node 内置 5 文件 | §2.2 |
| **只做 L0 能否构成可发布自洽的 pi-durable？** | **不能**。技术上能编译（K0 内核 9 文件 / 3,977 行运行时闭包无 `harness/`、`env/` 运行时依赖），但 L0 只保留 `index.ts` **203 个导出里的 97 个（48%）**，且丢掉的 **22 个值导出全是运行时核心**（`Harness` `ToolTask` `GenerationTask` `CompactionTask` `createRegistry` `watchEvents` `defineExtension` `hook` `wrapTool` …）——**留下的是一半的名字、几乎为零的可运行能力**；而且**没有任何消费者**——上游唯一消费者 `packages/env` 从不 import 内核。**最小可行集应该是「env 岛」而不是「L0 内核」** | §4 |
| L0 的独立验收 | **不可能**。L0 的语义口径是 `testing/storage-conformance.ts`（1,663 行），按任务定义属于 L3 | §4.3 |
| 孤岛性 | `pi-durable` 全仓消费者**只有 `packages/env`（`@earendil-works/pi-env`）**，且只用 `./env`、`./env/node` 两个子路径（另 3 个 import 点在 env 的测试里）。`coding-agent` **0 引用** | §2.4 |
| 建议批次 | **5 批：A env 岛 → B 内核+一致性套件 → C 工具与镜像 → D harness/scheduler → E codemode+平台适配** | §6 |
| 总工作量 | **约 41–58 人周**（不含 chord/pi-ai/pi-codemode 自身的移植）。Lead 简报里的「数人月」偏乐观 | §6.6 |
| 最大的好消息 | ① 有 **5,352 行 normative spec**；② 有**可执行的移植边界测试**；③ 一致性套件**与测试框架解耦**（6 个断言原语），pytest 适配约 40 行 | §4.4 |

---

## 1. 模块清单与规模

### 1.1 总量核对

```bash
cd /tmp/pi-up/v1.3.0/packages/durable
find src -type f -name '*.ts' | wc -l          # 82
find src -type f -name '*.ts' | xargs cat | wc -l   # 24989
```

**82 个文件 / 24,989 行**，与简报的「~25k 行 / 82 个 src 文件」**完全吻合**。

按目录汇总（`src/` 下每个目录的**直接** `.ts` 行数，相加 = 24,989）：

| 行数 | 文件数 | 目录 | 说明 |
|---:|---:|---|---|
| **8,177** | 21 | `src/harness/` | 最大的一块：调度器、生成、工具任务、压缩、扩展、事件 |
| **2,935** | 7 | `src/testing/` | 一致性套件 + 基准 |
| **2,513** | 4 | `src/session/` | Session 内核、事务、观察、分叉 |
| **2,300** | 5 | `src/env/` | 可移植环境接口 + Node 适配 |
| **1,936** | 9 | `src/`（顶层文件） | `types.ts`、`documents.ts`、`errors.ts`、`ids.ts`、`index.ts`、`search.ts`、`tasks.ts`、`truncate.ts`、`entries.ts` |
| **1,698** | 5 | `src/codemode/` | code mode 集成（依赖 pi-codemode） |
| **1,620** | 7 | `src/storage/sqlite/` | SQLite 后端（含 Cloudflare 适配） |
| **1,551** | 11 | `src/tools/` | read/write/edit/bash/powershell + 图像处理 |
| **904** | 2 | `src/storage/` | `memory.ts` + `scan.ts` |
| **866** | 3 | `src/storage/jsonl/` | JSONL 后端 |
| **390** | 6 | `src/images/` | Photon(WASM) 图像处理 |
| **99** | 2 | `src/tool-search/` | BM25 工具搜索 |
| **24,989** | **82** | | ✅ 相加吻合 |

递归汇总（含子目录）供交叉参考：

| 行数 | 目录树 |
|---:|---|
| 8,177 | `src/harness` |
| 3,390 | `src/storage`（含 sqlite 1,620 + jsonl 866 + 904） |
| 2,935 | `src/testing` |
| 2,513 | `src/session` |
| 2,300 | `src/env` |
| 1,698 | `src/codemode` |
| 1,551 | `src/tools` |
| 390 | `src/images` |
| 99 | `src/tool-search` |

### 1.2 逐文件行数（降序，完整 82 个文件）

```
   1953 src/harness/scheduler.ts        1663 src/testing/storage-conformance.ts
   1226 src/env/node.ts                 1154 src/types.ts
   1081 src/session/session.ts          1043 src/session/transaction.ts
   1037 src/codemode/codemode.ts         957 src/storage/sqlite/storage.ts
    911 src/harness/tool.ts              864 src/storage/memory.ts
    846 src/storage/jsonl/storage.ts     812 src/harness/types.ts
    715 src/harness/generation.ts        633 src/testing/env-conformance.ts
    522 src/env/node-watch.ts            506 src/harness/events.ts
    500 src/tools/edit-diff.ts           489 src/testing/storage-benchmark.ts
    470 src/harness/harness.ts           464 src/harness/compaction.ts
    377 src/env/index.ts                 333 src/harness/context.ts
    314 src/harness/agent.ts              312 src/tools/read.ts
    292 src/session/observation.ts       285 src/codemode/models.ts
    268 src/harness/live.ts              267 src/harness/view.ts
    241 src/harness/submissions.ts       236 src/harness/task-graph.ts
    216 src/storage/sqlite/node.ts       208 src/documents.ts
    206 src/codemode/output.ts           205 src/index.ts
    192 src/storage/sqlite/cloudflare.ts 184 src/tools/bash.ts
    182 src/tools/image.ts               174 src/truncate.ts
    168 src/harness/inbox.ts             158 src/harness/prompt.ts
    157 src/codemode/describe.ts         145 src/env/line-scan.ts
    144 src/tools/edit.ts                140 src/storage/sqlite/migrations.ts
    140 src/images/index.ts              130 src/images/node.ts
    114 src/harness/registry.ts           97 src/session/forks.ts
     92 src/tool-search/tool-search.ts     85 src/search.ts
     72 src/harness/usage.ts               70 src/images/exif.ts
     69 src/storage/sqlite/schema.ts       61 src/harness/util.ts
     58 src/tools/image-processor.ts       57 src/errors.ts
     55 src/tools/file-mutation-queue.ts   46 src/testing/types.ts
     44 src/harness/define.ts              40 src/storage/scan.ts
     39 src/tools/write.ts                 39 src/tools/index.ts
     39 src/testing/assertions.ts          39 src/harness/provider.ts
     38 src/testing/runner.ts              38 src/storage/sqlite/database.ts
     35 src/images/node-worker.ts          34 src/entries.ts
     31 src/harness/json.ts                30 src/tools/path-utils.ts
     30 src/env/decode.ts                  27 src/testing/index.ts
     14 src/storage/jsonl/node.ts          13 src/codemode/index.ts
     11 src/ids.ts                         10 src/images/cloudflare.ts
      8 src/tools/env.ts                    8 src/tasks.ts
      8 src/storage/sqlite/index.ts         7 src/tool-search/index.ts
      6 src/storage/jsonl/index.ts          5 src/images/photon-wasm.d.ts
```

> 注：`src/images/photon-wasm.d.ts`（5 行）是 WASM 模块的类型声明，不是可执行代码，但计入 82 个文件。

### 1.3 不属于 `src/` 但必须计入排期的资产

| 资产 | 规模 | 对移植的意义 |
|---|---:|---|
| `test/**/*.ts` | **38,793 行 / 112 个文件** | **比 `src/` 还大 55%**。若验收标准是「上游测试全过」，工作量翻倍。见 §6.6 |
| `docs/spec.md` | **5,352 行** | **normative 规范**，§13 还列了 11 条 Non-goals。这是本次移植**最大的降风险资产**——可以对着规范写，而不是逆向代码 |
| `README.md` | 768 行 | 含 Quick Start / Errors / Storage 等 34 节 |
| `CHANGELOG.md` | 144 行 | 4 节 Breaking / 14 条，见 §5.6 |

**test 目录按体量排序（前 10）**：`harness-compaction`(2,392)、`harness-structured`(1,914)、`harness-tasks`(1,392)、`harness-ownership`(1,376)、`harness-nested-tools`(1,343)、`harness-tools`(1,200)、`jsonl-storage`(1,156)、`codemode`(1,073)、`env-node`(1,002)、`harness-context`(982)。

---

## 2. 依赖图

### 2.1 内部模块依赖（基于实读 import 语句）

拓扑上分四层，箭头方向 = 依赖方向：

```
                      ┌───────────────────────────────────────────┐
   L3 集成层          │ codemode/  tools/  images/  tool-search/  │
                      │ testing/   env/                           │
                      └───────────────┬───────────────────────────┘
                                      │
                      ┌───────────────▼───────────────────────────┐
   L2 harness         │ harness/scheduler.ts (1953)               │
                      │ harness/harness.ts    harness/generation  │
                      │ harness/tool.ts       harness/compaction  │
                      │ harness/{agent,context,live,view,events,  │
                      │         inbox,prompt,provider,registry,   │
                      │         submissions,task-graph,usage,     │
                      │         json,util,define,types}           │
                      └───────────────┬───────────────────────────┘
                                      │
                      ┌───────────────▼───────────────────────────┐
   L1 任务与文档      │ harness/types.ts   tasks.ts   entries.ts  │
                      │ documents.ts                              │
                      └───────────────┬───────────────────────────┘
                                      │
                      ┌───────────────▼───────────────────────────┐
   L0 内核            │ types.ts (1154, Storage/Session/Tx 契约)  │
                      │ session/{session,transaction,observation, │
                      │         forks}.ts                         │
                      │ storage/{memory,scan,jsonl,sqlite}        │
                      │ env/index.ts (可移植环境接口)              │
                      └───────────────────────────────────────────┘
```

**关键观察 1：`types.ts` ↔ `harness/types.ts` 是循环依赖，但全是 `import type`。**

```bash
grep -n 'import' src/types.ts        # :12  from "./harness/types.ts"
grep -n 'import' src/harness/types.ts # :41  from "../types.ts"
```

`src/types.ts:1-12` 与 `src/harness/types.ts:1-44` 的**全部** 8 条 import 都是 `import type`，运行时全部擦除。

`types.ts` 从 `harness/types.ts` 只取 6 个类型，且**全部只出现在 `TaskRuntime`（`types.ts:170-243`）里**：

| 符号 | 唯一使用点 | 所在接口 |
|---|---|---|
| `RegistrySnapshot` | `types.ts:179` | `TaskRuntime` |
| `Agent` | `types.ts:181` | `TaskRuntime` |
| `Settings` | `types.ts:183` | `TaskRuntime` |
| `SettledTask` | `types.ts:210` | `TaskRuntime` |
| `ConversationHandle` | `types.ts:227` | `TaskRuntime` |
| `ContextView` | `types.ts:233` | `TaskRuntime` |

**这是「L0 能否独立」的胜负手**：`Storage`（`types.ts:1059`）、`Session`（`types.ts:950`）、`Tx`（`types.ts:790`）三个契约**完全不引用 harness 类型**。L0 与 L2 的唯一联系是 `TaskRuntime` 这一个接口。

**关键观察 2：`env/index.ts` 被 L0 反向依赖，但只是类型层。**
`types.ts:4` `import type { ExecutionEnv } from "./env/index.ts"`。因此 `env/index.ts`（377 行）虽在 L3 目录下，却是 L0 的**类型前置**。同理 `storage/jsonl/storage.ts:2` `import type { FileError, FileSystem } from "../../env/index.ts"`。

### 2.2 对外部包的依赖（实测）

```bash
grep -rhoE 'from "[^".][^"]*"' --include='*.ts' src/ | sed 's/from "//;s/"$//' \
  | sed 's|^\(@[^/]*/[^/]*\).*|\1|' | sort | uniq -c | sort -rn
```

| 包 | import 点 | 涉及文件 | 性质 |
|---|---:|---:|---|
| `@earendil-works/chord` | **68** | **44 / 82** | 贯穿全部层，见 §2.3 |
| `@earendil-works/pi-ai` | 24 | 15 | 模型层，见 §2.5 |
| `typebox` | 7 | 6 | 运行时 JSON Schema 校验 |
| `node:fs` | 7 | 5 | Node 专属 |
| `@earendil-works/pi-codemode` | 5 | 4 | 仅 `codemode/`，见 §2.6 |
| `node:path` | 3 | 3 | Node 专属 |
| `node:worker_threads` | 2 | 2 | 仅 `images/` |
| `node:sqlite` | 2 | 1 | 仅 `storage/sqlite/node.ts` |
| `node:module` | 2 | 2 | 仅 `images/` |
| `node:crypto` | 2 | 2 | `env/node.ts`、`env/node-watch.ts` |
| `@silvia-odwyer/photon` | 2 | 2 | Rust→WASM 图像库 |
| `node:url` / `node:os` / `node:child_process` | 各 1 | `env/node.ts` | Node 专属 |
| `diff` | 1 | `tools/edit-diff.ts` | 文本 diff |

**包声明**（`package.json:60-67`）：

```json
"dependencies": {
  "@earendil-works/chord": "1.3.0",
  "@earendil-works/pi-ai": "1.3.0",
  "@earendil-works/pi-codemode": "1.3.0",
  "@silvia-odwyer/photon": "0.3.3",
  "diff": "8.0.4",
  "typebox": "1.3.27"
}
```

`engines: { "node": ">=22.19.0" }`（`package.json:56-58`）——因为用了 `node:sqlite` 与 `Promise.withResolvers`。
`"sideEffects": false`（`package.json:53`）。

### 2.3 ★ 哪些模块依赖 chord（与 task-5 呼应）

**按子路径统计 import 点：**

| chord 子路径 | import 点 |
|---|---:|
| `@earendil-works/chord`（根） | 43 |
| `@earendil-works/chord/delta` | 12 |
| `@earendil-works/chord/context` | 10 |
| `@earendil-works/chord/output` | 3 |

**按「运行时闭包」计算各切片的 chord 需求（值导入 = 运行时真需要，类型导入可 `TYPE_CHECKING` 规避）：**

| 切片 | 运行时文件数 / 其中 import chord | **值导入 chord 符号** | 类型导入 chord 符号 |
|---|---|---|---|
| **K0 内核** | 9 / 7 | **6**：`BACKGROUND_CONTEXT` `awaitWithContext` `copyJson` `replicatedState` `track` `withoutAbortSignal` | 11 |
| **K2b 内核+sqlite+node** | 15 / 8 | **7**：上述 6 + `apply` | 11 |
| **env 岛** | 5 / 3 | **0（零！）** | 1：`Context` |
| **tools 岛** | 16 / 7 | **2**：`characterEnd` `utf8ByteLength`（来自 `chord/output`） | 2 |
| **codemode 岛** | 31 / 24 | **12**：+`OutputBuffer` `Progress` `PROGRESS_BYTES_PER_SECOND` `boundOutput` `overlap` | 13 |
| **PUB 根入口 `index.ts`** | 34 / 28 | **15**：+`withAbortSignal` `applyImmutable` `applyImmutableBatches` | 15 |

**★ 最重要的一条：`env` 岛是唯一零运行时 chord 依赖的切片。**
`env/index.ts:1` 只有 `import type { Context } from "@earendil-works/chord";`。这意味着 **`pi_durable.env` 可以在 chord 尚未移植的情况下先做完**——只需在 Python 侧定义一个 `Context` 协议（`Protocol`/`TYPE_CHECKING`），不必等 task-5。

**chord 在 durable 里承担的 5 件事**（按耦合深度）：

1. **`Context`（取消上下文）贯穿 620 处**——`grep -rho 'Context' src | wc -l` = 620，出现在几乎每个公开 API 签名里。**这是最深、最贵的耦合**：Python 侧要么对应 `contextvars` + `asyncio.CancelledError`，要么自造一个 token 对象。
2. **`chord/delta` 的 `Op`/`track`/`apply`/`applyImmutable(Batches)`**——文档的 CRDT 式增量表示。**直接决定 `StorageWrite.document.change` 的数据形态**（`types.ts:751-780`）。
3. **`replicatedState`/`AttachedReplicatedState`**——`session/session.ts:1`、`harness/view.ts:1`、`harness/task-graph.ts:1` 用它挂载文档状态。
4. **`chord/context` 的 `BACKGROUND_CONTEXT`/`awaitWithContext`/`withAbortSignal`/`withoutAbortSignal`**——取消传播原语。
5. **`chord/output` 的 `OutputBuffer`/`boundOutput`/`utf8ByteLength`/`characterEnd`**——工具输出的有界缓冲与截断。

> 不重复 task-5 的调研。此处只给 durable 侧的**用量与契约点**：task-5 需要交付的是 `Context`、`delta`（Op/Tracker/apply）、`replicatedState`、`context` 四个子路径的最小可用实现，`output` 可后置到批次 C。

### 2.4 ★ durable 的消费者（孤岛性实证）

```bash
cd /tmp/pi-up/v1.3.0
grep -rhoE 'from "@earendil-works/pi-durable[^"]*"' --include='*.ts' packages/ --exclude-dir=durable | sort | uniq -c
```

排除 durable 自身后，**全仓只有 14 个 import 点，全部在 `packages/env`**：

| specifier | 点 | 位置 |
|---|---:|---|
| `@earendil-works/pi-durable/env` | 8 | `env/src/{errors,watch,remote-env,list-files}.ts` |
| `@earendil-works/pi-durable/env/node` | 2 | `env/src/list-files.ts:13` + 测试 |
| `@earendil-works/pi-durable/testing` | 2 | `env/test/conformance.test.ts:4`、`ssh-external.test.ts:6`（**仅测试**） |
| `@earendil-works/pi-durable/tools` | 1 | `env/test/differential.test.ts:10`（**仅测试**） |
| `@earendil-works/pi-durable`（根） | 1 | `env/test/differential.test.ts:7`，且是 **`import type { ToolExecutionApi }`（仅类型）** |

**三条硬结论：**

1. `pi-durable` 的 `.` 根入口（203 个导出、12,633 行运行时闭包）**在生产代码里零消费者**。
2. `packages/env`（`@earendil-works/pi-env`）**只使用 `env` 岛**。它的 `src/` 里 6 个 import 点全部指向 `./env` 或 `./env/node`。
3. `coding-agent` 对 durable **0 引用**（`grep -rn durable packages/coding-agent/src/ | wc -l` = 0）。

**含义**：镜像出来的 `pi-durable` 会是**孤岛包**——这一条必须在批次建议里体现，且**「先做 env 岛」有直接的产品价值**（解锁 pi-env），而「先做 L0 内核」没有。

> 补充：`packages/env` 本体 2,346 行，另有一个 **4,998 行的 Rust daemon**（`packages/env/daemon/`，17 个 `.rs`，含 `pty.rs`/`watch.rs`/`sys/windows.rs`）。daemon 不在 durable 内，但它是 `env` 岛的下游需求方，见 §7。

### 2.5 pi-ai 依赖（模型层）

`pi-ai` 在根入口是**运行时**依赖，共 6 个子路径 / 11 个值符号：

| pi-ai 子路径 | 值符号 | 使用点 |
|---|---|---|
| `utils/estimate` | `calculateContextTokens` `estimateMessageTokens` | `harness/compaction.ts:272,321,325-327` |
| `utils/retry` | `isRetryableAssistantError` `retryDelayMs` | `harness/compaction.ts:188,191`；`harness/generation.ts:511,514` |
| `utils/transcript` | `declarationsEqual` `getCurrentTools` `toToolDeclaration` | `harness/prompt.ts:79,85,104,112,117`；`harness/generation.ts:584` |
| `utils/validation` | `validateToolArguments` | `harness/tool.ts:250` |
| `utils/uuid` | `uuidv7` | `harness/provider.ts:18` |
| `utils/overflow` | `isContextOverflow` | `harness/generation.ts:489` |

另有 **`Models` 结构性运行时依赖**——不是类型，是要真调方法：

```
harness/generation.ts:134,205,238,268  models.getModel(provider, modelId)
harness/generation.ts:241              models.fetchDeferred(model, handle, …)
harness/generation.ts:271              models.cancelDeferred(model, handle, …)
harness/generation.ts:431              models.streamSimple(model, {messages}, options)
harness/compaction.ts:181              models.completeSimple(model, {messages}, options)
codemode/models.ts:104,106,116,218     models.getModelsOfType/getAvailableOfType/getModelOfType
```

**规模对照**：`packages/ai` = **27,070 行 / 199 个文件**（比 durable 还大）；`packages/chord` = 13,553 行 / 31 个文件；`packages/codemode` = 3,037 行 / 17 个文件。

**含义**：批次 D（harness）**无法在 pi-ai 未移植的情况下开始**——但它只需要 pi-ai 的 6 个 `utils/*` 小模块 + `Models` 接口的 7 个方法，不需要整个 pi-ai。建议在批次 D 之前确认 pi-ai 的移植进度（见 §7 开放问题）。

### 2.6 pi-codemode 依赖（标注，不重复 task-7）

仅 `src/codemode/` 4 个文件依赖，5 个 import 点：

| 子路径 | 值符号 | 使用点 |
|---|---|---|
| `pi-codemode/source` | **`CODEMODE_SOURCE_GRAMMAR`、`parseCodemodeSource`** | `codemode/codemode.ts:47` |
| `pi-codemode/declarations` | **`renderToolSample`、`toCodemodeIdentifier`** | `codemode/describe.ts:9` |
| `pi-codemode/portable` | **`CodemodeHostCall`、`CodemodeOutcome`、`CodemodeResult`、`CodemodeRun`、`CodemodeSandboxBase`、`toCodemodeIdentifier`** | `codemode/codemode.ts:46`、`codemode/output.ts:9` |
| `pi-codemode`（根） | 类型 `CodemodeJsonSchema` | `codemode/describe.ts:8` |

**依赖 durable 侧的桥接点**（chord→`codemode` 用到的 4 个 chord 子路径全用上了）：
`codemode/codemode.ts` → `../documents.ts` `../env/index.ts` `../errors.ts` `../harness/define.ts` `../harness/tool.ts` `../harness/types.ts` `../search.ts` `../types.ts` `./describe.ts` `./models.ts` `./output.ts`。

**含义**：codemode 岛运行时闭包 **31 文件 / 9,761 行**——它是**唯一一个会把整个 harness 拖进来的可选子路径**。`CODEMODE_SOURCE_GRAMMAR` 是一个 Lark 语法 + 解析器，属 task-7 范围。

---

## 3. 公开 API 面

### 3.1 `package.json` 的 `exports`（16 个子路径 + package.json）

```json
".":                          → src/index.ts                  (203 个导出)
"./env":                      → src/env/index.ts              (18 个导出)
"./env/node":                 → src/env/node.ts               (NodeExecutionEnv + NodeWatchOptions)
"./codemode":                 → src/codemode/index.ts         (12 个导出)
"./tool-search":              → src/tool-search/index.ts      (5 个导出)
"./tools":                    → src/tools/index.ts            (19 个导出)
"./images":                   → src/images/index.ts           (createPhotonImages)
"./images/node":              → src/images/node.ts
"./images/cloudflare":        → src/images/cloudflare.ts
"./storage/memory":           → src/storage/memory.ts
"./storage/jsonl":            → src/storage/jsonl/index.ts
"./storage/jsonl/node":       → src/storage/jsonl/node.ts
"./storage/sqlite":           → src/storage/sqlite/index.ts   (7 个导出)
"./storage/sqlite/cloudflare":→ src/storage/sqlite/cloudflare.ts
"./storage/sqlite/node":      → src/storage/sqlite/node.ts
"./testing":                  → src/testing/index.ts          (26 个导出)
```

> 每个子路径有 4 个条件键（`source`/`types`/`import`/`default`），Python 侧对应 `pyproject.toml` 的显式包或一个顶层包 + 子模块。

### 3.2 根入口 `src/index.ts` 的 203 个导出（按任务要求分组）

23 条 `export` 语句，**203 个导出名**（**值 39 / 类型 164**，脚本核验见附录 A 命令 9）：

| 分组 | 值导出 | 类型导出 |
|---|---|---|
| **Session / Conversation** | `createSession`（`session/session.ts:120`） | `Session` `ConversationId` `ConversationRecord` `ConversationOwnership` `ConversationQuery` `ConversationDocToken` `ConversationDocFamilyToken` `RewindableConversationDocToken` `RewindableConversationDocFamilyToken` `ConversationView` `SessionDocToken` `SessionDocFamilyToken` `SessionEnd` `ROOT_CONVERSATION_ID`(值) |
| **Storage** | `MemoryStorage`（`storage/memory.ts:121`） | `Storage` `StorageWrite` `Cursor` `Page` `ScanOrder` `Seq` `StoredDocument` `DocumentContent` `DocumentRecord` `DocumentCreate` `DocumentAddress` `DocumentPoint` `DocumentQuery` `DocumentCopySource` `CommitChange` `CommitPublication` `TableCommitChange` `DocumentCommitChange` `CheckpointInfo` `ContextEdit` `Tx` |
| **Task** | `defineTask`（`tasks.ts:122`）；`CompactionTask` `GenerationTask` `ToolTask` `defineTool` | `Task` `TaskId` `TaskState` `TaskRecord` `TaskDefinition` `TaskRuntime` `TaskOptions` `TaskOwnership` `TaskOutcome` `TaskOutcomeError` `TaskQuery` `TaskDocToken` `TaskDocFamilyToken` `TaskInspection` `RunningTask` `NextTaskState` `SettledTask` `JoinPolicy` `PhaseHandler` `ToolTaskInput` `ToolTaskResult` `ToolTaskCheckpoint` `GenerationInput` `GenerationResult` `GenerationCheckpoint` `CompactionInput` `SummaryRequest` `CompactionCheckpoint` |
| **Document** | `defineDoc` `defineDocFamily` | `DocDefinition` `DocFamilyDefinition` `CommonDocDefinition` `DocToken` `DocFamilyToken` `DocumentSemantics` `LatestConversationSemantics` `RewindableConversationSemantics` `DocumentId` `DocumentReader` `DocumentObserver` `DocumentWatch` `DocumentState` `StoredDocument` |
| **Harness / Scheduler** | `Harness` `createRegistry` `configure` `AgentDoc` `DEFAULT_COMPACTION_POLICY` `DEFAULT_PROGRESS_POLICY` `DEFAULT_RETRY_POLICY` `LiveDoc` `InboxDoc` `ProviderDoc` `UsageDoc` `watchEvents` `defineExtension` `hook` `section` `wrapSection` `wrapTool` | `HarnessOptions` `HarnessSettings` `HarnessInspection` `Agent` `AgentState` `AgentChange` `AgentEvent` `AgentEventStream` `MessageChange` `SnapshotEvent` `ToolEventCall` `Registry` `RegistryReader` `RegistrySnapshot` `Settings` `Extension` `HookApi` `HookRegistration` `HookResult` `HooksOf` `HookRunner` `ContextView` `CompactionPolicy` `CompactionHooks` `CompactionResult` `CompactionReason` `CompactionFailure` `CompactionStatus` `LiveState` `InboxItem` `InboxState` `ProviderState` `UsageState` `TaskGraph` `TaskGraphNode` `TaskGraphState` `TaskGraphWatch` `TaskInspection` `PromptInput` `PromptSection` `UserInput` `InputEntry` `SettledSubmissionRecord` `Wrap` |
| **Tools** | `NestedCallDoc` | `ToolRegistration` `ToolExecutionApi` `ToolExecutionResult` `ToolExecutionMode` `ToolControl` `ToolDiagnostic` `ToolHookCall` `ToolHooks` `ToolSlot` `NestedToolSlot` `NestedToolSummary` `NestedCallState` `NestedToolExecutionResult` `ToolTaskInput` `ToolTaskResult` `ToolTaskCheckpoint` |
| **Submission / Inbox** | `InboxDoc`① | `Submission` `SubmissionDraft` `InputSubmissionDraft` `SubmissionCreate` `SubmissionId` `SubmissionQuery` `SubmissionRecord` `SubmissionSettlement` `QueueMode` |
| **Entry** | `defineEntry` `UserEntry` `AssistantEntry` `SystemEntry` `ToolResultEntry` `ResetEntry` `CompactionEntry` | `Entry` `EntryId` `EntryDraft` `EntryQuery` `EntryRecord` `TypedEntry` `TypedEntryDraft` |
| **其他** | `ConversationBusy` `ReadAfterWrite` `SessionFailed` `StorageRequestError`（`errors.ts:11`） | `Id` `JsonObject` `ModelRef` `EnvTarget` `WatchEnd` `WatchHandle` `Cursor` |
| **Codemode** | —（**不在根入口**，只走 `./codemode` 子路径） | — |

> ① `InboxDoc` 同时属 Submission/Inbox 与 Harness 分组，只计一次；上表为便于阅读在两组都列出。

**注意**：`docs/spec.md:308` 有独立的「**2.2 Public Harness surface**」小节（约 675 行），是公开面的规范定义。移植时应以它为准，而不是以 `index.ts` 为准。

### 3.3 子路径入口的导出

| 入口 | 导出 |
|---|---|
| `./env`（`env/index.ts:3-376`） | 值：`ok` `err` `getOrThrow` `getOrUndefined` `toError` `FileError` `ExecutionError` `rangeDecoder` `StreamDecoder` `startsWithBom` `LineScanner`；类型：`Result` `FileKind` `FileErrorCode` `ExecutionErrorCode` `FileInfo` `TextLine` `TextLineReader` `BinaryReader` `WatchTarget` `WatchChange` `FileWatcher` `LineScan` `DirReader` `ListFilesOptions` `FileListEntry` `FileListing` `FileSystem` `ShellSpillOptions` `ShellExecResult` `ShellExecOptions` `ShellOutputWindow` `ShellOutputSkip` `ShellOutputInfo` `Shell` `ExecutionEnv` |
| `./storage/sqlite`（`storage/sqlite/index.ts:1-8`） | 值：`applySqliteMigrations` `CURRENT_SQLITE_SCHEMA_VERSION` `SQLITE_MIGRATIONS` `SqliteStorage`；类型：`SqliteDatabase` `SqliteExecutor` `SqliteValue` `SqliteMigration` `SqliteStorageOptions` |
| `./tools`（`tools/index.ts:7-39`） | 值：`createBashTool` `createPowerShellTool` `createEditTool` `createReadTool` `createWriteTool` `createCodingTools` `CodingTools` `DEFAULT_IMAGE_LIMITS`；类型：`BashExecution` `BashPrepare` `BashToolInput` `BashToolOptions` `PowerShellToolInput` `PowerShellToolOptions` `EditToolDetails` `EditToolInput` `ImageLimits` `ImageProcessor` `PreparedImage` `ReadToolDetails` `ReadToolInput` `ReadToolOptions` `WriteToolInput` |
| `./testing`（`testing/index.ts:1-27`） | 值：`createExpectAssertions` `createEnvConformance` `registerEnvConformance` `registerStorageConformance` `createStorageConformance` `seedStorageBenchmark` `seedStorageWriteBenchmark` `storageBenchmarkPrimaryRecordCount` `STORAGE_MEMORY_SCALES` `STORAGE_READ_BENCHMARKS` `STORAGE_WRITE_BENCHMARKS` `TIMING_SCALE`；类型：`ExpectLike` `StorageConformanceRunner` `EnvConformanceAssertions` `EnvConformanceCase` `EnvConformanceOptions` `EnvConformanceProvider` `StorageConformanceAssertions` `StorageConformanceCase` `StorageConformanceOptions` `StorageConformanceProvider` `StorageBenchmarkDataset` `StorageBenchmarkScale` `StorageReadBenchmark` `StorageWriteBenchmark` |
| `./codemode`（`codemode/index.ts:1-13`） | 值：`CODEMODE_MODULE_VERSION` `CODEMODE_TOOL_NAME` `CodemodeStoreDoc` `createCodemodeExtension` `createCodemodeTool` `DEFAULT_CODEMODE_INLINE_BUDGET`；类型：`CodemodeGlobal` `CodemodeNestedCall` `CodemodeToolDetails` `CodemodeToolInput` `CodemodeToolOptions` `CodemodeNamespace` |
| `./tool-search`（`tool-search/index.ts:1-7`） | 值：`createToolSearchExtension` `createToolSearchTool` `DEFAULT_TOOL_SEARCH_LIMIT` `TOOL_SEARCH_TOOL_NAME`；类型：`ToolSearchToolDetails` |

---

## 4. 分层与最小可行集

### 4.1 分层（任务定义的四层，对应到实际文件）

| 层 | 文件 | 行数 | 运行时闭包 |
|---|---|---:|---|
| **L0 内核** | `types.ts`(1154) `session/*`(2513) `documents.ts`(208) `entries.ts`(34) `errors.ts`(57) `ids.ts`(11) `storage/{memory,scan}.ts`(904) `storage/sqlite/*` 除 cloudflare(1428) | **6,309** | **K2b = 15 文件 / 5,407 行** |
| **L1 任务与文档** | `tasks.ts`(8) + `types.ts` 中的 Task/Submission 记录类型（3 个实现文件 `harness/{task-graph,submissions,inbox}.ts` 物理上在 `harness/`，计入 L2，不重复计） | **8** | 含于 L2 闭包 |
| **L2 harness/scheduler** | `harness/*`（21 个文件，8,177 行）+ `truncate.ts`(174) + 根 `index.ts`(205) | **8,556** | **H0 = 29 文件 / 10,974 行** |
| **L3 集成层** | `env/`(2300) `testing/`(2935) `codemode/`(1698) `tools/`(1551) `storage/jsonl/`(866) `images/`(390) `tool-search/`(99) `storage/sqlite/cloudflare.ts`(192) | **10,031** | 见 §4.2 |
| 顶层共享 | `search.ts`(85)——被 `codemode/` 与 `tool-search/` 值引用 | **85** | — |
| **合计** | | **24,989** ✅ 与 §1.1 吻合 | |

> 说明：L1 在**语义**上是一层（Task/Submission 记录与文档），但在**文件布局**上它的实现散落在 `harness/` 里，无法按文件切分。**这正是「L0 无法独立成形」的结构性原因之一**——任务记录类型住在 `types.ts`（L0），任务的运行时住在 `harness/`（L2），而 `Storage`/`Tx`/sqlite schema 同时引用两者。

### 4.2 各可选子路径的运行时闭包（**这是排期的真正依据**）

用脚本按 `import type` 与值导入分离后计算的**传递闭包**：

| 切片（入口） | 运行时文件/行 | 类型闭包 | 并集 | 外部运行时依赖 |
|---|---|---|---|---|
| **K0 内核核心**（types+session+documents+entries） | **9 / 3,977** | 10 / 4,298 | 14 / 5,741 | chord ×3 子路径（6 值符号） |
| **K2 = K0 + sqlite** | 14 / 5,191 | 11 / 4,287 | 20 / 6,993 | +chord/delta `apply` |
| **K2b = K0 + sqlite + node 适配** | **15 / 5,407** | 12 / 4,503 | 21 / 7,209 | 同上（+`node:sqlite`） |
| **K3 = K0 + 三个后端** | 17 / 6,907 | 13 / 5,157 | 23 / 8,709 | 同上 |
| **E0 env 岛**（`env/index`+`env/node`） | **5 / 2,300** | 3 / 2,125 | 5 / 2,300 | **无（零运行时外部依赖）** |
| **E0b 仅 `env/index`** | **3 / 552** | 1 / 377 | 3 / 552 | **无** |
| **T0 tools 岛** | **16 / 2,321** | 1 / 39 | 16 / 2,321 | `chord/output`(2) + `diff` + `typebox` |
| **T1 tools+images** | 19 / 2,661 | 5 / 402 | 20 / 2,696 | +photon(WASM) |
| **TS tool-search 岛** | **4 / 228** | 1 / 7 | 4 / 228 | 无 |
| **C0 codemode 岛** | **31 / 9,761** | 1 / 13 | 31 / 9,761 | +pi-codemode(4 子路径) + typebox/compile |
| **`./testing` 岛** | 11 / 4,606 | 8 / 2,991 | 16 / 6,039 | chord/context(1) |
| **`./storage/jsonl`** | 6 / 1,824 | 1 / 6 | 6 / 1,824 | 无 |
| **H0 `harness/harness.ts`** | 29 / 10,974 | 8 / 4,431 | 31 / 12,163 | chord ×4 + pi-ai ×6 |
| **PUB 根 `index.ts`** | **34 / 12,633** | 7 / 3,123 | 36 / 13,822 | chord ×4 + pi-ai ×6 + typebox/compile |

**读法**：`PUB 根入口` 12,633 行运行时闭包**不到** 24,989 行总量的一半——**根入口不包含 sqlite/jsonl/env 后端**（`index.ts:121` 只导出 `MemoryStorage`）。要持久化必须走 `./storage/sqlite` 子路径。

### 4.3 ★ 只做 L0 能否构成「可发布、自洽的 pi-durable」？

**结论：不能。** 分三层论证。

#### (a) 技术自洽性：**能编译、能跑**

K0 的 9 个运行时文件是：

```
types.ts  documents.ts  entries.ts  errors.ts  ids.ts
session/{session,transaction,observation,forks}.ts
```

其中**没有任何** `harness/` 或 `env/` 的**运行时**导入。唯一联系是：
- `types.ts:4` → `env/index.ts`（**仅类型** `ExecutionEnv`）
- `types.ts:12` → `harness/types.ts`（**仅类型**，6 个符号，全在 `TaskRuntime`）

**证据（命令）**：把 `import type` 与值导入分开算闭包后，K0 运行时闭包 = 9 文件 / 3,977 行，不含 `harness/`、`env/`、`storage/`（见 §4.2 脚本）。
**交叉验证**：上游自带 `test/storage-runtime-boundary.test.ts:28-32` 断言根入口闭包**不含** `env/`、`storage/jsonl/`、`storage/sqlite/`，且断言每个文件不含 `node:` 导入。我用独立脚本重算，得到**同样的结论**（`index.ts` 闭包中 `env/` 出现 0 次，`storage/sqlite/index.ts` 闭包中 `sqlite/node.ts` 出现 0 次）。**两套方法互相印证。**

#### (b) 但「可发布的 pi-durable」需要三处偏离上游

| # | 问题 | 证据 | 最小改动 |
|---|---|---|---|
| 1 | `TaskRuntime` 悬空 | `types.ts:170-243` 是 6 个 harness 类型的**唯一**使用点 | 要么删除 `TaskRuntime`/`TaskDefinition`/`Task`/`TaskOptions`，要么前置声明这 6 个类型。**但 `TaskRuntime` 是 `ToolTask`/`GenerationTask`/`CompactionTask` 的宿主**，删了 L1/L2 就没了 |
| 2 | `Tx` 与 `Storage` 含任务/提交 | `types.ts:790-888` 的 `Tx` 有 `createTask` `settleSubmission` `placeSubmission`；`Storage`（`types.ts:1059-1139`）有 `task` `scanTasks` `submission` `scanSubmissions` `submissionByRequest` **5 个方法**；sqlite schema 9 张表里 `tasks`/`submissions` 占 2 张（`migrations.ts:41-60`） | 要删就得**同时**改 `Tx` + `Storage` + schema + migrations——**这不是「只做 L0」，这是改上游契约** |
| 3 | 公开面严重缩水 | `index.ts` 共 **203 个导出（值 39 / 类型 164）**；L0 只能提供 **97 个（48%）**，其中**值导出 17 个**：`defineDoc` `defineDocFamily` `defineEntry` + 6 个 entry 常量（`UserEntry` `AssistantEntry` `SystemEntry` `ToolResultEntry` `ResetEntry` `CompactionEntry`）+ 4 个错误（`ConversationBusy` `ReadAfterWrite` `SessionFailed` `StorageRequestError`）+ `createSession` `MemoryStorage` `defineTask` `ROOT_CONVERSATION_ID`。**丢掉的 22 个值导出全是运行时核心**：`Harness` `ToolTask` `GenerationTask` `CompactionTask` `createRegistry` `watchEvents` `defineExtension` `hook` `section` `wrapSection` `wrapTool` `configure` `AgentDoc` `LiveDoc` `InboxDoc` `ProviderDoc` `UsageDoc` `NestedCallDoc` `DEFAULT_{COMPACTION,PROGRESS,RETRY}_POLICY`。**即保留了一半的名字、丢掉了几乎全部可运行能力** |

#### (c) 而且**没有任何消费者**——这是决定性的一条

§2.4 实测：全仓 14 个 import 点**全部在 `packages/env`**，且**全部指向 `./env`、`./env/node`（生产）与 `./testing`、`./tools`（测试）**。
`packages/env/src/` 里**从不 import 根入口**——唯一一处根入口引用是 `env/test/differential.test.ts:7` 的 `import type { ToolExecutionApi }`（**仅类型，且只在测试里**）。

**所以「L0 内核」交付的是一个零消费者、且公开面只有上游 15% 的包。**

#### (d) L0 还**无法独立验收**

L0 的语义口径是 `testing/storage-conformance.ts`（1,663 行）——按任务定义属于 **L3**。它的运行时闭包只有 3 个文件（`ids.ts` `types.ts` 自己），但它承载的是 memory/sqlite/jsonl **三个后端共用的唯一语义口径**（`types.ts:1059` 注释「The semantic conformance suite covers memory, SQLite, and JSONL」见 `docs/spec.md` §10 末）。

**没有它，L0 的正确性只能靠手写测试——而那正是上游刻意避免的。**

### 4.4 ★ 最小可行集的正确切法：**env 岛**

**证据链：**

1. **唯一消费者只需要它**：`packages/env/src/{errors,watch,remote-env,list-files}.ts` 的 6 个 import 点全部是 `./env` / `./env/node`（§2.4）。
2. **零运行时外部依赖**：`env` 岛运行时闭包 5 文件 / 2,300 行，chord **值导入 0 个**（只有 `env/index.ts:1` 一个 `import type { Context }`），**不依赖 chord、不依赖 pi-ai、不依赖 typebox**（§2.3）。
3. **更小的可用子集**：只做 `env/index.ts` + `decode.ts` + `line-scan.ts` = **3 文件 / 552 行**，就是 pi-env 的 client 侧需要的全部接口（`ExecutionEnv`/`FileError`/`Result`/`FileWatcher` 等）。
4. **自带验收 oracle**：`testing/env-conformance.ts`（633 行）的运行时闭包只有 4 文件 / 1,185 行，且**只依赖 `env/index.ts`**——不需要 L0、不需要 harness。
5. **上游自己就把它当作独立可发布单元**：`.github/workflows/env.yml` 是**独立 workflow**，路径触发为 `packages/durable/src/env/**`、`packages/durable/src/tools/**`、`packages/env/**`；并有 `env-daemons.yml` 独立矩阵构建 daemon。

**最小可行集定义（建议）：**

> **MVP = `pi_durable.env` + `pi_durable.env.node` + `pi_durable.testing` 的 env 部分**
> 范围：`src/env/{index,decode,line-scan}.ts`(552) + `src/env/{node,node-watch}.ts`(1,748) + `src/testing/{env-conformance,types,assertions,runner}.ts`(756) = **3,056 行**
> 交付标准：pi-env 的 10 个生产 import 点全部可解析；`createEnvConformance` 的全部 case 在 `NodeExecutionEnv` 等价物上通过；`env/index` 的闭包不含 Node-only 模块（对应 `test/storage-runtime-boundary.test.ts:38-41`）。

**这个 MVP 的产品价值是明确的**：解锁 `pi-env`（含 SSH 远程执行），而 `pi-env` 是上游唯一真的在用 durable 的东西。

### 4.5 一致性套件是「可移植性红利」

`testing/assertions.ts:17-38` 把测试框架抽象成**6 个断言原语**：

```ts
ok / strictEqual / deepEqual / partialDeepEqual / greaterThan / rejects
```

`testing/runner.ts:13-25` 的 `registerStorageConformance` 只要一个 `{ describe, expect, it }` 形状的 runner。
**Python 侧写一个 pytest 适配器约 40 行**，就能直接复用上游 1,663 + 633 行的一致性用例（作为移植的验收口径）。**这是整个移植里性价比最高的一件事。**

---

## 5. 可移植性风险清单（逐项到文件）

风险等级：**H** = 需要设计决策/语义重建；**M** = 有明确 Python 对等物但要写不少代码；**L** = 机械替换。

### 5.1 Node 内置模块（5 个文件）

| # | 文件:行 | Node 机制 | Python 对等物 | 风险 |
|---|---|---|---|---|
| R1 | `storage/sqlite/node.ts:3-4` | `import { DatabaseSync, type StatementSync } from "node:sqlite"` | `sqlite3` 标准库（同步 API 也现成） | **L** |
| R2 | `storage/sqlite/node.ts:31-120` | `SerialOperationQueue`（132 行）+ `:121-135` `NodeSqliteTransaction` | `threading.Lock` / 单连接串行化 | **M** |
| R3 | `storage/sqlite/node.ts:158` | `AggregateError([error, rollbackError], …)` | Python 3.11+ `ExceptionGroup` | **L** |
| R4 | `storage/sqlite/migrations.ts:12` | 注释：「`next_id` is TEXT because **node:sqlite rejects INTEGER results outside JavaScript's safe integer range**」 | Python 整数无上限，**但 schema 必须保持 TEXT** 才能与上游库文件互通 | **M** |
| R5 | `env/node.ts:1` | `spawn` from `node:child_process` | `asyncio.create_subprocess_exec` / `subprocess.Popen` | **M** |
| R6 | `env/node.ts:2,1212` | `randomUUID` from `node:crypto` | `uuid.uuid4()` | **L** |
| R7 | `env/node.ts:3` + 全文件 | `node:fs` 的 `open/readdir/opendir/lstat/realpath/mkdtemp/access/rename/truncate/createWriteStream` | `os` + `pathlib` + `shutil` + `tempfile` | **M** |
| R8 | `env/node.ts:274-304` | `killProcessTree`：Windows 用 Job Object，POSIX 用进程组 | `os.killpg` + Windows `taskkill /T`；或 `psutil` | **M** |
| R9 | `env/node.ts:305-386` | `waitForChildProcess`：流式 stdout/stderr、背压、spill 到临时文件、`AbortSignal` | `asyncio` 子进程 + 流式写 | **M** |
| R10 | `env/node.ts:76,194,224` | `process.platform === "win32"` 分支（`~\` 展开、Git Bash 查找 `:224-250`、cmd 转义） | `sys.platform` / `os.name` | **M** |
| R11 | `env/node.ts:213-212` | `getBashShellConfig`：WSL 遗留 `bash.exe` 走 stdin（`-s`）而非 `-c`（`env/node.ts:208-215`） | 需保留，否则 Windows 上行为不同 | **M** |
| R12 | `env/node.ts:261-273` | `getShellEnv`：`process.env` 继承 | `os.environ` | **L** |
| R13 | `env/node.ts:1200` | `mkdtemp(join(tmpdir(), prefix))` | `tempfile.mkdtemp` | **L** |

### 5.2 文件监听（最高风险的单点）

| # | 文件:行 | 机制 | 问题 | 风险 |
|---|---|---|---|---|
| R14 | `env/node-watch.ts`（522 行）整体 | `fs.watch` 原生递归监听 | **Python 标准库没有对等物**（`watchdog` 是第三方，且语义不同） | **H** |
| R15 | `env/node-watch.ts:46-59` | 5 个调优常量：`DEBOUNCE_MS=50`、`FSEVENTS_SETTLE_MS=500`、`DEFAULT_POLL_MS=2000`、`DEFAULT_MAX_DIRECTORIES=10_000`、`HASH_MAX_BYTES=256KB`、`HASH_RECENT_MS=5000` | 语义参数必须复刻 | **M** |
| R16 | `env/node-watch.ts:184,294,415,512` | **平台矩阵**：`win32` 强制 polling（原生 watcher 会占住目录阻止重命名父目录）；`darwin` 在 `fs.watch` 返回后立即重扫（FSEvents 会漏）；`linux/android` 每目录监听 | 平台行为的**原因**必须保留，否则一致性用例会漏 | **H** |
| R17 | `env/node-watch.ts:61-75` | `UNRELIABLE_FILE_SYSTEMS` 集合（网络/FUSE 文件系统 → polling） | 需重建该名单 | **M** |
| R18 | `env/node-watch.ts:76` `BudgetExceeded`、`:139` `NodeFileWatcher` | 目录数预算 + 溢出（`{overflow:true}`）语义 | 契约见 `env/index.ts:119-127` | **M** |

> 上游 CI 专门为这类语义开了独立 workflow（`.github/workflows/env.yml` 路径触发 `src/env/**`），且 `CHANGELOG.md:88-89` 记录「env 一致性套件新增 3 项 `watch()` 行为检查」——**说明这块的语义还在演进，移植时要锁版本**。

### 5.3 并发/事件循环语义

| # | 文件:行 | 机制 | 问题 | 风险 |
|---|---|---|---|---|
| R19 | 29 处 | `AbortSignal` | 贯穿所有公开 API。Python：`asyncio.CancelledError` 或自造 token | **H** |
| R20 | `harness/scheduler.ts:570,1099`；`session/session.ts:643`；`session/observation.ts:108,242` | `queueMicrotask`（5 处） | 语义是「在当前同步块之后、在当前 turn 内」。Python 对等物是 `loop.call_soon`，但**与 asyncio 任务调度的交错顺序不同** | **H** |
| R21 | `harness/scheduler.ts:556-563`；`session/session.ts:649-658` | `setTimeout(...).unref()`（5 处） | **Node 专属**：定时器不阻止进程退出。**好消息**：代码**已经内建降级路径**——探测不到 `unref` 就放弃定时器（`#documentTimersUnavailable` / 直接 `return`），改为每次 commit 后清扫。**Python 侧应直接走这条降级路径** | **M** |
| R22 | 7 处：`images/node.ts:103`、`storage/sqlite/node.ts:48`、`harness/scheduler.ts:1285`、`harness/util.ts:12`、`session/session.ts:135-136`、`session/observation.ts:161` | `Promise.withResolvers` | Node 22+ / 现代浏览器。Python：`asyncio.Future` 或 `(Event, 容器)` | **L** |
| R23 | `harness/scheduler.ts:1953` 行 / 约 90 个成员 | 撤销级联、所有权索引、预留、`failFast`、`completing` 终局化——**一个大的并发状态机** | `.github` 的 `CHANGELOG.md:54-55` 记录了两次大重写（扇出 12s→50ms、内存索引有界化）。**移植是把这些优化重做一遍** | **H** |
| R24 | `session/session.ts:112-200`；`session/transaction.ts`（1,043 行） | Session 单写线 + 事务 + 文档缓存逐出 + 失败即终局（`SessionFailed`） | 契约见 `docs/spec.md` §10.1。**语义密度极高** | **H** |

### 5.4 类型/校验/数据

| # | 文件:行 | 机制 | 问题 | 风险 |
|---|---|---|---|---|
| R25 | `harness/tool.ts:13,557-580` | `Compile` from `typebox/compile` 做**运行时** JSON Schema 校验（`structuredOutputSchema`） | Python：`jsonschema` 或 `pydantic`。**注意错误消息格式**：`:578` 把 `instancePath` 转成点号路径并拼成 `"… does not match its schema: path: message"` | **M** |
| R26 | `tools/{read,edit,bash,write}.ts` 各 1 处；`codemode/{models,codemode}.ts` | `Type` from `typebox`（7 个 import 点） | 工具参数 schema 声明 → pydantic 模型 | **M** |
| R27 | `tools/edit-diff.ts`（500 行，**与 v0.85.1 逐字节相同**） | `diff` npm 包（`diff@8.0.4`） | Python `difflib` 的 hunk 边界与上下文行数**与 npm `diff` 不同** → `edit` 工具的 patch 渲染与错误消息会不一致 | **M** |
| R28 | `harness/tool.ts:557`、`harness/view.ts:65`、`storage/sqlite/cloudflare.ts:57` | `WeakMap`（3 处，身份键缓存） | `weakref.WeakKeyDictionary`（仅对可弱引用对象）或 `id()` 字典 | **M** |
| R29 | `harness/usage.ts:66`；`testing/storage-conformance.ts:935,964,977` | `structuredClone`（4 处） | `copy.deepcopy` | **L** |
| R30 | 全仓 1,433 处 `#private` 字段 + 74 处 `get` 访问器 | `#x` 真私有字段、getter | Python 命名约定 / `@property`。机械 | **L** |
| R31 | `types.ts:20-36` | `Id<Kind>` 品牌类型（`number & {…}`）、`Seq` | Python `NewType`/`TypeAlias`。**注意 `ids.ts:4-11` 是「擦除品牌」的信任边界** | **L** |
| R32 | 全仓 | JSON 序列化顺序、`Number.isSafeInteger`（17 处） | Python dict 保序、int 无上限。**但 sqlite 里 `next_id` 是 TEXT**（R4） | **L** |
| R33 | 全仓 57 处 `Uint8Array`、12 处 `TextDecoder`、3 处 `TextEncoder` | 字节/文本边界 | `bytes` / `str.encode`。**注意 BOM 处理**：`env/index.ts:376` 导出 `startsWithBom`，`CHANGELOG.md:99,130` 有两处 BOM 相关的修复 | **M** |

### 5.5 平台专属适配（Python 侧可能没有对应物）

| # | 文件 | 机制 | 结论 | 风险 |
|---|---|---|---|---|
| R34 | `storage/sqlite/cloudflare.ts`（192 行） | Cloudflare Durable Object SQLite（`ctx.storage`）；`:57` `WeakMap` 队列、`:72` `BigInt(Number.MIN_SAFE_INTEGER)` 钳制 | **Python 无对等物**。建议**明确排除**并在 PORTING.md 记录（这是平台适配器，不是通用后端） | 排除 |
| R35 | `images/cloudflare.ts`（10 行） | Wrangler 的 `CompiledWasm` import 规则（Workers 无法运行时编译 WASM） | **Python 无对等物**，排除 | 排除 |
| R36 | `images/{index,node,node-worker,exif}.ts`（390 行） | **Photon（Rust→WASM）** + `worker_threads` + `import.meta.url` + `createRequire`（`images/node.ts:51,73-75`；`images/node-worker.ts:4,24`） | Python：`Pillow` 做等价处理。**但要复刻的是行为**：EXIF 方向矫正、JPEG 质量阶梯 `[80,70,55,40]`、0.75 递减缩放、base64 预算、`images/index.ts:119-139` 的手写转置（因为 Photon 的 `rotate` 会丢像素）。**建议用 Pillow 重写而不是模拟 WASM** | **H** |
| R37 | `images/node.ts:15,79,117-122` | worker 空闲 30s 退出以释放 WASM 内存 | Python：线程/进程池 + TTL。可选 | **M** |
| R38 | `harness/generation.ts:431` | `for await (const event of events)` 异步迭代模型流 | Python async generator | **M** |

### 5.6 上游自身的变动风险（自标 experimental）

| 事实 | 证据 |
|---|---|
| 包自标 **"Experimental. The API changes without notice between releases."** | `README.md` |
| **v1.2.0 一个 minor 就有 6 条 Breaking** | `CHANGELOG.md:20-27` |
| 4 个版本节含 Breaking（1.0.3 / 1.0.4 / 1.1.0 / 1.2.0） | `CHANGELOG.md:20,60,86,104` |
| 1.0.0 于 **2026-10-01**，1.3.0 于 **2026-10-15**——**包龄仅 2.5 周，已发 5 个版本** | `CHANGELOG.md:3,140` |
| `Storage` 契约在 1.1.0 变更（新增 `order` 字段） | `CHANGELOG.md:62` |
| SQLite schema **只有 1 个 migration**（`CURRENT_SQLITE_SCHEMA_VERSION = 1`） | `storage/sqlite/migrations.ts:90,95` |

**含义**：**必须锁定 v1.3.0 作为移植目标，并在 `PORTING.md` 写明「不对齐后续版本」**。按这个变动速度，跟随上游会让排期永远收敛不了。

### 5.7 测试资产的可移植性

| 事实 | 证据 | 影响 |
|---|---|---|
| 测试 **38,793 行 / 112 文件**，比 `src/` 大 **55%** | `find test -name '*.ts' \| xargs cat \| wc -l` | 若验收含「上游测试全过」，工作量翻倍 |
| 一致性套件框架无关（6 个断言原语） | `testing/assertions.ts:17-38` | **利好**：pytest 适配约 40 行 |
| 有 **spec 用法编译检查** | `test/spec-usage.test.ts`（380 行）：把 `docs/spec.md` 的用法示例逐字复制并**编译检查** | **利好**：Python 侧可镜像为 `mypy`/`pyright` 检查 |
| 有 **可执行的架构边界测试** | `test/storage-runtime-boundary.test.ts`（48 行）：断言闭包内无 `node:` 导入、根入口不含 sqlite/jsonl/env | **利好**：直接镜像为 Python 的 import 闭包检查 |
| 基准测试 7 个 | `package.json:22-32`（`bench:*` 脚本） | `scheduler-fanout`(20,000 任务 1.5s)、`scheduler-memory`、`document-cache` 等**性能验收线**要一并移植才能证明没退化 |

---

## 6. 建议批次

> **⚠️ 2026-10-18 修正（批次 C 施工后回填）**
>
> 本节初版把**批次 C 整体排在批次 D 之前**，这是**错的**。施工批次 C 时发现：
> 批次 C 的 2,204 行里有 **1,325 行**（`tools/{bash,read,write,edit,edit-diff}.ts`、
> `tool-search/`、`tools/{env,index}.ts`）依赖 `harness/define.ts` 的 `defineTool` /
> `defineExtension` —— 而且是**值依赖**，不是类型依赖（工具对象由这两个函数构造），
> 因此**无法在 harness 到位前落地**。
>
> 初版为什么会错：§4.2 的闭包算法算的是**运行时 import 闭包**，它确实把
> `harness/define.ts` 圈进了工具岛（见 §4.2 的 `T0 tools island` 运行时文件清单）；
> 但 §6 排期时我按「目录」而不是按「依赖」排序，把 C 放在了 D 前面，漏掉了这个交叉。
>
> **修正**：把 C 拆成 **C₁（不依赖 harness，可先做）** 与 **C₂（依赖 D，必须在 D 之后）**；
> 并新增 **D-1** 作为 C₂ 的最小前置（只做 `define.ts` 的两个函数 + 工具执行的窄类型面，
> 而不是整个 D）。依赖图因此变成：
>
> ```
> A ──┬─→ C₁（709 行，无 harness）        ✅ 已交付
>     └─→ D-1（define 层 + 窄类型面）     ✅ 已交付
>              └─→ C₂（1,325 行）          ⛔ 待做
>                       └─→ D 主体（scheduler 等 ~8,100 行）
> ```

排序原则：**先做有消费者、无重依赖、有 oracle 的**；把 chord 依赖最重的放中间；把需要 pi-ai / pi-codemode 的放最后。

### 批次 A：env 岛（**建议第一个做**）

| 项 | 内容 |
|---|---|
| **范围** | `src/env/{decode,index,line-scan}.ts`(552) + `src/env/{node,node-watch}.ts`(1,748) + `src/testing/{env-conformance,types,assertions,runner}.ts`(756) = **3,056 行** |
| **对外依赖** | **chord 值导入 0**（仅 `import type { Context }`）、pi-ai 0、typebox 0。仅 Python 标准库 |
| **产出的公开面** | `pi_durable.env`（`ExecutionEnv`/`FileSystem`/`Shell`/`FileError`/`Result` 等 24 个）+ `pi_durable.env.node`（`NodeExecutionEnv`）+ `pi_durable.testing` 的 env 部分 |
| **验收标准** | ① `createEnvConformance` 全部 case 在 `NodeExecutionEnv` 等价物上通过（含 `watch()` 的 native/polling 两模式、`listFiles` 可选、`openBinaryReader`/`openDirReader`/`scanLines`）；② 镜像 `test/storage-runtime-boundary.test.ts:38-41`：`pi_durable.env` 的运行时 import 闭包含 0 个 Node-only 模块；③ `pi-env` 的 6 个 `src/` import 点 + 2 个 `./env/node` 全部可解析并可运行 |
| **风险** | R5–R18（尤其 R14–R18 文件监听）。**建议把 `watch()` 拆成独立子任务** |
| **工作量** | **6–9 人周**（其中 `env/node-watch.ts` 单独 2–3 周） |
| **依赖** | **无前置**。可与 chord 移植并行 |
| **状态** | ✅ **已交付**（81 条测试） |

### 批次 B：内核 + 一致性套件（L0）

| 项 | 内容 |
|---|---|
| **范围** | `types.ts`(1,154) + `session/*`(2,513) + `documents.ts`(208) + `entries.ts`(34) + `errors.ts`(57) + `ids.ts`(11) + `storage/{memory,scan}.ts`(904) + `storage/sqlite/*` 除 cloudflare(1,428) + `testing/storage-conformance.ts`(1,663) = **约 7,972 行** |
| **对外依赖** | **chord 值导入 7 个**：`replicatedState` `copyJson` `track` `apply` `awaitWithContext` `withoutAbortSignal` `BACKGROUND_CONTEXT`（3 个子路径）；pi-ai **仅类型**（`Message`/`Models`） |
| **产出的公开面** | `createSession` `MemoryStorage` `defineDoc` `defineDocFamily` `defineEntry` + 7 个 entry 常量 + 4 个错误 + `Storage`/`Session`/`Tx`/`Document*`/`Entry*`/`Task*`/`Submission*` 类型（约 130 个） |
| **验收标准** | ① `createStorageConformance` 全绿 × **memory + sqlite** 两后端；② sqlite schema 与上游逐表逐索引一致（**9 张表 / 16 个索引**，`storage/sqlite/schema.ts:5-34`）、migration 版本从 1 起连续（`migrations.ts:113-117` 的连续性校验）；③ **与上游 v1.3.0 生成的库文件互通**（同 schema 读写）；④ `Promise.withResolvers`/`queueMicrotask` 的替代方案不改变 Session 的「只有已提交状态可观察」不变式 |
| **风险** | R19–R24（Session 单写线、失败即终局、文档缓存逐出）；**R4（`next_id` 必须保持 TEXT）** |
| **工作量** | **10–14 人周** |
| **依赖** | chord 的 `delta` + `context` + 根（**task-5**）。**这是关键路径**——chord 不到位的部分需要先在 Python 侧做接口桩 |
| **状态** | ⛔ 未做 |

### 批次 C₁：工具与镜像岛的**无 harness 部分**（原「批次 C」的前一半）

| 项 | 内容 |
|---|---|
| **范围** | `truncate.ts`(174) + `tools/{image,image-processor,path-utils,file-mutation-queue}.ts`(325) + `images/{exif,index}.ts`(210) = **709 行**（+ 依赖项 `search.ts` 85 行） |
| **对外依赖** | 批次 A 的 env 抽象；`chord/output` 的 `characterEnd` / `utf8ByteLength`；`photon`→Pillow（可选 extra） |
| **产出的公开面** | `pi_durable.tools`、`pi_durable.images`、`pi_durable.truncate`、`pi_durable.search` |
| **验收标准** | ① 上游 `test/env-truncate.test.ts` 的用例逐条通过；② 图像：EXIF 方向、JPEG 质量阶梯、0.75 缩放、base64 预算与上游一致（**行为等价，非字节等价**——编码器不同）；③ `ruff` / `mypy` / 全仓测试不回归 |
| **风险** | R27（difflib ≠ npm diff，属 C₂）、R36/R37（Photon→Pillow 行为等价） |
| **工作量** | **3–4 人周** |
| **依赖** | 批次 A |
| **状态** | ✅ **已交付**（184 条测试；Pillow 做成可选 extra） |

### 批次 D-1：harness 的 **define 层 + 工具执行窄类型面**（新增，为解锁 C₂）

| 项 | 内容 |
|---|---|
| **范围** | `harness/define.ts:6-17`（2 个函数）+ `harness/types.ts` 的窄提取（`ToolRegistration`/`ToolExecutionApi`/`ToolExecutionResult`/`ToolDiagnostic`/`ToolControl`/`ToolOutputLimits`/`RetainedOutput`/`Agent`/`Extension`/`ModelRef`/`Models`） |
| **对外依赖** | **新增 `pi-py-ai`**（上游 `durable/package.json:60-67` 本就把它列为运行时依赖）；`chord/context` 的 `Context` |
| **产出的公开面** | `pi_durable.harness`（14 个符号） |
| **验收标准** | ① C₂ 的 9 个文件所需符号 100% 覆盖（含 `api` 的 7 个实测成员）；② `define_tool` 的泛型推断经**正反两个类型探针**验证（正向 mypy 通过；把字段用错时报 `attr-defined`）；③ 按 `tools/write.ts` / `tool-search.ts` / `tools/bash.ts` / `tools/read.ts` 的真实形状各写一个最小工具并跑通 |
| **风险** | **本端 `ToolExecutionApi` 是窄接口（7 成员），不是上游那个 20+ 成员的类型**——必须写进 PORTING.md 偏离条目，否则后续会误以为已完整 |
| **工作量** | **1–2 人周**（实际远低于此） |
| **依赖** | 无（可与 C₁ 并行） |
| **状态** | ✅ **已交付**（19 条测试） |

### 批次 C₂：工具与镜像岛的**依赖 harness 部分**（原「批次 C」的后一半）

| 项 | 内容 |
|---|---|
| **范围** | `tools/{edit-diff,read,bash,edit,write,index,env}.ts`(1,226) + `tool-search/*`(99) = **1,325 行** |
| **对外依赖** | 批次 D-1 的 `define_tool`/`define_extension` + 窄类型面；`diff`(npm)→`difflib`；C₁ 的 `truncate` / `path_utils` / `file_mutation_queue` / `images` |
| **产出的公开面** | `pi_durable.tools`（补上 `create_coding_tools` / `CodingTools` 与四个 `create*Tool`）+ `pi_durable.tool_search` |
| **验收标准** | ① `createCodingTools()` 的 4 个工具在真实文件系统上可跑；② 镜像 `test/tools.test.ts`、`test/tools-read-differential.test.ts`（**read 与 Node 实现同结果**——最硬的验收）；③ `edit` 的 patch 渲染与 npm `diff` 的差异有专门对拍（R27） |
| **风险** | R27（difflib 的 hunk 合并策略与上下文行数与 npm `diff` 不同 → 用户可见的 patch 文本不一致） |
| **工作量** | **3–4 人周** |
| **依赖** | **D-1（已就绪）** + C₁（已就绪） |
| **状态** | ⛔ 未做（前置已全部就绪） |

### 批次 D：harness / scheduler 运行时主体（L1+L2，最大的一块）

| 项 | 内容 |
|---|---|
| **范围** | `harness/*`(8,177) 减 D-1 已做的部分 + `tasks.ts`(8) + 重组根 `index.ts`(205) ≈ **8,100 行** |
| **对外依赖** | chord ×4 子路径（**15 个值符号**）；**pi-ai 6 个 `utils/*` 子路径（11 个值符号）+ `Models` 接口的其余 7 个方法**；`typebox/compile` |
| **产出的公开面** | 根入口的 **203 个导出**（`Harness` `ToolTask` `GenerationTask` `CompactionTask` `createRegistry` `watchEvents` `hook` `section` `wrapTool` `LiveDoc` `InboxDoc` `AgentDoc` `ProviderDoc` `UsageDoc` + 全部类型） |
| **验收标准** | ① 镜像 `test/harness-*` 系列（约 24 个文件 / **20,000+ 行**）：`tasks` `tasks-recovery` `ownership` `scheduler-indexes` `scheduler-retention` `cancellation-barrier` `compaction` `nested-tools` `nested-tools-restart` `structured` `structured-output` `generation` `generation-recovery` `context` `inbox` `submissions` `events` `view` `task-graph` `registry` `callers` `lifecycle` `storage-failure` `inspect`；② **`test/spec-usage.test.ts` 的等价物**：`docs/spec.md` §2.2 的全部用法示例可通过类型检查；③ 性能线：`bench:scheduler-fanout`（20,000 owned tasks < 1.5s）、`bench:scheduler-memory`、`bench:document-cache`、`bench:document-append` 不得显著退化 |
| **风险** | **R19–R24**（调度器状态机 + 事件循环语义），R25/R26（typebox） |
| **工作量** | **16–22 人周**（若含全部 harness 测试的移植） |
| **依赖** | 批次 A + B + C₁；**pi-ai 的 6 个 `utils/*` 模块 + `Models` 的其余方法**（跨包，见 §7） |
| **状态** | ⛔ 未做（D-1 之外**一行未写**：`scheduler.ts` 1,953 行、`tool.ts` 911、`types.ts` 的其余部分 812、`generation.ts` 715、`events.ts` 506、`harness.ts` 470、`compaction.ts` 464、`context.ts` 333、`agent.ts` 314、`live.ts` 268、`view.ts` 267、`submissions.ts` 241、`task-graph.ts` 236、`inbox.ts` 168、`prompt.ts` 158、`registry.ts` 114、`usage.ts` 72、`util.ts` 61、`json.ts` 31、`provider.ts` 39，以及 `define.ts` 的另外 4 个函数） |

### 批次 E：codemode + 平台适配收尾

| 项 | 内容 |
|---|---|
| **范围** | `codemode/*`(1,698) + `storage/jsonl/*`(866) + `storage/sqlite/cloudflare.ts`(192) + `images/cloudflare.ts`(10) = **约 2,766 行**（另需 pi-codemode 3,037 行） |
| **对外依赖** | **pi-codemode 4 个子路径**（task-7）；`typebox/compile`；chord ×4 |
| **产出的公开面** | `pi_durable.codemode`(12) + `pi_durable.storage.jsonl` |
| **验收标准** | ① 镜像 `test/codemode.test.ts`(1,073)、`codemode-describe`、`codemode-models`；② `codemode` 的**确定性重放**（脚本日志先写后交付）在重启后可续跑——这是 codemode 的核心不变式；③ JSONL 后端过 `createStorageConformance`（与 memory/sqlite 同口径） |
| **建议** | **`storage/sqlite/cloudflare.ts` 与 `images/cloudflare.ts` 建议明确排除**（R34/R35，Python 无对等物），在 `PORTING.md` 记录为「平台专属，不移植」 |
| **工作量** | **6–9 人周**（不含 pi-codemode 本身） |
| **依赖** | 批次 D（codemode 运行时闭包 9,761 行覆盖整个 harness）+ task-7 |
| **状态** | ⛔ 未做 |

### 6.6 工作量汇总与校准

| 批次 | 范围行数 | 人周 | 前置 | 状态 |
|---|---:|---:|---|---|
| A env 岛 | 3,056 | 6–9 | 无 | ✅ 已交付 |
| B 内核 + 一致性套件 | 7,972 | 10–14 | chord（task-5） | ⛔ 未做 |
| **C₁** 工具岛（无 harness） | 709 | 3–4 | A | ✅ 已交付 |
| **D-1** define 层 + 窄类型面 | ~120 | 1–2 | 无 | ✅ 已交付 |
| **C₂** 工具岛（依赖 harness） | 1,325 | 3–4 | **C₁ + D-1** | ⛔ 未做（前置已就绪） |
| D harness/scheduler 主体 | ~8,100 | 16–22 | A + B + C₁ + pi-ai | ⛔ 未做 |
| E codemode + 收尾 | 2,766 | 6–9 | D + pi-codemode（task-7） | ⛔ 未做 |
| **合计** | **24,048** | **40–56 人周 ≈ 10–14 人月** | | |
| 未纳入（可后置） | 941：`search.ts`(85) + `testing/{index,storage-benchmark}.ts`(516) + `images/node{,-worker}.ts`(165) + `photon-wasm.d.ts`(5) + `images/cloudflare.ts`(10) + 账目尾差(160) | — | 被动依赖或平台专属 | |

**四点必须说清楚的校准：**

1. **「~25k 行 / 数人月」偏乐观。** 24,989 行 `src` 是**高密度并发代码**（平均每文件 305 行，但 `scheduler.ts` 1,953 行 / 约 90 个成员），不是 CRUD。按 40–56 人周算更贴近「数人月」的上沿到 10+ 人月。
2. **若验收含「上游测试全过」，再加 38,793 行测试的移植**——但测试移植的性价比高于源码移植（很多是逐字翻译），且一致性套件（§4.5）可**直接复用**而非重写。**建议验收口径定为「一致性套件 + spec-usage + 边界测试」三件套，而不是全部 112 个测试文件。**
3. **关键路径是 chord 和 pi-ai，不是 durable 自己。** B 卡 chord；D 卡 pi-ai。**建议 Lead 先确认这两条的进度再排 B/D。**
4. **C 的拆分修正了原先的错误排期**（见本节开头的修正说明）：C₂ 必须在 D-1 之后，而 D-1 只是 D 的 1.4%。**这说明「按目录排批次」会系统性低估批次间的交叉依赖**——本报告 §4.2 的闭包数据里其实已有线索（工具岛运行时闭包含 `harness/define.ts`），排期时漏读了。

### 6.7 建议的施工顺序（甘特式，已按 C₁/C₂ 修正）

```
第 1 波（立即可开工，互不阻塞）
  ├─ A    env 岛（含 env-conformance 的 pytest 适配器）        ✅ 已完成
  └─ C₁   工具岛无 harness 部分                                ✅ 已完成

第 1.5 波（为解锁 C₂ 而切出的最小前置）
  └─ D-1  harness define 层 + 工具执行窄类型面                 ✅ 已完成

第 2 波（前置已全部就绪，可立刻开工）
  └─ C₂   tools/{bash,read,write,edit,edit-diff} + tool-search（1,325 行）

第 3 波（chord 就绪后）
  └─ B    内核 L0 + storage-conformance（chord delta/context 是硬前置）

第 4 波（pi-ai 就绪后）
  └─ D    harness/scheduler 主体（8,100 行，最大、最贵、风险最高）

第 5 波
  └─ E    codemode（依赖 task-7）+ JSONL 后端
```

---

## 7. 无法确定项（明确列出）

| # | 项 | 为什么不确定 | 建议 Lead 动作 |
|---|---|---|---|
| U1 | **`agent/src/pico/**` 谱系** | `/tmp/pi-up/v0.85.1/packages/agent/src/pico/` **不存在**——`find /tmp/pi-up/v0.85.1 -type d -name 'pico*'` 返回空。这与 `recon/00` 的时间线**一致**（pico 于 2026-09-18 的 `08016016` 改名，晚于 v0.85.1 的 2026-09-05），但 v1.3.0 的 `.git` 是 **shallow clone（depth 1，只有 `71023ae Release v1.3.0`）**，`git show 08016016` 报 `unknown revision`。**我无法独立验证 7 个 rename**，只能采信 `recon/00` | 以 `recon/00:1.2` 为准；若能拿到完整 clone，建议补 commit 证据 |
| U2 | **v0.85.1 的真实前身对照** | 磁盘上 v0.85.1 没有 pico，我的对照对象实际是 **`v0.85.1/packages/agent/src/harness/**`（21,851 行）**。**独立佐证**：`tools/edit-diff.ts` 在 v0.85.1 与 durable 中都是 **500 行且 `diff` 输出 0 行差异（逐字节相同）**——与 `recon/00` 的「blob SHA 相同」结论**互相印证**。其余配对的改动量都很大（`session/types.ts` 601→1,154 改动 1,559 行；`runtime/lane.ts` 2,012→`scheduler.ts` 1,953 改动 3,601 行） | 建议把 §1「旧版对照」的路径订正为 `agent/src/harness/**` + `session-backends/sqlite-node/`，避免队友找错目录 |
| U3 | **`pi-ai` 的移植进度** | 批次 D 硬依赖 pi-ai 的 6 个 `utils/*` 模块（11 个值符号）+ `Models` 接口（7 个方法）。`packages/ai` 是 **27,070 行 / 199 文件**。我未调研其状态（不属本任务） | **这是批次 D 的唯一硬前置**，请 Lead 向其他侦察线确认 |
| U4 | **`pi-codemode` 的移植进度** | 批次 E 硬依赖 4 个子路径（含 `CODEMODE_SOURCE_GRAMMAR` + `parseCodemodeSource` 的 Lark 语法解析器）。属 task-7 | 由 task-7 输出 |
| U5 | **`photon`（Photon WASM）能否找到等价物** | `images/index.ts` 依赖 Rust→WASM 的 Photon 做 EXIF 矫正/缩放/JPEG 编码。Python 侧 `Pillow` 能覆盖功能，但**像素级输出不会完全相同**（不同重采样实现、JPEG 编码器），而 `test/tools-images.test.ts`（565 行）可能断言具体字节 | 需评估「行为等价」vs「字节等价」，建议验收定为**行为等价 + 上限/下限约束** |
| U6 | **`diff`(npm) 与 `difflib` 的输出差异影响面** | `tools/edit-diff.ts` 500 行与上游逐字节相同，但它调用 `diff` 包的 `structuredPatch`/`applyPatch`。difflib 的 hunk 合并策略、上下文行数、空白处理都不同。**影响用户可见的 patch 文本** | 建议在批次 C 单列一个「diff 差分测试」子任务，以 `test/tools-read-differential.test.ts` 的方法论对拍 |
| U7 | **`node:sqlite` 与 Python `sqlite3` 的库文件互通** | `next_id` 是 TEXT（`migrations.ts:12`），schema 用 `STRICT` 表 + `json_valid()` CHECK + 部分索引（`entry_heads_by_conversation ... WHERE head IS NOT NULL`）。这些 SQLite 特性需要 **SQLite ≥ 3.37（STRICT）/ ≥ 3.9（部分索引）**。Python 标准库的 `sqlite3` 链接的系统 SQLite 版本**因平台而异**（macOS 系统 Python 可能偏旧） | **批次 B 开工前必须先验证**目标环境的 `sqlite3.sqlite_version`；若过旧需绑定 `pysqlite3-binary`。这是我建议的**第一批次 B 冒烟测试** |
| U8 | **`docs/spec.md` 是否与 v1.3.0 代码完全同步** | 规范 5,352 行、normative，但 `CHANGELOG.md:144` 说 spec 在 GitHub 主分支。本地 `docs/spec.md` 与代码同批下载，但未验证一致性。`spec.md:38` 还提到依赖「pi-ai PR #9548」（`SystemMessage` 契约），状态未知 | 建议用 `test/spec-usage.test.ts`（380 行）作为「spec 与代码同步」的机器校验——它已经在做这件事 |
| U9 | **测试验收口径** | 我建议「一致性套件 + spec-usage + 边界测试」三件套，但这是**我的判断**，不是用户决定。若要求「112 个测试文件全过」，批次 D 的工作量会显著上浮 | 请 Lead 与用户确认 |
| U10 | **`pi-storage-sqlite` 的复用结论复核** | 简报说 `database.py` 可复用、`storage.py`+`migrations.sql` 作废。我**独立复核属实且有更强结论**：`storage.py`(316 行) 是 v0.85.1 `session-backends` 的 **session/entry/label 模型**（`create_entry_id` `get_leaf_id` `set_leaf_id` `get_label` `set_label` …），与上游新 `Storage`（`types.ts:1059`，15 个方法名 / 16 个签名，覆盖 conversations/entries/tasks/submissions/documents 五类记录）**唯一重合的方法名是 `close`**。`migrations.sql`(69 行) 同理 | 建议在排期文档中明确：`pi-storage-sqlite` 的复用仅限 `database.py`(131 行) 的连接/事务封装，其余**全部重写** |

---

## 附录 A：复现命令

```bash
D=/tmp/pi-up/v1.3.0/packages/durable

# 1. 总量核对
find $D/src -name '*.ts' | wc -l                    # 82
find $D/src -name '*.ts' | xargs cat | wc -l        # 24989

# 2. 逐目录汇总
for d in $(find $D/src -mindepth 1 -type d); do \
  echo "$(find $d -maxdepth 1 -name '*.ts' | xargs cat | wc -l)  $d"; done | sort -rn

# 3. 外部依赖频率
grep -rhoE 'from "[^".][^"]*"' --include='*.ts' $D/src \
  | sed 's/from "//;s/"$//' | sed 's|^\(@[^/]*/[^/]*\).*|\1|' | sort | uniq -c | sort -rn

# 4. chord 子路径用量
grep -rhoE 'from "@earendil-works/chord[^"]*"' --include='*.ts' $D/src | sort | uniq -c | sort -rn

# 5. 消费者（排除 durable 自身）
cd /tmp/pi-up/v1.3.0 && grep -rhoE 'from "@earendil-works/pi-durable[^"]*"' \
  --include='*.ts' packages/ --exclude-dir=durable | sort | uniq -c

# 6. 运行时/类型闭包与 chord 值符号：见本报告 §4.2 / §2.3 的脚本（closure3.py / chord2.py 思路）

# 7. 上游自带的移植边界断言（本报告 §4.3 的交叉验证来源）
cat $D/test/storage-runtime-boundary.test.ts

# 8. 前身逐字节相同证据
diff /tmp/pi-up/v0.85.1/packages/agent/src/harness/tools/edit-diff.ts $D/src/tools/edit-diff.ts
# → 无输出（0 行差异）

# 9. 公开导出的「值 / 类型」拆分（本报告 §3.2 的 39 / 164 来源）
python3 - <<'PY'
import re
src = open("/tmp/pi-up/v1.3.0/packages/durable/src/index.ts").read()
stmts = re.findall(r'^export .*?;$', src, re.S | re.M)
v = t = 0
for s in stmts:
    if s.strip().startswith("export type"):
        t += len([n for n in re.search(r'\{([^}]*)\}', s, re.S).group(1).split(',') if n.strip()])
    else:
        for n in re.search(r'\{([^}]*)\}', s, re.S).group(1).split(','):
            if n.strip():
                t += n.strip().startswith("type ")
                v += not n.strip().startswith("type ")
print("value:", v, " type:", t, " total:", v + t)   # → value: 39  type: 164  total: 203
PY

# 10. sqlite schema 对象数（本报告 §6 批次 B 验收标准引用：9 表 / 16 索引）
grep -c '": "' $D/src/storage/sqlite/schema.ts   # 表名 9 + 索引名 16 + 其他
sed -n '5,34p' $D/src/storage/sqlite/schema.ts
```

## 附录 B：一句话给排期

> **durable 是 25k 行源码 + 39k 行测试 + 5.4k 行规范的三件套；它的 82 个文件里有 44 个直接依赖 chord，唯一的外部消费者只用得到其中一个 2,300 行的子岛。先做那个岛（6–9 人周，解锁 pi-env，零 chord 依赖），再做内核（10–14 人周，卡 chord），工具岛并行（3–4 人周），harness 最后（16–22 人周，卡 pi-ai），codemode 收尾（6–9 人周，卡 task-7）。总计 41–58 人周。**

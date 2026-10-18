# 侦察报告 02：上游运行时结构重组（v0.85.1 → v1.3.0）

> 范围：`@earendil-works/pi-agent-core` 瘦身、`harness` 去向、`durable` 拆分、新包地图、以及这对 pi-py 包边界的含义。
> 输入（只读）：`/tmp/pi-up/v0.85.1/packages/`、`/tmp/pi-up/v1.3.0/packages/`、pi-py `packages/*`。
> 本报告只写 `recon/02-upstream-runtime-restructure.md`，未改动仓库其它文件。

---

## 0. 结论摘要（TL;DR）

1. **不是「搬家」，是「删除 + 另起一个包重写」。** 上游 v1.0.0 的 commit `7fd478a2`（2026-10-01，标题 `feat(agent): remove the experimental harness from pi-agent-core`）改动统计是 **removed 290 / added 0 / renamed 0**。`packages/agent/src/harness/**` 的 82 个 `.ts` 是**被删掉的**，不是被移动到 `durable`。

2. **`pi-durable` 不是 `session-backends` 的后继，而是 `pico` 的后继。** `packages/durable` 诞生于 commit `08016016`（2026-09-18，`feat(durable): move Pico into dedicated package`），该 commit 的 7 个 rename 全部是 `packages/agent/src/pico/**` → `packages/durable/src/**`。v0.85.1 时 `durable` 还不存在（`packages/durable/` blob 数 = 0）。

3. **`durable/src/harness/` 与旧 `agent/src/harness/` 几乎没有文本延续。** 逐文件行集合 Jaccard 相似度全在 **0.003–0.05** 区间（旧 `runtime/lane.ts` `wc -l`=2012 行 vs 新 `harness/scheduler.ts` `wc -l`=1953 行 = **0.013**）。旧 harness 的 durable-drive 状态机在 1.3.0 里**没有同构实现**，是被「任务 + 提交 + 文档」模型整体重写。

4. **整条迁移链上，真正「搬迁」的文件只有一个**：`packages/agent/src/harness/tools/edit-diff.ts` → `packages/durable/src/tools/edit-diff.ts`（blob SHA **完全相同**）。其余 81 个 harness `.ts` 要么被删除、要么被 coding-agent / durable **重写**。

5. **`session-backends` 也是被整体删除的**（同一个 commit `7fd478a2`），而 `durable/src/storage/sqlite` 的 schema 与它**完全不同**（旧 `sessions/entries/scalar_values/list_values/usage_ledger` vs 新 `durable_schema/…/conversations/entries/tasks/submissions/documents/document_revisions`）。所以 pi-py 的 `pi-storage-sqlite` 不是「换个位置」，是「换一套数据模型」。

6. **Lead 给的一条前提需要更正**：`packages/protocol` 与 `packages/client` **是有 `package.json` 的正式包**（`@earendil-works/pi-protocol` / `@earendil-works/pi-client`，v0.85.1），并参与根 `build` 脚本。它们连同 `packages/server` 一起在 commit `7472cda0`（2026-10-12，`feat(coding-agent): remove the experimental server stack and the client, protocol, and server packages`）被删除，**落在 v1.2.0**（v1.1.0 时 `packages/server/` 还有 35 个 blob，v1.2.0 为 0）。详见 §3.4。

7. **对 pi-py 最要紧的两件事**：
   - pi-py 的 `pi-server`（对齐上游 `@earendil-works/pi-server`）**上游已删除**，追赶计划必须处置它；
   - pi-py 的 `pi-agent-core` 里的 `harness/`（session/skills/compaction）**在上游已不存在同名物**，但它的功能在 1.3.0 里被**拆到两个包**：会话/持久化/工具执行 → `pi-durable`，技能/压缩/消息/工具实现 → `pi-coding-agent/src/core/`。

8. **一个反直觉但很重要的事实**：上游 `pi-coding-agent` v1.3.0 **完全不依赖 `pi-durable`**（`grep -rn "pi-durable" coding-agent/src | wc -l` = 0；其 `package.json` 依赖只有 agent-core / ai / codemode / mcp / tui）。全仓库只有 `pi-env` 依赖 `pi-durable`。也就是说，**照抄上游包边界，会得到一个「几乎没人用」的 pi-durable**。

---

## 1. 方法与证据基线

### 1.1 本地源码树与 tag 一致性校验

本地树就是 tag 内容，可以放心用（这是下面所有 `find`/`diff` 结论的前提）：

```bash
find /tmp/pi-up/v0.85.1/packages -type f -not -path '*/node_modules/*' | wc -l   # 1580
gh api "repos/earendil-works/pi/git/trees/346294a615d2d0ad4f6e5fbccb4cee4ccd7b2d6c?recursive=1" \
  --jq '[.tree[]|select(.type=="blob")]|length'                                  # 1669（含 packages/ 以外）
awk -F'\t' '$1 ~ /^packages\//' /tmp/tree_0851.tsv | wc -l                        # 1580  ✅ 一致
awk -F'\t' '$1 ~ /^packages\//' /tmp/tree_130.tsv  | wc -l                        # 1783
```

- v0.85.1 tree SHA：`346294a615d2d0ad4f6e5fbccb4cee4ccd7b2d6c`（tag → commit `d981de12…`）
- v1.3.0 tree SHA：`25e0557c8b13f98848bebd48fe7922908f66fe4a`（tag → commit `71023aec…`）

### 1.2 使用的权威命令（可复现）

```bash
# A) 完整文件级 added/removed（不受 compare API 的 300 文件上限影响）
gh api "repos/earendil-works/pi/git/trees/<tree-sha>?recursive=1" \
  --jq '.tree[]|select(.type=="blob")|[.path,.sha]|@tsv'

# B) 精确重命名（内容完全相同的移动）＝ blob SHA 交集
#    见 /tmp/exact_renames.tsv

# C) git 自带 rename 检测（跨 16 个中间 tag 分段，避开 300 文件上限）
for t in v0.85.1 v0.86.0 v0.86.1 v0.87.0 v0.87.1 v0.99.0 v0.99.1 v0.99.2 \
         v1.0.0 v1.0.1 v1.0.2 v1.0.3 v1.0.4 v1.1.0 v1.2.0 v1.3.0; do
  gh api "repos/earendil-works/pi/compare/$prev...$t" \
    --jq '.files[]|select(.status=="renamed")|[.previous_filename,.filename]|@tsv'
done

# D) 关键 commit 的改动统计
gh api repos/earendil-works/pi/commits/7fd478a2 --jq '[.files[].status]|group_by(.)|map({(.[0]):length})|add'
gh api repos/earendil-works/pi/commits/08016016 --jq '.files[]|select(.status=="renamed")|[.previous_filename,.filename]|@tsv'

# E) 文件级去向推断：行集合 Jaccard + 同 basename 加权
#    （脚本 /tmp/map_agent.py → /tmp/agent_map.json）
```

> 注意：`compare` 端点硬上限 300 文件（`--paginate` 不生效，第二页 `.files` 为 null）。跨大版本必须走 tree API 或分段 compare。

### 1.3 「重命名」的完整结果（只有 6 条）

| 区间 | 旧路径 | 新路径 |
|---|---|---|
| v0.87.0→v0.87.1 | `packages/durable/src/memory-storage.ts` | `packages/durable/src/storage/memory.ts` |
| v0.99.1→v0.99.2 | `packages/durable/src/env/utils/truncate.ts` | `packages/durable/src/truncate.ts` |
| v1.0.0→v1.0.1 | `packages/coding-agent/src/modes/interactive/components/pi-logo-animation.ts` | `…/easter-egg-3d.ts` |
| v1.2.0→v1.3.0 | `packages/durable/src/harness/output.ts` | `packages/chord/src/output.ts` |
| v1.2.0→v1.3.0 | `packages/durable/test/harness-output-skip.test.ts` | `packages/chord/test/output-skip.test.ts` |
| v1.2.0→v1.3.0 | `packages/durable/test/harness-output.test.ts` | `packages/chord/test/output.test.ts` |

**跨 0.85.1→1.3.0 整段，git 的 rename 检测一条都没给出**（因为分段后各区间内都没有跨包移动）。blob SHA 精确匹配也只找到 4 条，其中 3 条是通用配置文件（`protocol/tsconfig.build.json`→`telemetry/`、`protocol/vitest.config.ts`→`mcp/`、`server/tsconfig.build.json`→`telemetry/`），属噪声；唯一有意义的代码搬迁是：

```
packages/agent/src/harness/tools/edit-diff.ts
  → packages/durable/src/tools/edit-diff.ts        # blob SHA 完全相同
```

---

## 2. 包级迁移地图

### 2.1 包清单对比

| v0.85.1（11 个） | v1.3.0（11 个） | 变化 |
|---|---|---|
| `agent` | `agent` | **大幅瘦身**（见 §3） |
| `ai` | `ai` | 增量（见报告 01） |
| `chord` | `chord` | 增量（v1.2.0 起吸收 `durable/src/harness/output.ts`） |
| `client` | — | ❌ v1.2.0 删除 |
| `coding-agent` | `coding-agent` | 增量 + 吸收旧 harness 的 skills/compaction/tools |
| — | `codemode` | ➕ v0.99.0 新增 |
| — | `durable` | ➕ v0.87.x 新增（源自 `pico`） |
| — | `env` | ➕ v1.1.0 新增 |
| `evals` | `evals` | 内部调整 |
| — | `mcp` | ➕ v0.99.0 新增 |
| `protocol` | — | ❌ v1.2.0 删除 |
| `server` | — | ❌ v1.2.0 删除 |
| `session-backends` | — | ❌ v1.0.0 删除 |
| `telemetry` | `telemetry` | — |
| `tui` | `tui` | 增量 |

### 2.2 时间线（按 tag 实测 `packages/<name>/` 下的 blob 数）

| tag | durable | codemode | mcp | env | agent/src/harness | server |
|---|---:|---:|---:|---:|---:|---:|
| v0.85.1 | 0 | 0 | 0 | 0 | **82** | 29 |
| v0.87.1 | 32 | 0 | 0 | 0 | 122 | — |
| v0.99.0 | 119 | 21 | 40 | 0 | 123 | — |
| v0.99.2 | 154 | 21 | 40 | 0 | 123 | — |
| v1.0.0 | 166 | 21 | 40 | 0 | **0** | — |
| v1.1.0 | 180 | 23 | 40 | 42 | 0 | 35 |
| v1.2.0 | 219 | 43 | 40 | — | 0 | **0** |
| v1.3.0 | 201¹ | 35¹ | 32¹ | 42 | 0 | 0 |

¹ 全部为 `gh api …/git/trees/<sha>?recursive=1` 的 blob 数（口径一致）。`agent/src/harness` 在 v0.85.1 是 82，到 v0.87.1/v0.99.x 涨到 122/123（因为期间新增了 `harness/pico3/**`），v1.0.0 归零。

**读法**：`durable` 在 harness 还存在的时候就独立生长了 3 个 minor；它不是「harness 的收容所」，而是**并行开发的新运行时**，等它成熟后旧 harness 才被一次性删除。

### 2.3 依赖方向（`@earendil-works/*` 内部依赖）

**v0.85.1**

```
telemetry ← ai ← agent-core ← session-backends/sqlite-node
                     ↑
chord ←──────────────┴── protocol ← client
                     └── server
coding-agent → { chord, agent-core, ai, tui }
```

**v1.3.0**

```
telemetry ← ai ← agent-core
                ↑
                └── coding-agent → { ai, agent-core, codemode, mcp, tui }
chord ← durable → { ai, codemode }
chord ← env → durable
```

关键变化：
- `agent-core` **不再依赖 `chord` 和 `pi-telemetry`**（`package.json` dependencies 从 8 项缩到 2 项：`pi-ai` + `typebox`）；
- `durable` 站在 `chord`（文档状态）+ `ai`（模型）+ `codemode`（沙箱）之上；
- `env` 在 `durable` 之上（远程执行环境），**不构成环**；
- `coding-agent` **不依赖 `durable`**（见 §0.8）。

---

## 3. 文件级迁移地图

### 3.0 `packages/agent` 概览

```bash
awk -F'\t' '$1 ~ /^packages\/agent\/src\/.*\.ts$/' /tmp/tree_0851.tsv | wc -l   # 90
awk -F'\t' '$1 ~ /^packages\/agent\/src\/.*\.ts$/' /tmp/tree_130.tsv  | wc -l   #  6
diff -rq /tmp/pi-up/v0.85.1/packages/agent/src /tmp/pi-up/v1.3.0/packages/agent/src
# → 5 个共有文件不同；3 个只在旧树（harness/、node.ts、search/）
```

v1.3.0 的 6 个 `src/*.ts`：

```
packages/agent/src/agent-loop.ts   packages/agent/src/agent.ts   packages/agent/src/index.ts
packages/agent/src/proxy.ts        packages/agent/src/stream-fn.ts   packages/agent/src/types.ts
```

v0.85.1→v1.3.0，`packages/agent/` 下被删除的文件按目录统计：

| 目录 | 删除文件数 |
|---|---:|
| `packages/agent/src` | 84（其中 `src/harness/**` **82**，全部为 `.ts`） |
| `packages/agent/docs` | 53 |
| `packages/agent/test` | 49 |
| `packages/agent/benchmark` | 11 |
| `packages/agent/vitest.harness.config.ts` / `vitest.benchmark.config.ts` / `scripts/` | 3 |

`package.json` `exports` 从 8 个条目缩到 2 个：

```
v0.85.1: "."  "./node"  "./harness/context"  "./harness/env/nodejs"
         "./harness/runtime/reducer"  "./harness/session"  "./harness/session/testing"  "./package.json"
v1.3.0 : "."  "./package.json"
```

### 3.1 `agent/src/harness/**` 逐个文件去向（82 个 `.ts`）

**判定口径**

- **P（搬迁）**：与 v1.3.0 某文件行集合 Jaccard ≥ 0.40，或有实质代码延续（含 1 例 blob 完全相同）；
- **R（重写）**：功能在 v1.3.0 有明确对应实现，但无文本延续（Jaccard < 0.12）；
- **D（删除）**：v1.3.0 树中无对应实现；
- 括号内 `old LOC`；`→` 后为 v1.3.0 路径与相似度（`j=`）。

> **LOC 口径**：下列各表的 LOC 列是**非空行数**（相似度脚本 `/tmp/map_agent.py` 的口径，去掉空行后计数）。§0/§2 中引用 `wc -l` 时另行标注；两者差值是空行与注释。例如 `runtime/lane.ts` 非空 1085 / `wc -l` 2012。

**分组与文件数校验**（合计 82 = `packages/agent/src/harness/**` 在 v0.85.1 的全部 `.ts`）：

```
3.1.1 顶层                  12
3.1.2 runtime/              21
3.1.3 session/ + session/testing/   17 + 10 = 27
3.1.4 compaction/            3
3.1.5 tools/                10
3.1.6 env/ + utils/ + execution/     9
                          -----
                            82  ✅
```

#### 3.1.1 顶层 harness 文件（12 个）

| 旧文件 | LOC | 去向 / 判定 |
|---|---:|---|
| `harness/agent-harness.ts` | 433 | **R** → `durable/src/harness/harness.ts`（`Harness.open()`/Conversation/Submission 模型；j=0.011）。旧 `AgentHarness` 类名在 1.3.0 不存在 |
| `harness/skills.ts` | 244 | **R/P** → `coding-agent/src/core/skills.ts`（j=0.123，cov=0.246） |
| `harness/prompt-templates.ts` | 181 | **R** → `coding-agent/src/core/prompt-templates.ts`（j=0.110） |
| `harness/system-prompt.ts` | 27 | **R** → `coding-agent/src/core/system-prompt.ts` + durable 的 `section()`（`durable/src/harness/define.ts`） |
| `harness/messages.ts` | 104 | **P** → `coding-agent/src/core/messages.ts`（**j=0.72，cov=0.92**，最高相似度之一） |
| `harness/types.ts` | 291 | **R** → 拆到 `durable/src/env/index.ts`（`Result`/`ok`/`err`/`getOrThrow`/`getOrUndefined`/`toError`；j=0.21）+ `durable/src/harness/types.ts`；`ExecutionEnv`/`FileSystem`/`Shell` 现由 `durable/src/env/index.ts` 定义 |
| `harness/telemetry.ts` | 304 | **R** → `@earendil-works/pi-telemetry`（1.3.0 独立包）+ `coding-agent/src/core/telemetry.ts`（j≈0.003，实为拆解重写） |
| `harness/hooks.ts` | 310 | **R** → `durable/src/harness/define.ts`（`hook()` / `defineExtension()`；j=0.003） |
| `harness/events.ts` | 195 | **R** → `durable/src/harness/events.ts`（`watchEvents`；j=0.014） |
| `harness/result.ts` | 74 | **D**（`Result` 工具函数族迁到 `durable/src/env/index.ts` 第 4–23 行；`TaggedError`/`LaneBusy`/`OperationMismatch`/`HarnessFault` 等错误类在 1.3.0 无对应，被 `durable/src/errors.ts` 的 `ConversationBusy`/`SessionFailed`/`StorageRequestError` 取代） |
| `harness/context.ts` | 24 | **D**（1.3.0 的 `Context` 来自 `@earendil-works/chord/context`，本包不再自行定义 `Context`） |
| `harness/config.ts` | 24 | **D** |

#### 3.1.2 `harness/runtime/**` — 旧 durable drive 状态机（21 个）→ **整体重写**

| 旧文件 | LOC | 去向 / 判定 |
|---|---:|---|
| `runtime/lane.ts` | 1085 | **R** → `durable/src/harness/scheduler.ts`（`wc -l` 1953 行；**j=0.013**）。「lane」概念在 1.3.0 不存在，换成 task/ownership/scheduler 模型 |
| `runtime/harness.ts` | 275 | **R** → `durable/src/harness/harness.ts` |
| `runtime/drive.ts` | 80 | **D**（`Drive` 抽象消失） |
| `runtime/drive/boundary.ts` | 202 | **D/R** → 边界概念并入 `durable/src/harness/generation.ts` |
| `runtime/drive/checkpoint.ts` | 129 | **R** → `durable/src/harness/tool.ts` 的 `ToolTaskCheckpoint` / `tasks.ts` 的 checkpoint |
| `runtime/drive/deferred.ts` | 194 | **D**（1.3.0 用 `ToolRegistration.deferred` + `modelTools.add`，见 `durable/src/harness/tool.ts`） |
| `runtime/drive/generation.ts` | 209 | **R** → `durable/src/harness/generation.ts`（`GenerationTask`） |
| `runtime/drive/reconcile.ts` | 113 | **D** |
| `runtime/drive/recovery.ts` | 121 | **R** → `harness.resume()` + scheduler 启动扫描（`durable/src/harness/scheduler.ts`） |
| `runtime/drive/response.ts` | 322 | **R** → `durable/src/harness/generation.ts` |
| `runtime/drive/retry.ts` | 27 | **D**（重试并入 `settings.retry`） |
| `runtime/drive/structural.ts` | 644 | **R** → `durable/src/session/transaction.ts`（1043 行；j=0.013） |
| `runtime/drive/terminal.ts` | 73 | **D** |
| `runtime/drive/tool-placement.ts` | 232 | **D**（1.3.0 由 `pi.live` 文档 + `ToolSlot` 呈现） |
| `runtime/drive/tools.ts` | 374 | **R** → `durable/src/harness/tool.ts`（`ToolTask`，911 行） |
| `runtime/index.ts` | 1 | **D** |
| `runtime/progress.ts` | 87 | **R** → `durable/src/harness/live.ts`（`LiveDoc`/`ToolSlot`/`CompactionStatus`） |
| `runtime/reducer.ts` | 135 | **R** → `durable/src/harness/view.ts`（`ConversationView`） |
| `runtime/restore.ts` | 127 | **R** → 恢复逻辑并入 `durable/src/session/*` + scheduler |
| `runtime/transcript.ts` | 96 | **R** → `durable/src/harness/context.ts`（`ContextView`，333 行） |
| `runtime/types.ts` | 111 | **R** → `durable/src/harness/types.ts`（812 行） |

> **证据强度**：这 21 个文件与 `durable` 对应物的 Jaccard 全部 ≤ 0.02（`lane.ts`→`scheduler.ts`=0.013，`structural.ts`→`transaction.ts`=0.013，`runtime/types.ts`→`harness/types.ts`=0.017，`harness.ts`→`harness.ts`=0.011）。这不是「重命名后小改」，是**换模型重写**。

#### 3.1.3 `harness/session/**` — 会话 + JSONL 存储（17 个）→ **整体重写**

| 旧文件 | LOC | 去向 / 判定 |
|---|---:|---|
| `session/session.ts` | 290 | **R** → `durable/src/session/session.ts`（1081 行；j=0.020） |
| `session/types.ts` | 363 | **R** → `durable/src/types.ts`（1154 行；j≈0）。`EntryType` 枚举从 4 类变成 `pi.user`/`pi.assistant`/`pi.tool-result`/`pi.system`/`pi.reset` + 自定义 `defineEntry` |
| `session/memory.ts` | 265 | **R** → `durable/src/storage/memory.ts`（864 行；j=0.020） |
| `session/in-memory-storage-state.ts` | 196 | **R** → 并入 `durable/src/storage/memory.ts` |
| `session/commit.ts` | 79 | **R** → `durable/src/session/transaction.ts`（`tx` 提交回调） |
| `session/fork.ts` | 117 | **R** → `durable/src/session/forks.ts` + `conversation.fork(entryId, …)` |
| `session/fork-policy.ts` | 29 | **R** → `defineDoc({ fork: "initial" \| "current" \| "asOf" })`（`durable/src/documents.ts`） |
| `session/values.ts` | 114 | **R** → `durable/src/documents.ts` 的 `defineDoc` / `defineDocFamily` |
| `session/context.ts` | 50 | **D** |
| `session/index.ts` | 34 | **D** |
| `session/mutation-line.ts` | 18 | **D** |
| `session/jsonl/codec.ts` | 45 | **R** → `durable/src/storage/jsonl/storage.ts` |
| `session/jsonl/types.ts` | 28 | **R** → 同上 |
| `session/jsonl/repo.ts` | 239 | **R** → `durable/src/storage/jsonl/index.ts` + `node.ts` |
| `session/jsonl/storage.ts` | 253 | **R** → `durable/src/storage/jsonl/storage.ts`（846 行；j=0.021） |
| `session/jsonl/index.ts` | 3 | **R** → 同上 |
| `session/jsonl/legacy-v3.ts` | 362 | **D**（旧 v3 迁移逻辑无对应；1.3.0 是 `durable/src/storage/sqlite/migrations.ts` 的新 schema 迁移体系） |
| `session/testing/**`（10 个） | 合计 ~1317 | **R** → `durable/src/testing/`（`storage-conformance.ts` **1663 行**、`env-conformance.ts` 633、`runner.ts`、`assertions.ts`、`index.ts`） |

#### 3.1.4 `harness/compaction/**`（3 个）

| 旧文件 | LOC | 去向 / 判定 |
|---|---:|---|
| `compaction/compaction.ts` | 510 | **P/R** → `coding-agent/src/core/compaction/compaction.ts`（j=0.31）；`durable` 侧另有 `durable/src/harness/compaction.ts`（464 行，j=0.046）= 重写 |
| `compaction/branch-summarization.ts` | 222 | **P/R** → `coding-agent/src/core/compaction/branch-summarization.ts`（j=0.30） |
| `compaction/utils.ts` | 92 | **P** → `coding-agent/src/core/compaction/utils.ts`（**j=0.49，cov=0.71**） |

> 这是「一分为二」的典型：**纯算法部分被 coding-agent 拿走**（保留较高相似度），**调度/持久化部分被 durable 重写**。

#### 3.1.5 `harness/tools/**`（10 个）→ 主要归 `durable/tools`，算法副本另存 coding-agent

| 旧文件 | LOC | 去向 / 判定 |
|---|---:|---|
| `tools/edit-diff.ts` | 306 | **P（唯一真·搬迁）** → `durable/src/tools/edit-diff.ts`，blob SHA 完全相同；`coding-agent/src/core/tools/edit-diff.ts` 是它的近亲（j=0.88） |
| `tools/path-utils.ts` | 23 | **P** → `durable/src/tools/path-utils.ts`（j=0.80） |
| `tools/edit.ts` | 106 | **P** → `durable/src/tools/edit.ts`（j=0.63）；coding-agent 副本 j=0.35 |
| `tools/write.ts` | 37 | **P** → `durable/src/tools/write.ts`（j=0.49） |
| `tools/image.ts` | 75 | **P** → `durable/src/tools/image.ts`（j=0.58/cov 0.83）+ `coding-agent/src/utils/mime.ts`（j=0.44） |
| `tools/bash.ts` | 115 | **R/P** → `durable/src/tools/bash.ts`（j=0.18）+ `coding-agent/src/core/tools/bash.ts`（j=0.06） |
| `tools/read.ts` | 109 | **R** → `coding-agent/src/core/tools/read.ts`（j=0.23）+ `durable/src/tools/read.ts`（j=0.12） |
| `tools/index.ts` | 21 | **R** → `durable/src/tools/index.ts`（`createCodingTools()` / `CodingTools`） |
| `tools/file-mutation-queue.ts` | 49 | **R** → `coding-agent/src/core/tools/file-mutation-queue.ts`（j=0.15）与 `durable/src/tools/file-mutation-queue.ts`（j=0.14）**两边各留一份** |
| `tools/tool-context.ts` | 5 | **D**（换成 `ToolExecutionApi` / `api.executeTool()`） |

#### 3.1.6 `harness/env`、`harness/utils`、`harness/execution`（9 个）

| 旧文件 | LOC | 去向 / 判定 |
|---|---:|---|
| `env/nodejs.ts` | 530 | **P** → `durable/src/env/node.ts`（1226 行；**j=0.60，cov=0.90**）。`NodeExecutionEnv` 类名保留（`durable/src/env/node.ts:603`），并新增 `openBinaryReader`/`openDirReader`/`watch`/`listFiles` |
| `utils/truncate.ts` | 195 | **P** → `durable/src/truncate.ts`（j=0.46）+ `coding-agent/src/core/tools/truncate.ts`（j=0.55） |
| `utils/output-capture.ts` | 178 | **R** → `durable/src/env/line-scan.ts`/`decode.ts` + `durable/src/truncate.ts`（j≈0.02） |
| `utils/shell-output.ts` | 91 | **R** → `durable/src/env/index.ts` 的 `ShellOutputWindow`/`ShellOutputSkip`/`ShellOutputInfo` |
| `utils/usage.ts` | 29 | **R** → `durable/src/harness/usage.ts`（`addUsage`/`recordUsage`/`UsageDoc`） |
| `utils/adaptive-publisher.ts` | 63 | **D** |
| `execution/assistant.ts` | 138 | **R** → `durable/src/harness/generation.ts` |
| `execution/tools.ts` | 146 | **R** → `durable/src/harness/tool.ts` |
| `execution/effect-gate.ts` | 47 | **D**（由 storage 事务原子性取代） |

### 3.2 `agent/src/search/**`、`node.ts`、`index.ts`

| 旧文件 | LOC | 内容 | 去向 |
|---|---:|---|---|
| `src/search/index.ts` | 19 | 纯 `interface` 声明：`SearchQuery` / `SessionSearchHit` / `EntrySearchHit` / `SessionSearchService`（会话与条目全文检索的服务契约） | **D**。v1.3.0 树里 `SearchService`/`search-service` 字符串 **0 命中**。**同名但不同物**的新东西：`durable/src/tool-search/`（`createToolSearchExtension()`，对 *deferred tools* 做 BM25 排序）+ `coding-agent/src/extensions/tool-search/`。旧 search 是「检索会话历史」，新 tool-search 是「检索工具清单」，**不要混淆** |
| `src/node.ts` | 2 | `export { NodeExecutionEnv } from "./harness/env/nodejs.ts"; export * from "./index.ts";` | **D**（`./node` subpath export 被删）。`NodeExecutionEnv` 现在从 `@earendil-works/pi-durable/env/node` 导出 |
| `src/index.ts` | 152 | 123 个具名导出 + 16 个 `export *` | **重写为 5 行**：`export *` agent.ts / agent-loop.ts / proxy.ts / types.ts + `setDefaultStreamFn` |

### 3.3 `session-backends/**` → `durable/**`

**先说结论：不是前身，是「同时被删」+「另一套 schema 顶上」。**

```bash
# session-backends 的 33 个文件在同一个 commit 里被删，0 renamed
gh api repos/earendil-works/pi/commits/7fd478a2 \
  --jq '[.files[]|select(.status=="renamed")]|length'    # 0
```

| 维度 | v0.85.1 `session-backends/sqlite-node` | v1.3.0 `durable/src/storage/sqlite` |
|---|---|---|
| 包名 | `@earendil-works/pi-session-backend-sqlite-node` | 无独立包（`@earendil-works/pi-durable/storage/sqlite[/node]`） |
| 依赖 | `pi-ai` + `pi-agent-core` | `chord` + `pi-ai` + `pi-codemode` |
| 表结构 | `sessions` / `entries` / `scalar_values` / `list_values` / `usage_ledger` | `durable_schema` / `durable_metadata` / `record_ids` / `conversations` / `entries` / `tasks` / `submissions` / `documents` / `document_revisions` |
| 迁移文件 | `src/sqlite/migrations/001_initial.sql` | `src/storage/sqlite/migrations.ts` + `schema.ts`（表名/索引名集中登记，供历史迁移渲染） |
| 对应接口 | `Storage`（11 个方法：`commit`/`getEntries`/`getValue`/`scanValues`/`readList`/`scanBranch`/`scanBranchStructure`/`scanEntries`/`scanUsage`/`getStats`/`close`） | `Storage`（15 个方法：`commit`/`mintId`/`conversation`/`scanConversations`/`entry`×2/`findLatestHeadMarker`/`scanEntries`/`task`/`scanTasks`/`submission`/`scanSubmissions`/`submissionByRequest`/`findDocument`/`document`/`scanDocuments`/`close`） |
| 相似度 | `sqlite/storage.ts`(8.0 KB) → `durable/src/storage/sqlite/storage.ts`：**j=0.02** | |
| 新增后端 | — | `storage/memory`、`storage/jsonl`、`storage/sqlite/cloudflare`（Durable Object） |

对 pi-py 的直接含义：**`pi-storage-sqlite` 现有 schema 与查询全部作废**，只能保留 `database.py`（`sqlite3` 封装 + PRAGMA + migration 执行器）这一层。

### 3.4 `protocol` / `client` / `server`：不是「无 package.json」，是「被整体删除」

**前提更正**：这三个包在 v0.85.1 **都有正式 `package.json`，都参与根 `build` 脚本**：

```bash
grep -E '^packages/(protocol|client)/package.json' /tmp/tree_0851.tsv
# packages/client/package.json    e4670c587417c17ba8eefa0f7b2d802b7cb40fe8
# packages/protocol/package.json  65353ec95075fc95ff3f9fc5590ec1e5f6bc66bf
```

| 包 | v0.85.1 名 / 版本 | 依赖 | 职责（README 原文摘要） | 结局 |
|---|---|---|---|---|
| `packages/protocol` | `@earendil-works/pi-protocol` 0.85.1 | `chord` | "Runtime-neutral routed envelopes, CBOR encoding, and byte-stream framing for the experimental Pi protocol"（协议版本 8；4 字节大端长度 + CBOR 帧） | ❌ v1.2.0 删除 |
| `packages/client` | `@earendil-works/pi-client` 0.85.1 | `chord`, `pi-protocol` | "Transport-neutral client for the experimental Pi service protocol" | ❌ v1.2.0 删除 |
| `packages/server` | `@earendil-works/pi-server` 0.85.1 | `chord`, `pi-agent-core`, `pi-protocol` | "Experimental local server for the new durable Session and Agent Harness interfaces" | ❌ v1.2.0 删除 |

删除 commit：`7472cda0`（2026-10-12）`feat(coding-agent): remove the experimental server stack and the client, protocol, and server packages`，161 个文件。时间点落在 **v1.1.0（10-07）之后、v1.2.0（10-12）**：v1.1.0 的 `packages/server/` 有 35 个 blob，v1.2.0 为 0。

**这三个包没有替代者**：1.3.0 树里 `pi-protocol`/`pi-client`/`pi-server` 无任何引用。coding-agent 改为自带 `src/modes/rpc/` + `package.json` 的 `"./rpc-entry"` subpath（`./dist/bundle/rpc-entry.js`）。

> 对 pi-py：`pi-server`（对齐 `@earendil-works/pi-server`）**上游已放弃**。继续维护它＝单方面维护一个上游不存在的包。

---

## 4. 新包职责与公开入口

### 4.1 `@earendil-works/pi-durable`（v1.0.0 首次发布，1.3.0 = 82 个 `src/*.ts`，~24,989 行）

- **描述**：*"Durable conversation, task, and document runtime for Pi"*（`durable/README.md` 首行标注 **Experimental**：*"The API changes without notice between releases."*）
- **职责边界**：会话（Conversation/Entry）＋ 任务（Task/TaskGraph）＋ 文档（Document）三者共用一条原子提交线；进程崩溃后 `resume()` 从 storage 续跑。**不是**「旧 harness 的新家」，而是一个新的运行时内核：
  - `Harness`（`durable/src/harness/harness.ts`，470 行）——打开 storage + 注册表，跑 agent
  - `Session`/`Conversation`/`Transaction`（`durable/src/session/*`）——提交、分支、观察
  - `scheduler.ts`（1953 行）——任务调度、ownership、abort 级联
  - `tool.ts`（911 行）——`ToolTask`、nested call、`callers`、deferred
  - `generation.ts`（715 行）——`GenerationTask`
  - `codemode/codemode.ts`（1037 行）——`codemode` 工具的 durable 版（脚本可跨重启续跑）
  - storage：`memory` / `jsonl` / `sqlite` / `sqlite/cloudflare`
  - env：`ExecutionEnv` 抽象 + `NodeExecutionEnv`（`/env/node`）
  - tools：`read`/`write`/`edit`/`bash`/`powershell` + `createCodingTools()`/`CodingTools` 扩展
  - `testing`：`registerStorageConformance()` / `registerEnvConformance()`

- **公开入口**（`durable/package.json` `exports`，**逐条实读**）：

| subpath | 源文件 |
|---|---|
| `.` | `src/index.ts` |
| `./env` | `src/env/index.ts` |
| `./env/node` | `src/env/node.ts` |
| `./codemode` | `src/codemode/index.ts` |
| `./tool-search` | `src/tool-search/index.ts` |
| `./tools` | `src/tools/index.ts` |
| `./images` / `./images/node` / `./images/cloudflare` | `src/images/{index,node,cloudflare}.ts` |
| `./storage/memory` | `src/storage/memory.ts` |
| `./storage/jsonl` / `./storage/jsonl/node` | `src/storage/jsonl/{index,node}.ts` |
| `./storage/sqlite` / `./storage/sqlite/node` / `./storage/sqlite/cloudflare` | `src/storage/sqlite/{index,node,cloudflare}.ts` |
| `./testing` | `src/testing/index.ts` |
| `./package.json` | — |

  注意所有 subpath 都带 `"source": "./src/*.ts"` 条件（monorepo 内直跑 TS）。

- **根导出**（`durable/src/index.ts`，205 行）关键分组：`Harness` / `createSession` / `createRegistry` / `defineExtension` / `defineTool` / `defineDoc` / `defineEntry` / `defineTask` / `hook` / `section` / `wrapTool` / `wrapSection` / `watchEvents` / `GenerationTask` / `ToolTask` / `CompactionTask` / `AssistantEntry` / `UserEntry` / `SystemEntry` / `ToolResultEntry` / `ResetEntry` / `CompactionEntry` / `MemoryStorage` / 错误类 `ConversationBusy` / `ReadAfterWrite` / `SessionFailed` / `StorageRequestError` / `ROOT_CONVERSATION_ID`。

### 4.2 `@earendil-works/pi-mcp`（v0.99.0 新增，32 个文件）

- **描述**：*"Standalone Model Context Protocol client for pi and other applications"*
- **职责边界**：**独立 MCP 客户端**，不绑定 pi 运行时。自带 JSON-RPC 协议层、三种 transport（stdio / streamable-http / in-memory）、OAuth（discovery/callback/flow/provider）、内容类型转换 `toLlmContent()`。
- **依赖**：仅 `cross-spawn`（**不依赖 `pi-ai`**）。
- **exports**：`.`（`src/index.ts`）、`./oauth`（`src/oauth/index.ts`）、`./testing`（`src/testing/index.ts`）。
- **公开 API**（实读 `mcp/src/index.ts`）：`McpClient`、`StdioTransport`、`StreamableHttpTransport`、`McpTransport`、`parseJsonRpcMessage`、`JSON_RPC_ERROR_CODES`、`McpError`/`McpAbortError`/`McpTimeoutError`/`McpConnectionClosedError`/`McpHttpError`/`McpSessionExpiredError`/`McpAuthRequiredError`、MCP 协议类型（`Tool`/`InitializeParams`/`ServerCapabilities`/…）、`toLlmContent`。
- 值得注意的是：`pi-agent-core` 的 README 把 MCP 与 codemode 作为 **`AgentTool` 的用法示例**（`agent/examples/mcp-codemode/`），即 MCP 在 1.3.0 是**独立的、可选的**能力，不再内建在 agent 循环里。

### 4.3 `@earendil-works/pi-codemode`（v0.99.0 新增，35 个文件）

- **描述**：*"Sandboxed JavaScript execution where the only capability is calling injected tools"*
- **职责边界**：QuickJS-WASM 沙箱（唯一依赖 `quickjs-wasi@3.6.2`）。宿主、内联、远程（`RemoteCodemodeSandbox`）、Cloudflare 四种运行形态；源码语法（`CODEMODE_SOURCE_GRAMMAR`，Lark 语法）+ 声明渲染（`renderDeclarations`）。**不含任何 agent 概念**。
- **exports**：`.`、`./declarations`、`./source`、`./portable`、`./cloudflare`、`./worker`。
- **公开 API**（实读 `codemode/src/index.ts`）：`CodemodeSandbox`、`CodemodeSandboxBase`、`InlineCodemodeSandbox`、`RemoteCodemodeSandbox`、`serveCodemodeRemote`、`parseCodemodeSource`、`renderDeclarations`、`toCodemodeIdentifier`、`loadQuickJSWasm`、上限常量 `MAX_OUTPUT_CHARS`/`MAX_OUTPUT_ITEMS`/`MAX_STORE_TOTAL_CHARS`/`MAX_STORE_VALUE_CHARS`。
- **与 durable 的关系**：`durable/src/codemode/`（1037 行）是**把沙箱接进 durable 任务模型**的适配层（journal 重放、nested call 复用），sandbox 本身在 `pi-codemode`。

### 4.4 `@earendil-works/pi-env`（v1.1.0 新增，42 个文件 + Rust daemon）

- **描述**：*"Remote execution environments for Pi Durable: a small daemon deployed over SSH and its ExecutionEnv client"*
- **职责边界**：把 `durable` 的 `ExecutionEnv` 实现搬到远端机器（Rust daemon 经 SSH 部署 + framed stdio 协议）。**只有 `.` 一个 exports**。
- **依赖**：`chord` + `pi-durable`（**唯一依赖 durable 的包**）。
- 对 pi-py 的优先级判断：这是「远程执行」这一非常垂直的需求，**建议本轮不追**。

### 4.5 `protocol` / `client` 是什么？（回答 Lead 的疑问）

**它们是 v0.85.1 的正式包，不是内部/私有包**（见 §3.4 的 `package.json` 证据）。三个包合起来是**上游一整套「实验性 server 栈」**：`protocol`（CBOR 帧 + 路由信封）→ `client`（传输无关客户端）→ `server`（本地 server + Session 路由 + Unix transport），并由 `coding-agent/src/cli/experimental/` 与 `coding-agent/src/experimental/mini/`、`.../services/` 消费。

它们在 **v1.2.0 被整体删除**（commit `7472cda0`），没有替代实现。所以：
- 「protocol/client 没有 package.json」这一观察**不成立**；
- 但「它们已不在 1.3.0」是对的，且**不是改名，是废弃**。

---

## 5. 对 pi-py 的影响判断

### 5.1 pi-py 现状（实读）

| pi-py 包 | 版本 | 上游对应（`PORTING.md` 自称） | 上游现状 |
|---|---|---|---|
| `pi-ai` | 0.85.1 | `@earendil-works/pi-ai` | 存在（报告 01 覆盖） |
| `pi-agent-core` | 0.85.1 | `@earendil-works/pi-agent-core` | 存在，但 **harness 已被删除** |
| `pi-storage-sqlite` | 0.85.1 | `@earendil-works/pi-storage-sqlite-node`（README 表所写） | ❌ **名字与路径都对不上了**（真实包名 `@earendil-works/pi-session-backend-sqlite-node`，路径 `packages/session-backends/sqlite-node`，且已删） |
| `pi-coding-agent` | 0.85.1 | `@earendil-works/pi-coding-agent` | 存在 |
| `pi-server` | 0.85.1 | `@earendil-works/pi-server` | ❌ **v1.2.0 已删除** |

`pi-agent-core` 代码量（`find | xargs wc -l`）：

```
src/pi_agent_core/agent_loop.py    697   src/pi_agent_core/agent.py         381
src/pi_agent_core/harness/session.py    439   src/pi_agent_core/types.py    306
src/pi_agent_core/harness/skills.py     383   src/pi_agent_core/harness/compaction.py 263
src/pi_agent_core/__init__.py           164   src/pi_agent_core/event_stream.py  22
（tests 共 1,535 行）
```

`pi-storage-sqlite`：`database.py`(131) + `storage.py`(316) + `migrations.sql`。

依赖方向：`pi-ai` ← `pi-agent-core` ← {`pi-storage-sqlite`, `pi-coding-agent`} ← `pi-server`。

### 5.2 上游新包 ←→ pi-py 现有代码的映射

| pi-py 现状 | 上游 1.3.0 对应 | 需要做什么 |
|---|---|---|
| `pi_agent_core/agent_loop.py` + `agent.py` + `types.py` + `event_stream.py` | `@earendil-works/pi-agent-core`（1.3.0） | **保留 + 按 §7 对齐 API 破坏性变更**（`shouldStopAfterTurn` → `finishTurn`、新增 `prepareRequest`、`AgentTool.outputSchema`、`state.systemPrompt` 只读） |
| `pi_agent_core/harness/session.py` | 上游**无同名物**；功能落在 `pi-durable` 的 `Conversation`/`Entry`/`Document`/`Storage` | **重写**（若追 durable）或**改名 + 保留**（若不追）|
| `pi_agent_core/harness/compaction.py` | `pi-coding-agent/src/core/compaction/*`（j≈0.30–0.49） | **可保留**，算法层可移植性最好 |
| `pi_agent_core/harness/skills.py` | `pi-coding-agent/src/core/skills.ts`（j=0.12） | **可保留**（上游 skills 也搬去了 coding-agent） |
| `pi_storage_sqlite/database.py` | `pi-durable/src/storage/sqlite/{database,node,migrations}.ts` | **可保留**（sqlite3 封装 + PRAGMA + migration 机制） |
| `pi_storage_sqlite/storage.py` + `migrations.sql` | `pi-durable/src/storage/sqlite/{storage,schema}.ts` | **作废重写**（schema 与 `Storage` 接口都不同，见 §3.3） |
| `pi_coding_agent/*` | `@earendil-works/pi-coding-agent` | 独立线（不依赖 durable，上游亦然） |
| `pi_server/*` | **无**（v1.2.0 删除） | **决策：下线 / 冻结 / 明确登记为「上游已废弃但本端保留」** |
| —（不存在） | `@earendil-works/pi-durable` | 若要追：**净新增一个大包**（TS 侧 82 个 src 文件 / ~25k 行） |
| —（不存在） | `@earendil-works/pi-mcp` / `pi-codemode` / `pi-env` | 可选，与本条线弱耦合 |

### 5.3 「搬家 / 重写 / 保留」三分类（针对 pi-py 现有文件）

**可以直接保留（最多做接口适配）**
- `pi_agent_core/agent_loop.py`、`agent.py`、`event_stream.py` —— 结构对齐 1.3.0，仅需 API 级改动（§7）
- `pi_agent_core/harness/compaction.py` —— 上游算法层去 coding-agent，逻辑稳定
- `pi_agent_core/harness/skills.py` —— 同上；且 pi-py 已在 PORTING.md 登记过有意偏离
- `pi_storage_sqlite/database.py` —— sqlite3 封装层与 schema 无关
- `pi_coding_agent/*` —— 与本次重组解耦

**需要搬家（同一份代码换归属）**
- `pi_agent_core/harness/*` → 若决定追上游语义，**归属应改为「pi-py 自己的会话层」或「新的 pi-durable」**，不能继续叫 `harness`（上游该名字已指向一个 25k 行的不同东西，会造成理解灾难）
- `pi_storage_sqlite/*` → 若追 durable，则应变成 `pi_durable.storage.sqlite` 而非独立包

**需要重写**
- `pi_storage_sqlite/storage.py` + `migrations.sql`（schema 与查询全变）
- `pi_agent_core/harness/session.py`（上游会话模型从「树 + 条目类型枚举」变成「Conversation + 不可变 Entry + Document + Task + 原子提交」）
- `pi_server/*`（上游无对应，能否保留取决于产品决策）

---

## 6. 建议的目标包拓扑（3 个方案 + 代价）

### 方案 A：严格镜像上游

```
pi-ai             ← @earendil-works/pi-ai
pi-agent-core     ← @earendil-works/pi-agent-core（只剩 Agent/loop/proxy/types）
pi-durable        ← @earendil-works/pi-durable        【新】
pi-codemode       ← @earendil-works/pi-codemode       【新，可选】
pi-mcp            ← @earendil-works/pi-mcp            【新，可选】
pi-coding-agent   ← @earendil-works/pi-coding-agent
（pi-storage-sqlite 并入 pi-durable；pi-server 下线）
```

依赖方向：`pi-ai ← pi-agent-core`；`pi-codemode ← pi-durable → {chord 等价物, pi-ai}`；`pi-coding-agent → {pi-ai, pi-agent-core}`（上游 coding-agent 不依赖 durable）。

**理由**
- 包边界与上游 1:1，`SYNC.md` 的「逐包 diff 对照」流程可原样保留；
- `PORTING.md` 的「有意偏离」表可以大幅缩短（harness 偏离项直接消失）。

**代价（必须明说）**
- `pi-durable` 是 **~25k 行 TS / 82 个 src 文件**的全新重写：scheduler（1953 行）、tool task（911）、session/transaction（1081+1043）、4 种 storage、codemode 集成、conformance 测试设施（1663+633 行）。这是本仓库历史上最大的单包工作量，**远超 0.85.1 全量同步**；
- pi-py 还需要一个 `chord` 等价物（不可变 JSON 状态 + delta 追踪 + 观察者）或把 durable 简化到不依赖它 —— 上游 `chord` 是 1.3.0 的独立包，py 侧没有；
- 新增 2–4 个 PyPI 发行名（`pi-py-durable` 等）、CI job、发布流水线、依赖上下界；
- **上游 coding-agent 不用 durable**，所以 pi-durable 在 pi-py 里也会是「孤岛包」，除非 pi-py 自己造消费场景；
- 破坏性：`pi-agent-core` 现有 `Session`/`SessionStorage`/`JsonlSessionStorage`/`Skills`/compaction 的公开导入路径全部消失，属于**一次 1.0 级 break**。

### 方案 B：保持 5 包形态，内部重组（最小 break）

```
pi-ai             （不变）
pi-agent-core     = agent loop + Agent + proxy/types
                    + pi_agent_core.sessions（原 harness/ 改名，公开登记为本地扩展）
pi-storage-sqlite = pi_agent_core.sessions 的 SQLite 后端（不再是「上游包对应物」）
pi-coding-agent   （不变）
pi-server         （决策：下线，或明确冻结）
```

依赖方向不变（`pi-ai` ← `pi-agent-core` ← {`pi-storage-sqlite`, `pi-coding-agent`}）。

**理由**
- 不新增包、不动 CI/发布名，代价最低；
- 保住全部现有测试（1,535 行 test 无需改动）；
- 立即消除「对齐 v0.85.1 的 `@earendil-works/pi-storage-sqlite-node`」这类**引用了不存在的上游包名**的错误陈述。

**代价**
- 包边界与上游仍然不一致，未来每轮同步都要重新做「本端哪个模块对应上游哪个包」的映射；
- 「harness」这个词在上游已经指向完全不同的东西，**必须改名**（建议 `sessions`），否则后续读者会持续误判；
- 永远拿不到 durable 的能力（崩溃续跑、任务图、文档、nested tool call、deferred tools、codemode）——如果 pi-py 的定位是 SDK，这可能恰好是可接受的裁剪；
- 需要一个「上游已删除/已废弃包」的登记机制（`UPSTREAM_DELTA.md` 或 PORTING.md 新小节），否则 `pi-server` 会变成无主债。

### 方案 C：分阶段（推荐）

```
阶段 1（本轮，1.0）：只做「agent-core 瘦身 + API 对齐 + 命名纠偏」
  pi-agent-core   = agent loop / Agent / proxy / types（对齐 1.3.0）
                    harness/ → sessions/（改名，PORTING.md 登记为本地扩展）
  pi-storage-sqlite  保留，PORTING.md 上游引用改为「无（上游包已删）」
  pi-server          决策：下线（推荐）或冻结并登记
阶段 2（按需）：只有当 pi-py 真要「崩溃续跑 / 持久任务 / 文档状态」时，
                再引入 pi-durable（届时可裁到 scheduler + session + storage/sqlite 的最小集）
阶段 3（可选）：pi-mcp / pi-codemode（互相独立，无 durable 依赖，可单独摘）
```

**理由**
- 阶段 1 是**不可回避**的：`pi-agent-core` 与上游的 API 已经分叉（§7），且 `pi-storage-sqlite`/`pi-server` 的上游引用已失效。无论选 A 还是 B，这部分都要做；
- 把最大的一块（25k 行 durable）从 1.0 的关键路径上摘下来，避免追赶计划被单包阻塞；
- 保留 A/B 的切换可能性：阶段 1 结束时，`sessions/` 的边界已经和 durable 的 `Conversation` 概念对齐过，届时升格为独立包的成本比现在低。

**代价**
- 阶段 1 结束后 pi-py 仍然「不是上游的镜像」，需要维护一份「本端扩展」清单；
- 若将来真要做 durable，`sessions/` 会经历第二次 break（第一次是改名，第二次是换成 durable 模型）；
- 「分阶段」在文档上要求更高：必须把「当前对齐到哪一步」写清楚，否则 README 的包表会继续误导（现状就是如此）。

### 三个方案对比

| 维度 | A 严格镜像 | B 内部重组 | C 分阶段（推荐） |
|---|---|---|---|
| 新增包数 | +1 ~ +4 | 0 | 0（本轮） |
| 本轮工作量 | 极大（≥25k 行） | 小 | 中 |
| 包边界与上游一致 | ✅ 完全 | ❌ | ⏳ 阶段 1 后部分一致 |
| 破坏现有导入路径 | 是 | 最小 | 是（`harness` → `sessions`） |
| 复用 SYNC.md 逐包流程 | ✅ | ❌ 需映射表 | 部分 |
| 拿到 durable 能力 | ✅ | ❌ | 延后 |
| 主要风险 | 单包阻塞整轮同步 | 错配长期化 | 二次 break |

---

## 7. 破坏性 API 变更：agent loop before / after

### 7.1 包级（`packages/agent/package.json`）

| 项 | v0.85.1 | v1.3.0 |
|---|---|---|
| `exports` 子路径 | `"."`, `"./node"`, `"./harness/context"`, `"./harness/env/nodejs"`, `"./harness/runtime/reducer"`, `"./harness/session"`, `"./harness/session/testing"`, `"./package.json"` | `"."`, `"./package.json"` |
| `dependencies` | `chord`, `pi-ai`, `pi-telemetry`, `diff`, `ignore`, `typebox`, `yaml`（7） | `pi-ai`, `typebox`（2） |
| 根导出 | 123 个具名 + 16 个 `export *` | 1 个具名（`setDefaultStreamFn`）+ 4 个 `export *` |
| 构建工具 | `tsgo -p tsconfig.build.json` | `tsc -p tsconfig.build.json` |

**被删掉的公开导出族**（旧 `src/index.ts` 实读）：
`AgentHarness*`（9 个类型）、`Compaction*`（`compact`/`CompactionSettings`/`CompactResult`/`CompactionError`/`calculateContextTokens`/`estimateContextTokens`/`estimateTokens`/`findCutPoint`/`findTurnStartIndex`/`generateSummary`/`generateSummaryWithUsage`/`getLastAssistantUsage`/`prepareCompaction`/`serializeConversation`/`shouldCompact`/`DEFAULT_COMPACTION_SETTINGS`）、`Branch*`（`generateBranchSummary`/`prepareBranchEntries`/`collectEntriesForBranchSummary`/…）、`ExecutionEnv`/`FileSystem`/`Shell`/`Shell*`（13 个）、`Skill`/`PromptTemplate`、`ExecutionError`/`FileError`/`err`/`ok`/`getOrThrow`/`getOrUndefined`/`toError`、全部 `Telemetry*`/`Span*`/`AiSpan`/`HarnessSpan`/`AGENT_TELEMETRY_SCHEMAS`/`startAiSpan`/`startHarnessSpan`、`uuidv7`、`InMemoryTelemetryContext`/`NOOP_TELEMETRY_CONTEXT`/`createTypedSpanStarter`/`defineTelemetrySchema`、`reduceLaneSnapshot`/`LaneSnapshotReduction`、`applyShellOutputUpdate`、全部 search 类型。

CHANGELOG 原文（`packages/agent/CHANGELOG.md` v1.0.0 → Breaking Changes）：

> Removed the experimental harness from `@earendil-works/pi-agent-core`: `AgentHarness`, sessions and session storage, the durable runtime, pico3, harness tools, compaction, skills, prompt templates, system prompt helpers, telemetry schemas, the search service types, and the `uuidv7` and pi-telemetry re-exports. The `./node`, `./harness/*`, and `./experimental/pico3` subpath exports are gone. The package now contains only `Agent`, the agent loop, the proxy stream, and their types. Use `@earendil-works/pi-durable` for durable sessions.

### 7.2 `AgentLoopConfig`（`types.ts`，逐字段实读 diff）

| 字段 | v0.85.1 | v1.3.0 | 说明 |
|---|---|---|---|
| `shouldStopAfterTurn?` | ✅ | ❌ **删除** | 由 `finishTurn` 取代（v0.87.0 Breaking） |
| `finishTurn?` | ❌ | ✅ **新增** | 返回 `{ action: "continue" } \| { action: "end" } \| undefined`；对 normal/error/aborted 都运行，error/aborted 的决策被忽略 |
| `prepareRequest?` | ❌ | ✅ **新增** | 每次 provider 请求前运行（含第一次），可替换 `context.messages` |
| `prepareNextTurn?` | ✅ | ✅ | 语义有调整（v0.84.4 起仅在确定继续下一轮时运行） |
| `transformContext` / `getApiKey` / `getSteeringMessages` / `getFollowUpMessages` / `toolExecution` / `beforeToolCall` / `afterToolCall` / `convertToLlm` / `model` | ✅ | ✅ | 不变 |

`types.ts` 新增导出：`AgentTurnContext`、`AgentTurnDecision`、`FinishTurn`、`PrepareRequest`、`PrepareRequestContext`、`AgentRequestUpdate`、`AgentToolCallOutcome`。
删除导出：`ShouldStopAfterTurnContext`。

### 7.3 `AgentOptions`（`agent.ts`）

| 字段 | v0.85.1 | v1.3.0 |
|---|---|---|
| `shouldStopAfterTurn?` | ✅ | ❌ **删除** |
| `finishTurn?` | ❌ | ✅ **新增** |
| `prepareRequest?` | ❌ | ✅ **新增** |
| `onProviderStreamEvent?` | ❌ | ✅ **新增**（v0.99.0） |
| `initialState?` | `Partial<Omit<AgentState, …>>` | `AgentInitialState`（新类型别名） |
| 其余（`convertToLlm`/`transformContext`/`streamFn`/`getApiKey`/`onPayload`/`onResponse`/`beforeToolCall`/`afterToolCall`/`prepareNextTurn`/`prepareNextTurnWithContext`/`steeringMode`/`followUpMode`/`sessionId`/`thinkingBudgets`/`transport`/`maxRetryDelayMs`/`toolExecution`） | ✅ | ✅ 不变 |

`Agent` 类新增方法：`peekQueuedMessages()`（v0.87.0）。
`AgentState.systemPrompt` 从可写变为 **`readonly`**（系统提示词由 transcript 拥有；改提示词要 append system message）。

### 7.4 `AgentTool`

| 字段 | v0.85.1 | v1.3.0 |
|---|---|---|
| `outputSchema?` | ❌ | ✅ **新增**（对应 durable 的 `structuredOutputSchema`） |
| `label` / `prepareArguments` / `execute` / `replay` / `executionMode` | ✅ | ✅ 不变 |

**对 pi-py 的直接影响**（实读 pi-py 代码）：

```
packages/pi-agent-core/src/pi_agent_core/agent.py:90      prepare_next_turn: Any = None
packages/pi-agent-core/src/pi_agent_core/agent.py:91      should_stop_after_turn: Any = None
packages/pi-agent-core/src/pi_agent_core/agent_loop.py:262 if config.should_stop_after_turn:
packages/pi-agent-core/src/pi_agent_core/types.py:184     should_stop_after_turn: Any = None
packages/pi-agent-core/src/pi_agent_core/types.py:185     prepare_next_turn: Any = None
```

→ pi-py 仍在 v0.85.1 的 `should_stop_after_turn` 语义上，**必须迁移到 `finish_turn`**，并补 `prepare_request`、`AgentTool.output_schema`、`AgentState.system_prompt` 只读化、`peek_queued_messages()`。

---

## 8. 开放问题（需要 Lead / 决策者回答）

1. **`pi-server` 怎么办？** 上游 v1.2.0 已删除 `@earendil-works/pi-server`（连同 protocol/client）。继续维护 = 单方面维护上游不存在的包；下线 = 破坏 pi-py 用户。需要一个明确决策，并同步 `README.md` 的包表。
2. **`pi-durable` 要不要做？** 若做，必须先回答：py 侧 `chord` 等价物（不可变 JSON + delta + 观察者 + 事务）是自己写、简化掉、还是引第三方。这是 durable 能否落地的前置条件，**没有它 durable 无法移植**。
3. **上游 `durable` 自己标注 "Experimental. The API changes without notice between releases."** 在 1.2.0 一个 minor 里就有 6 条 Breaking Changes（`ToolTaskInput`/`ToolTaskResult` 变判别联合、`ToolExecutionResult.content`→`output`、storage 错误模型重写、display-only write 语义变更）。**照抄它的 API 会让 pi-py 背上高频 break 的债**——是否值得？
4. **`pi-agent-core` 的 `harness/` 改名成什么？** 建议 `sessions`（对应上游 durable 的 `Conversation` 语义），但需确认不与上游未来命名冲突。
5. **`pi-storage-sqlite` 的定位**：上游对应包已删。是（a）继续作为独立 PyPI 包，（b）并入 `pi-agent-core`，（c）未来并入 `pi-durable`？三者对发布流水线影响不同。
6. **`pi-coding-agent` 是否要跟上上游的 `compaction`/`skills`/`messages` 落点？** 上游把这三个从 agent-core 挪进了 `coding-agent/src/core/`；pi-py 目前它们还在 `pi-agent-core`。若追上游，需要把一个完整的 `core/` 搬到 `pi-coding-agent`（而 pi-py 的 `pi-coding-agent` 只有 4 个文件、还是大幅裁剪版）。
7. **`pi-mcp` / `pi-codemode` 的优先级**：二者**不依赖 durable**，可独立摘取（mcp 仅需 `cross-spawn` 等价物 + httpx；codemode 需要 QuickJS-WASM 的 Python 绑定）。是否本轮排期？
8. **中间版本的 `pico` / `pico3` 谱系要不要建图？** 本报告只覆盖 0.85.1→1.3.0 两端。若后续要追 v0.87–v0.99 的行为，需要单独一轮。

---

## 9. 证据附录

### 9.1 关键 commit

| commit | 日期 | 标题 | 影响 |
|---|---|---|---|
| `08016016` | 2026-09-18 | `feat(durable): move Pico into dedicated package` | `packages/agent/src/pico/**` → `packages/durable/**`（7 rename）；18 files |
| `7fd478a2` | 2026-10-01 | `feat(agent): remove the experimental harness from pi-agent-core` | **removed 290 / added 0 / renamed 0**；删掉 `agent/src/harness/**`、`agent/docs/**`、`agent/test/**`、`packages/session-backends/**`、`mini-test.sh` |
| `7472cda0` | 2026-10-12 | `feat(coding-agent): remove the experimental server stack and the client, protocol, and server packages` | 161 files；删掉 `packages/{server,client,protocol}/**` |

### 9.2 落地文件（本次侦察产生的中间产物，均在 `/tmp`，不入库）

```
/tmp/tree_0851.tsv      # v0.85.1 全树 path<TAB>blobsha
/tmp/tree_130.tsv       # v1.3.0  全树
/tmp/diff_removed.txt   # 446 条 removed 路径
/tmp/diff_added.txt     # 702 条 added 路径
/tmp/exact_renames.tsv  # 4 条 blob 相同的移动
/tmp/renames_all.tsv    # 6 条 git rename 检测结果
/tmp/agent_map.json     # 214 个旧 agent 文件 × top3 相似候选
/tmp/map_agent.py       # 相似度脚本
```

### 9.3 复现命令速查

```bash
# 包清单
ls /tmp/pi-up/v0.85.1/packages/   # agent ai chord client coding-agent evals protocol server session-backends telemetry tui
ls /tmp/pi-up/v1.3.0/packages/    # agent ai chord codemode coding-agent durable env evals mcp telemetry tui

# agent/src .ts 数量
awk -F'\t' '$1 ~ /^packages\/agent\/src\/.*\.ts$/' /tmp/tree_0851.tsv | wc -l   # 90
awk -F'\t' '$1 ~ /^packages\/agent\/src\/.*\.ts$/' /tmp/tree_130.tsv  | wc -l   #  6

# harness 82 个 .ts 的去向（相似度表）
python3 - <<'EOF'
import json; d=json.load(open('/tmp/agent_map.json'))
for r,n,b in sorted(d):
    if r.startswith('v0.85.1/packages/agent/src/harness/') and r.endswith('.ts'):
        print(r.split('agent/')[1], n, [(j,x.split('packages/')[1]) for j,c,x in b[:1]])
EOF

# 唯一真·搬迁（blob 相同）
grep 'edit-diff' /tmp/exact_renames.tsv

# 两个 harness 的相似度（重写证据）
diff -q /tmp/pi-up/v0.85.1/packages/agent/src/harness/runtime/lane.ts \
        /tmp/pi-up/v1.3.0/packages/durable/src/harness/scheduler.ts   # 不同
# 行集合 Jaccard = 0.013

# 新包 exports
python3 -c "import json;print(json.load(open('/tmp/pi-up/v1.3.0/packages/durable/package.json'))['exports'].keys())"
```

### 9.4 一句话给追赶计划

> **旧 `pi-agent-core` 的 harness 不是「被搬走了」，而是「被删掉、由另一个从 Pico 长起来的新包按新模型重写」。** pi-py 的 `harness/` 因此不是「路径过时」，而是「语义过时」——继续叫这个名字、继续按旧模型演进的成本，会随上游每一轮同步递增。

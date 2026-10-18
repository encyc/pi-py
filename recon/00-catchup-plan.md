# pi-py 追赶与跟踪方案（提案，待讨论）

- 编写：Lead，2026-10-18
- 依据：`recon/01..04`（四份侦察报告）+ Lead 独立复核
- 上游基线：`earendil-works/pi` **v1.3.0**（2026-10-15）
- 本仓基线：`UPSTREAM_VERSION = 0.85.1`

---

## 0. 一句话结论

上游 1.0 的变动**看起来很大、实际上不吓人**：676 commits / 300 files / 16 个 release，
但真正影响 pi-py 已暴露 API 的**破坏性条目只有 ~30 条**。

真正的问题不是「追多少」，而是**pi-py 一直把自己说成「上游 v0.85.1 的对齐复刻」，
而它实际是一个 ~8,900 行的精选子集，且部分 `PORTING.md` 的说法与代码不符**。
追赶计划的第一步应该是**把定位说准**，否则排期会系统性失真。

---

## 1. 三个必须先纠正的认知

### 1.1 `UPSTREAM_VERSION = 0.85.1` 高估了对齐度

| 证据 | 事实 |
|---|---|
| `packages/pi-coding-agent/PORTING.md:12-14` | 裁剪表声称 `src/modes/rpc/`、`src/extensions/`、`src/utils/` 已复刻 —— **实际 0 行** |
| `packages/pi-coding-agent` 实测 | 4 个 `.py` / **1,263 LOC**，对上游 `core/` 的 **~4%** |
| `packages/pi-agent-core/PORTING.md`「v0.85.1 同步说明」 | 自述「**未移植（裁剪范围）**：上游本轮 agent 包重写为 harness v3 / durable drive…继续维持既有偏离」 |
| 全仓 `PORTING.md`「cherry-pick」小节 | 5 个包**全部为空**，从未走过 cherry-pick 流程 |

即：**分叉不是从 0.85.1 开始的，而是 0.84–0.85 期间上游重写 harness 时就已经发生**，
pi-py 当时选择「维持既有偏离」，此后 `UPSTREAM_VERSION` 只反映「我读过哪个版本」，
不反映「我实现了多少」。

### 1.2 上游不是「搬家」，是「删除 + 另起一个包重写」

Lead 最初判断「harness 从 agent 搬到了 durable」，**这个判断是错的**，侦察报告用 commit 证据纠正了：

| 事实 | 证据 |
|---|---|
| `packages/agent/src/harness/**` 82 个 `.ts` 被**删除** | commit `7fd478a2`（2026-10-01）改动量 **removed 290 / added 0 / renamed 0** |
| `pi-durable` 是 **`pico` 的后继**，不是 `session-backends` 的后继 | commit `08016016`（2026-09-18）7 个 rename 全部是 `agent/src/pico/**` → `durable/src/**` |
| 新旧 harness 几乎无文本延续 | 逐文件行集合 Jaccard **0.003–0.05** |
| 整条链上真正「搬迁」的文件只有 1 个 | `harness/tools/edit-diff.ts` → `durable/src/tools/edit-diff.ts`（blob SHA 相同） |
| `session-backends` 同样被整体删除，schema 完全不同 | 旧 `sessions/entries/scalar_values/…` vs 新 `conversations/entries/tasks/submissions/documents/…` |

**含义**：pi-py 的 `pi-storage-sqlite` 不是「换了个上游包名」，是**一整套数据模型失去了对应物**。

### 1.3 pi-py 有一个包在跟踪已被上游删除的东西

| pi-py 包 | 自称对应上游 | 上游现状（v1.3.0） |
|---|---|---|
| `pi-server` | `@earendil-works/pi-server` | ❌ **v1.2.0 删除**（commit `7472cda0`，连同 `pi-protocol`、`pi-client`） |
| `pi-storage-sqlite` | `@earendil-works/pi-storage-sqlite-node` | ❌ 真实包名是 `@earendil-works/pi-session-backend-sqlite-node`，路径 `packages/session-backends/sqlite-node`，**已删** |
| `pi-agent-core` | `@earendil-works/pi-agent-core` | ⚠️ 存在，但 harness 已删；pi-py 的 harness 是**本地扩展** |

> 补充：`pi-protocol` / `pi-client` **是有 `package.json` 的正式包**（Lead 曾怀疑它们是内部包，不成立）；
> 它们是「实验性 server 栈」的三个组成部分，一起被废弃，**没有替代实现**。

---

## 2. 上游 1.0 的实际变动面

### 2.1 结构：包从 11 个重整为 11 个，但内容大换血

```
v0.85.1  agent ai chord client coding-agent evals protocol server session-backends telemetry tui
v1.3.0   agent ai chord codemode coding-agent durable env evals mcp telemetry tui
                                    ↑新增     ↑新增(自 pico)  ↑新增  ↑新增
                          ✗删除 client / protocol / server / session-backends
```

- `agent`：**90 → 6 个 `.ts`**（只剩 agent-loop / agent / proxy / stream-fn / types / index）
- `durable`：新增，**82 个 src 文件 / ~25k 行**，自标 *"Experimental. The API changes without notice between releases."*
- 反直觉事实：**上游 `pi-coding-agent` 完全不依赖 `pi-durable`**（grep = 0）—— 全仓库只有 `pi-env` 依赖它

### 2.2 规模 vs 真实工作量

| 指标 | 数值 |
|---|---:|
| commits（0.85.1→1.3.0） | 676 |
| 变更文件 | 300 |
| release 数 | 16 |
| **AI 包 Breaking 条目** | **8** |
| **agent-core Breaking 条目** | **2** |
| durable Breaking 条目 | 14 |
| mcp / codemode Breaking | 2 / 2 |
| coding-agent Breaking（core 相关 10 / 纯 CLI 1 / 继承 2） | 13 |

**结论**：churn 的大头是新增 provider / 模型目录 / 修复，**不是破坏性变更**。
按报告 01 的估算，pi-ai 的 MUST-PORT 是 **S×11 / M×5 / L×1**；
按报告 04，coding-agent 的 Tier-A 是 **9 项 / 约 1–2 人周**。

### 2.3 真正的价值在「静默 bug」，不在新功能

报告 01 列出的高价值项全部是**「不跟就会行为不一致」**类型：

- 坏 JSON 工具参数**不再静默执行**（新增 `argumentsError`）
- JSON 合法转义集去掉 `"u"`（`\usepackage` 丢字段）
- 跨模型 thinking 由「转文本」**反转为丢弃**
- 不可解析 `Retry-After` 导致**立即重试**
- 重试延迟**默认被 60s 封顶**（默认行为变更）
- 字符/token 估算 **4 → 3.5**
- 未知端点 strict schema 默认由启发式改为 `false`

---

## 3. 需要拍板的四个决策

### D1　`pi-durable` 做不做？（最大的一块）

| | 做 | 不做 |
|---|---|---|
| 成本 | ~25k 行 TS 等价物 + `chord` 等价物（py 侧没有） | 永远拿不到崩溃续跑／任务图／文档状态 |
| 风险 | 上游自标 Experimental，1.2.0 **一个 minor 就有 6 条 Breaking** | 定位收窄为「无持久化运行时」 |
| 建议 | ❌ **本轮不做** | ✅ |

**理由**：上游自己都不依赖它，且公开声明 API 不稳定。照抄会把高频 break 的债背到 pi-py 身上。

### D2　`src/extensions/`（codemode + mcp + tool-search，**5,915 LOC**）纳不纳入？

- 这是本轮**最大的能力增量**，而且**被 re-export 进 SDK 包入口** `src/index.ts`
- codemode 需要 Python 侧的 JS 沙箱（上游用 `quickjs-wasi`），是硬前置
- mcp **不依赖 durable**，可独立摘取（httpx + 子进程即可）

**建议**：本轮不纳入；mcp 可单独立项（它最独立、收益最直接）。

### D3　`pi-server` 去留？

上游已删除，pi-py 单方面维护 646 LOC + 一个 PyPI 发行名。
其 `PORTING.md` 自己写着「本包为**可选**…最后评估是否值得实现」。

**建议**：✅ **下线**（保留 git tag，README 包表移除，PyPI 发一个最终版标注 deprecated）。

### D4　`pi-agent-core` 的 API 对齐 + `harness/` 改名

这一项**不可回避**——上游 agent-core 的公开 API 已经分叉：

| 上游 1.3.0 | pi-py 现状 |
|---|---|
| `finishTurn`（替代 `shouldStopAfterTurn`） | 有 `should_stop_after_turn`，无 `finish_turn` |
| `prepareRequest` | 无 |
| `AgentTool.outputSchema` / `structuredContent` | 无（只有 `name/description/parameters/label/execution_mode`） |
| `SystemMessage` + `sections` | `types.py:318` 注释明确「无 SystemMessage」 |
| `TranscriptContext` / `normalizeContext` | 无 |

且 `harness/` 这个名字**在上游已指向完全不同的东西**，必须改名（建议 `sessions`），
否则后续读者会持续误判。

---

## 4. 跟踪机制修订提案

### 4.1 旧规则为什么失效（有数据）

`SYNC.md` 现规则：「仅在上游发布 `0.x.0` minor 时集中同步，不追 patch」。三个前提：

| 前提 | 现状 | 证据 |
|---|---|---|
| 上游存在 `0.x.0` 节奏 | ❌ 已进入 1.x（v1.3.0） | — |
| minor 之间通常不引入破坏性变更 | ❌ **被上游自己的发布记录证伪** | v1.0.3 是 **patch 却带 Breaking**（Azure provider 改名，v1.0.3）；v1.1.0/v1.3.0 是 minor 却 **0 条 Breaking** |
| Breaking 段是可信信号 | ❌ **v0.99.0 与 v1.0.0 的 Breaking 段为 0**，但前者换了构建体系与主题体系、首次引入 MCP/codemode | 报告 03 §8.2 |
| 追 patch 不现实 | ✅ **仍然成立且更强**：1.x 实测 **4.00 release/周**（0.x 尾段 2.21） | 报告 03 §8.1 |

**即：版本号已经不能作为触发信号，必须换成「变更内容」触发。**

### 4.2 新机制：changelog-driven

上游**每个包都有 `CHANGELOG.md`**，按 `## [版本] - 日期` 分节，节内含 `### Breaking Changes` 子节。
这比 GitHub Release Notes 可靠得多——后者以 TUI/coding-agent 为主，SDK 变更会被淹没。

Lead 已写好并**跑通**原型：`recon/upstream_scan.py`

```
$ python3 recon/upstream_scan.py
上游基线 (UPSTREAM_VERSION) = 0.85.1
⚠ pi-ai            → ai      15 个版本,  8 条 Breaking
⚠ pi-agent-core    → agent   15 个版本,  2 条 Breaking
⚠ pi-storage-sqlite→ durable  8 个版本, 14 条 Breaking
⚠ pi-coding-agent  → coding-agent 15 个版本, 13 条 Breaking
⚠ (未建包) mcp      → mcp     11 个版本,  2 条 Breaking
上游已移除、但 pi-py 仍有包：
    ✗ pi-server
合计待处理 Breaking 条目: 41
```

它做三件事：**只报增量、按包分组、显式暴露「无对应上游」的包**。

### 4.3 建议的节奏规则（替换 SYNC.md 第 7–18 行）

| 项 | 旧 | 新（提案） |
|---|---|---|
| 触发 | 上游发 `0.x.0` | **固定节拍（建议 2 周）+ 扫描器命中 Breaking 时提前** |
| 依据 | 手动 `gh api compare` 看 commit | **各包 `CHANGELOG.md`**（脚本化） |
| 基线记录 | 只记版本号 | **版本号 + 上游 commit sha**（0.87.1→0.99.0 的跳号证明单靠版本号无法判断间距：那段只有 87 个 commit，而 0.85.1→1.3.0 有 676 个） |
| 完成判据 | 「跑测试」 | **`ci.yml` 的 4 条命令 + integration（DeepSeek 通道）全绿** |

### 4.4 附带发现：发布流水线有欠账

**v0.85.1 从未发到 PyPI**——tag 已推但没建 GitHub Release → `publish.yml` 从未触发。
PyPI 五个包 latest 仍是 **0.84.1**（2026-08-18）。
追赶完成后需要补发，且建议在 CI 加一道「tag 与 Release 不一致」的检查。

---

## 5. 建议的执行顺序

**第 0 步（先做，不写代码）**：修正文档失真，否则排期必错
- 修 `pi-coding-agent/PORTING.md` 的裁剪表（`modes/rpc`/`extensions`/`utils` 标记为「未复刻」）
- 修 `README.md` 同步状态：把「对齐 v0.85.1」改成「基于 v0.85.1 的精选子集，覆盖 X/Y/Z」
- 修 `pi-storage-sqlite/PORTING.md` 的上游包名（现在的名字是错的）
- `UPSTREAM_VERSION` 增加上游 commit sha

**第 1 批（低风险、高收益）**：报告 01 的 S 级 11 项（行为/常量对齐）
**第 2 批**：报告 01 的 M 级 5 项 + `TranscriptContext`
**第 3 批**：报告 04 的 Tier-A 9 项（约 1–2 人周）
**第 4 批（单独立项）**：报告 01 的 L 级 1 项 —— Anthropic strict 降级重试，
它与 pi-py `PORTING.md:43`「provider 不抛异常、重试是外层关注点」的既定原则**直接冲突**，需先定架构方向

**验收用例来源**：上游本轮新增约 40 个测试文件，其中 7 个与 MUST-PORT 项一一对应
（`json-parse` / `event-stream` / `transform-messages-cross-model-thinking` /
`system-message-replay` / `anthropic-strict-tool-*` / `message-types` / `model-types`），
**读测试比读 release notes 精确得多**。

---

## 6. 开放问题（侦察报告提出，需要你回答）

1. **pi-ai 是否补 `Models` / `Provider` 门面？** —— 报告 01 判定这是**排期关键路径**：
   pi-py 现有 `models.py:48-100` 是扁平注册表，上游全部新能力都挂在 `Provider` 接口上，
   不先补这层，后面每项新能力都要重写接线。
2. **Anthropic strict 降级重试放哪层？**（D4 第 4 批的架构前提）
3. **模型目录增量缺失**：`providers/data/*.json` 不在已下载的源码树里，
   「新增/改价了哪些具体模型」尚**无法给出结论**，需要补拉。
4. **`pi-coding-agent` 是否要跟上游把 `compaction`/`skills`/`messages` 挪进 coding-agent？**
   上游已把它们从 agent-core 挪走，pi-py 目前仍在 `pi-agent-core`。

---

## 7. 决策记录（2026-10-18 与用户确认，最终版）

| # | 决策项 | 用户选择 | Lead 原建议 | 结论 |
|---|---|---|---|---|
| D0 | **定位** | **忠实镜像上游** | 精选子集 SDK | 用户定：镜像 |
| D1 | `pi-durable` | **纳入**（读法 B 后追加） | 不做 | 用户定：做 |
| D2 | `pi-mcp` / `pi-codemode` | **纳入**（读法 B 后追加） | 不纳入 | 用户定：做 |
| D3 | `pi-server` | 下线 + 补发 PyPI | 下线 | ✅ 一致 |
| D4 | `pi-agent-core` API 对齐 | 纳入 | 纳入（不可回避） | ✅ 一致 |
| D5 | `pi-ai` 行为对齐 S×11 + M×5 | 纳入 | 纳入 | ✅ 一致 |
| D6 | `pi-coding-agent` Tier-A 9 项 | 纳入 | 纳入 | ✅ 一致 |
| D7 | 同步节奏 | 固定节拍 + Breaking 提前 | 同 | ✅ 一致 |
| D8 | `chord`（durable 硬前置） | **先调研再定** | — | 调研中（task-5） |

### 7.1 关于「现在就镜像」的说明

Lead 曾提出 D0 与 D1/D2 存在矛盾（镜像不含 durable 不成立），并建议分阶段。
**用户选择读法 B：本轮即镜像**，并据此把 durable / mcp / codemode 纳入范围。
因此本方案按**完整镜像**编排，规模为**数人月**，不再是原估的 1–2 人周。

必须随方案一起接受的两个已知代价（侦察报告已证）：

1. **`pi-durable` 是孤岛包**：上游 `coding-agent` 对它 **0 引用**，全仓库只有 `env` 依赖它。
   镜像出来后在 pi-py 里同样缺少消费场景，需要自己造或接受它无人调用。
2. **durable 的 API 不稳定**：上游自标 *"The API changes without notice between releases."*，
   v1.2.0 一个 minor 就有 6 条 Breaking。镜像它 = 把高频 break 的债接下来，
   后续每轮同步都要重做 durable 的适配。

### 7.2 目标包拓扑（镜像 v1.3.0）

```
pi-ai             ← @earendil-works/pi-ai
pi-agent-core     ← @earendil-works/pi-agent-core（瘦身为 6 模块核心）
pi-durable        ← @earendil-works/pi-durable       【新，~25k 行】
pi-mcp            ← @earendil-works/pi-mcp           【新】
pi-codemode       ← @earendil-works/pi-codemode      【新】
pi-coding-agent   ← @earendil-works/pi-coding-agent（含 extensions/）
（pi-storage-sqlite 并入 pi-durable；pi-server 下线）

另需：chord 等价物（py 侧无）—— 待 task-5 结论
上游还有 env / telemetry / chord / evals / tui 五个包，是否镜像待定
```

### 7.3 三个前置调研的结论（已完成）

| 调研 | 结论 | 报告 |
|---|---|---|
| **chord** | 包**没被删**（v1.2.0 只是 coding-agent 不再依赖）。宿主/RPC/插件那半边 **8,117 行在 v1.3.0 零消费**；durable 实际只依赖一个 **4,189 行、零外部依赖**的自包含子层。→ **路线 (a) 自建，M–L**；第三方无等价物且负收益 | [05](./05-chord-feasibility.md) |
| **durable** | **24,989 行 / 82 文件**（另 tests 38,793 行、normative spec 5,352 行）。「只做 L0」**不成立**（会丢掉全部 22 个运行时值导出且零消费者）；正确切法是 **「env 岛」3,056 行**。→ **41–58 人周** | [06](./06-durable-breakdown.md) |
| **codemode** | **能，有条件**。推荐 wasmtime(Python) + 上游同一份 `quickjs.wasm`；中断需求可满足（三级）。但需**从零写 600–1,200 行 Python↔wasm C ABI 宿主**，且该 ABI 是 vercel-labs 私有契约、月更无兼容承诺。→ 约 **2,500–3,500 行 Python**，含 Phase-0 冒烟门限 | [07](./07-codemode-feasibility.md) |

### 7.4 ⚠️ 完整镜像的总规模（决策时必须看到）

用户选择「现在就镜像」时，可用数据只有 Lead 的粗估「数人月」。调研完成后，**实际数字显著更大**：

| 工作项 | 估算 | 来源 |
|---|---|---|
| `pi-durable` | **41–58 人周** | 报告 06 §6.6 |
| `pi-chord`（孤岛子集） | ~4–8 人周 | 报告 05 |
| `pi-codemode` | ~6–8 人周 + 1,200 行不确定宿主 | 报告 07 |
| `pi-mcp` | 未调研（估 3–6 人周） | — |
| `pi-ai` 对齐（S×11/M×5/L×1） | ~4–6 人周 | 报告 01 |
| `pi-agent-core` API 对齐 | ~2–4 人周 | 报告 02 |
| `pi-coding-agent` Tier-A | ~1–2 人周 | 报告 04 |
| **合计** | **≈ 61–92 人周 ≈ 15–23 人月** | |

**这是单人维护者 1.5–2 年的工作量**，且不含：
- `pi-coding-agent/src/extensions/` 的 codemode/mcp/tool-search 完整镜像（5,915 行 TS）
- `pi-env`（上游 42 文件 + Rust daemon）—— 但 durable 的「env 岛」批次 A 会解锁它
- 上游仍在以 **4 release/周** 前进（镜像一个移动靶）

**关键路径不是 durable 自己，而是它前面的 chord 与 pi-ai**（报告 06 §6.5）。

### 7.5 若调整范围，建议的最小后悔路径

不打乱已定方向，但建议把顺序调整为「先做不可逆性低、且无论如何都要做的」：

1. **立即开工（无前置、不阻塞）**：批次 A「env 岛」3,056 行 / 6–9 人周 —— 唯一零 chord 依赖的切片，自带 oracle
2. **并行**：pi-ai 行为对齐（S×11/M×5）+ pi-agent-core API 对齐 + coding-agent Tier-A（约 7–12 人周）
3. **决策点**：codemode Phase-0 八条冒烟（S1–S6 全过才立项）
4. **重活**：chord 孤岛 → durable 批次 B → D → E

这样即使中途叫停，第 1–2 步的产出**在任何定位下都不会浪费**。

---

## 8. 侦察产物索引

| 文件 | 内容 | 行数 |
|---|---|---:|
| [`recon/01-upstream-ai-delta.md`](./01-upstream-ai-delta.md) | 上游 pi-ai 增量（+29/−8/102 改），22 条破坏性变更，S×11/M×5/L×1 | 739 |
| [`recon/02-upstream-runtime-restructure.md`](./02-upstream-runtime-restructure.md) | 结构重组真相（删除≠搬家）、新包职责、3 个拓扑方案 | 741 |
| [`recon/03-pi-py-state-and-cadence.md`](./03-pi-py-state-and-cadence.md) | pi-py 家底 + 基线健康度 + 发布节奏分析 | 513 |
| [`recon/04-upstream-coding-agent-delta.md`](./04-upstream-coding-agent-delta.md) | coding-agent core 增量 + 越界判断 + Tier 分级 | 477 |
| [`recon/05-chord-feasibility.md`](./05-chord-feasibility.md) | chord 职责与 Python 侧可行性（宿主半边零消费、孤岛 4,189 行） | 647 |
| [`recon/06-durable-breakdown.md`](./06-durable-breakdown.md) | durable 24,989 行拆分、最小可行集、批次表（§6 已修正） | 778 |
| [`recon/07-codemode-feasibility.md`](./07-codemode-feasibility.md) | codemode 的 JS 沙箱选型（wasmtime + quickjs.wasm） | 411 |
| [`recon/08-chord-delta-spec.md`](./08-chord-delta-spec.md) | delta 子层规格：7 个 op、UTF-16 规则、20 条测试向量 | 581 |
| [`recon/09-codemode-phase0-results.md`](./09-codemode-phase0-results.md) | Phase-0 冒烟 S1–S8 实测结果（14/14 PASS → 立项） | 348 |
| [`scripts/upstream_scan.py`](../scripts/upstream_scan.py) | changelog 驱动的增量扫描器（已落地，按上游包去重） | 215 |

**基线健康度（实测，报告 03 §3）**：`pytest` 262 passed / 16 deselected；`ruff check` 全绿；
`mypy`（README/CI 命令）`Success: no issues found in 36 source files`；
integration 16 条中 11 passed / 5 skipped（本机有 DeepSeek key，具备真实 LLM 回归能力）。

---

## 9. 实施进展（2026-10-18 更新）

### 9.1 已完成

| 工作流 | 内容 | 测试 |
|---|---:|---:|
| `pi-ai` | 15 项 MUST-PORT 行为对齐（M-1…M-11、M-13、M-17、M-21、M-22）+ 5 个新模块 | 113 → **270** |
| `pi-agent-core` | `finishTurn`、`prepareRequest`、`AgentTool.outputSchema`、`systemPrompt` 只读、`harness/`→`sessions/` | 99 → **102** |
| `pi-chord` | **整个 chord 孤岛**：`context` + `json` + `delta`（ops/apply/textdiff/**tracker**）+ `state` + `output` | **292** |
| `pi-durable` | 批次 A（env 岛）+ C₁ + C₂（1,325 行工具层）+ D-1（harness define 层） | **416** |
| `pi-codemode` | 骨架：wasmtime + `quickjs.wasm` 宿主（wasm 入库 + sha256 断言 + wheel 自包含验证） | **37** |
| `pi-coding-agent` | Tier-A 9 项 + PORTING 裁剪表纠偏 | 33 → **53** |
| `pi-storage-sqlite` | 改名下游适配 | **18** |
| `pi-server` | **删除**（上游 v1.2.0 已移除该包） | — |
| 发布加固 | `release-guard.yml`（实测命中 `tag v0.85.1 has no GitHub Release`）、`publish.yml` 加固 | — |
| 跟踪机制 | [`SYNC.md`](../SYNC.md) 新规则 + [`scripts/upstream_scan.py`](../scripts/upstream_scan.py) | — |

**全仓：262 → 1188 passed**（`ruff check` / `ruff format --check` / `mypy` 全绿）。
**源码量：8,926 → 23,096 行**，包数 5 → 7（去掉 `pi-server`，新增 `pi-chord`/`pi-durable`/`pi-codemode`）。

### 9.2 批次顺序修正（重要）

`recon/06` 初版把批次 C 整体排在 D 之前，**是排期错误**。真实依赖图是 C 被 D 劈成两半：

```
A（env 岛）──┬─→ C₁（709 行，无 harness）           ✅ 已交付
             └─→ D-1（define 层）──→ C₂（1,325 行）  ✅ 已交付
                                      └─→ D（剩余 ~8,100 行）──→ E（codemode 集成）
```

修正后的批次表：**`A → C₁ → D-1 → C₂ → B → D → E`**。

### 9.3 剩余

| 项 | 规模 | 状态 |
|---|---|---|
| 批次 B（durable 存储内核 + storage-conformance） | 7,972 行 | 进行中（chord 前置已就绪） |
| `pi-mcp` 核心面 | 3,277 行 | 进行中（OAuth 延后为第二期） |
| 批次 D 剩余（harness 主体） | ~8,100 行 | 未开始（`scheduler.ts` 1,953 行居首） |
| 批次 E（codemode 完整声明层 + 平台适配） | ~2,766 行 | 未开始 |
| `pi-storage-sqlite` 并入 `pi-durable` | — | 待批次 B 完成后执行 |
| PyPI 补发 v0.85.1 + 版本号统一 | — | **用户指示暂缓**（详见 §9.4） |

### 9.4 待用户裁决：版本号方案

现状**不一致**（是 bug，不只是风格问题）：

| 包 | `pyproject.toml` | `__version__` |
|---|---|---|
| `pi-ai` | 0.85.1 | **1.3.0** |
| `pi-agent-core` | 0.85.1 | **1.3.0** |
| `pi-chord` / `pi-durable` / `pi-codemode` | 1.3.0 | 1.3.0 |
| `pi-coding-agent` / `pi-storage-sqlite` | 0.85.1 | 0.85.1 |

依赖上界也随之失效（`pi-coding-agent` 声明 `pi-py-ai>=0.85.1,<0.86`）。

根因是 pi-py 沿用了「**包版本号 = 对齐到的上游版本**」这一约定，而**所有包都只是上游的子集**，该约定在任何包只完成部分对齐时都会失真。

候选方案：
- **A. 各自对齐**：`pyproject` 跟 `__version__`，依赖上界统一放开 —— 保留「版本号即对齐点」的语义
- **B. 自有 lockstep 版本线**：pi-py 用独立版本号（如 `0.86.0`），上游对齐点记在 `__upstream_ref__` 与 README —— 与上游自身做法一致（v1.3.0 CHANGELOG：「Pinned internal Pi dependencies to exact release versions」），且不会虚假声称对齐度
- **C. 全部升 1.3.0** —— 最省事，但 `pi-coding-agent` 仅覆盖上游 core 的 ~4.5%，声称 1.3.0 会严重高估

> 注意：`pi-ai/tests/test_smoke.py` **硬断言** `__version__ == "1.3.0"`，改版本号时须同步。

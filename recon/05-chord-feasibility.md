# 调研 E：chord 职责与 Python 侧可行性（pi-durable 硬前置）

> 任务：`task-5` ｜ 调研员：`chord-scout` ｜ 日期：2026-10-18
> 上游源码（只读）：`/tmp/pi-up/v1.3.0/`、`/tmp/pi-up/v0.85.1/`
> 本报告所有结论均附 `文件:行号` 或可复现命令。**凡未实读的一律标注「未验证」。**

---

## 0. 结论摘要（TL;DR）

1. **`@earendil-works/chord@1.3.0` 仍然存在，没有被删除。** v1.2.0 那句「Removed the dependency on `@earendil-works/chord`」出现在 **`packages/coding-agent/CHANGELOG.md:110`** —— 是 **coding-agent 不再依赖它**，不是包被删。coding-agent 已改挂 `pi-agent-core` / `pi-ai` / `pi-codemode` / `pi-mcp` / `pi-tui`（`packages/coding-agent/package.json` dependencies）。

2. **报告 02 的描述需要重大纠正。** 「不可变 JSON 状态 + delta 追踪 + 观察者 + 事务」只覆盖了 chord **约 31% 的代码**。chord 的真实自我定位是 `"Typed host runtime for plugins, contracts, replicated state, and RPC over JSON links"`（`packages/chord/package.json:4`）—— 一个**跨进程对象/契约/RPC/热重载宿主运行时**。delta/state 只是它的一个子模块。

3. **但纠正之后，报告 02 对 §8.2 的路线判断反而是成立的**，因为有一个关键的结构性事实：

   > **chord 的「宿主/RPC/插件/热重载」那一半（8,117 行）在 v1.3.0 全仓库零消费。**
   > `createHost` / `defineFacet` / `defineRegistry` / `ipcLink` / `webSocketLink` / `memoryLinkPair` / `acceptWebSockets` / `hostNamed` / `local(` / `method(` 在 `packages/` 下（除 chord 自身）**使用数均为 0**。

   chord 实际只被 2 个包消费：`durable`（164 处 import）与 `env`（8 处，且只用到 `Context` 一个类型）。

4. **durable 真正依赖的是 chord 的一个自包含孤岛**：`delta/` + `context/` + `json.ts` + `output.ts` + `state.ts` = **4,189 行**，且**零外部 import**（不依赖 typebox，不触碰 host/link/reload）。这是本报告最重要的可行性依据。

5. **推荐路线：(a) 自建，但严格限定在孤岛子集**（约 4.2k 行 TS → Python），明确**不移植**宿主那一半。理由见 §5。**(b) 简化掉**是可行的降级方案，但它不是「删代码」而是「重构 durable 的存储与发布层」，且与用户「忠实镜像上游」的决定相冲突；**(c) 引第三方**没有等价物，会把问题从「写 tracker」变成「写 tracker + 适配数据模型」，见 §4.3。

---

## 1. 包概况

### 1.1 基本信息

| 项 | 值 | 证据 |
|---|---|---|
| 包名 | `@earendil-works/chord` | `packages/chord/package.json:2` |
| 版本 | `1.3.0` | `packages/chord/package.json:3` |
| 许可 | **MIT** | `packages/chord/package.json` `"license": "MIT"` |
| 描述 | `Typed host runtime for plugins, contracts, replicated state, and RPC over JSON links` | `packages/chord/package.json:4` |
| 运行时 | `"type": "module"`，`engines.node >= 22.19.0` | `packages/chord/package.json` |
| 运行时依赖 | **仅 1 个：`typebox@1.3.27`** | `packages/chord/package.json` `dependencies` |
| `sideEffects` | `false` | 同上 |

> 注：`typebox` 只被宿主半边使用。§1.4 会证明 durable 所需子集**完全不碰 typebox**。

### 1.2 代码规模

```bash
find packages/chord/src -name "*.ts" | wc -l      # 31
find packages/chord/src -name "*.ts" | xargs wc -l | tail -1   # 13553 total
find packages/chord/test -type f | wc -l          # 56
find packages/chord/test -name "*.ts" | xargs wc -l | tail -1  # 16334 total
```

- **src：31 个 `.ts`，13,553 行**（另有 `src/delta/README.md` 193 行设计文档）
- **test：56 个文件，16,334 行**（测试量 > src 量，说明该包是重度工程化的）

按功能切分（三者相加 13,471 + `index.ts` 82 = 13,553 ✓）：

| 分区 | 行数 | 内容 |
|---|---|---|
| **宿主半边** | **8,117** | `host.ts`(1303) `object.ts`(1932) `connection.ts`(1327) `reload.ts`(723) `remote.ts`(658) `link.ts`(415) `contract.ts`(440) `manager.ts`(443) `cluster.ts`(251) `schema.ts`(219) `wire.ts`(181) `loader.ts`(67) `publish.ts`(39) `declare.ts`(38) `registry.ts`(30) `websocket.ts`(35) `node.ts`(16) |
| **durable 所需孤岛** | **4,189** | `delta/tracker.ts`(2239) `delta/index.ts`(738) `delta/apply-immutable-trusted.ts`(128) `delta/draft.ts`(10) `state.ts`(423) `output.ts`(383) `json.ts`(135) `context/index.ts`(133) |
| **durable 不需要** | **1,165** | `delta/diff.ts`(523) `cell.ts`(402) `types.ts`(89) `errors.ts`(76) `delta/revision-validator.ts`(75) |

（末行 `errors.ts` 虽小，但 durable 未 import 任何 `ChordError`/`isChordError`，故归入不需要；命令见 §3.2。）

### 1.3 公开入口（实读，非推测）

`package.json` 的 `exports` 定义了 5 个入口：

| 子路径 | source | 内容（取自 `README.md` 入口表） |
|---|---|---|
| `.` | `./src/index.ts` | 契约、宿主、链接、状态、错误、JSON 基元 |
| `./context` | `./src/context/index.ts` | 显式取消与调用值 |
| `./output` | `./src/output.ts` | 有界输出流 + 节流进度 |
| `./delta` | `./src/delta/index.ts` | 事务性 JSON tracker、结构化 diff、不可变 apply、wire 编解码 |
| `./node` | `./src/node.ts` | Node 子宿主进程（`createHostManager`/`forkDriver`/`childControl`/`processLock`）；**注意：`node.ts` 不在根 `index.ts` 的导出里** |

#### 根入口 `src/index.ts` 逐符号清单（82 行全文实读）

**值导出（运行时实体，25 个，按来源模块分组）：**

- `contract.ts`：`bind`、`coverage`、`hostFamily`、`hostNamed`、`local`、`method`、`opaque`、`remote`（`index.ts:4-26`）
- `errors.ts`：`ChordError`、`forbidden`、`invalid`、`isChordError`（`index.ts:29`）
- `host.ts`：`createHost`、`defineFacet`（`index.ts:31-43`）
- `json.ts`：`copyJson`、`isJsonValue`（`index.ts:44`）
- `link.ts`：`inflateMessage`、`ipcLink`、`memoryLinkPair`、`PACK_THRESHOLD`、`webSocketLink`（`index.ts:45-55`）
- `registry.ts`：`defineRegistry`（`index.ts:67`）
- `state.ts`：`replicatedState`（`index.ts:69-79`）
- `websocket.ts`：`acceptWebSockets`（`index.ts:81`）
- `wire.ts`：`PROTOCOL_VERSION`（`index.ts:82`）

**类型导出（约 77 个，按来源模块分组）：**

- `cell.ts`：`Cell`（`:1`）
- `connection.ts`：`AcceptOptions`、`LinkOptions`（`:2`）
- `context/index.ts`：`Context`、`ContextKey`（`:3`）
- `contract.ts`：`Accepts`、`Adapter`、`AnyContract`、`Description`、`HostFamily`、`HostNamed`、`ImplementationOf`、`InputOf`、`LocalContract`、`Placement`、`PlacementFor`、`Ref`、`RemoteContract`（`:4-26`）
- `declare.ts`：`EventOf`、`Resource`、`ResourceDeclaration`、`Subscribable`、`ValueOf`（`:27`）
- `delta/index.ts`：`Draft`（`:28`）
- `errors.ts`：`ChordErrorCode`（`:29`）
- `host.ts`：`ConnectOptions`、`PrincipalMatch`、`PrincipalPattern`、`Route`、`Facet`、`FacetApi`、`FacetDefinition`、`FacetLoader`、`Host`、`HostOptions`、`LoadOptions`、`PluginRef`、`RootOptions`（`:30-43`）
- `json.ts`：`CopyJsonOptions`、`JsonRepresentation`、`JsonValue`（`:44`）
- `link.ts`：`IpcChannel`、`Link`、`WebSocketLike`、`WebSocketLinkOptions`（`:45-55`）
- `object.ts`：`Factory`、`HostServeOptions`、`Lifetime`、`ReferencedServeOptions`、`Self`、`ServeOptions`、`ServeOptionsFor`、`StateInit`、`StateOptions`（`:56-66`）
- `registry.ts`：`Registry`、`RegistryTable`、`RegistryView`（`:67`）
- `reload.ts`：`ReloadOptions`、`ReloadReport`（`:68`）
- `state.ts`：`AttachedReplicatedState`、`MutableReplicatedState`、`ReplicatedState`、`ReplicatedStateDelivery`、`ReplicatedStateSource`、`ReplicatedStateSourceAttachment`、`ReplicatedStateSourceFrame`、`ReplicatedStateSourceOptions`（`:69-79`）
- `types.ts`：`Ctx`、`GetOptions`、`Readable`、`RefStatus`、`Shape`、`StateEvent`（`:80`）
- `websocket.ts`：`WebSocketAcceptOptions`、`WebSocketServerLike`（`:81`）
- `wire.ts`：`Frame`（`:82`）

**根入口没有 default 导出。**

#### 子入口导出（对 durable 关键的两个）

- **`chord/context`**（`src/context/index.ts`，133 行，**全文实读**）：`Context`、`ContextKey`、`BACKGROUND_CONTEXT`、`TODO_CONTEXT`、`createContextKey`、`withContextValue`、`withAbortSignal`、`withoutAbortSignal`、`withCancel`、`awaitWithContext`。**零 import。**
- **`chord/delta`**（`src/delta/index.ts` 导出面，行号实读）：类型 `JsonValue`/`Seg`/`Path`/`NonEmptyPath`/`PathRef`/`Op`/`WireOp`/`Draft`，值 `isReplace`(70) `isBase`(76) `overlap`(87) `diffRevisions`(114) `track`(116) `RESERVED_SEGMENTS`(131) `UnsafePathError`(133) `assertValidOp`(152) `assertValidWireOp`(211) `assertSafePath`(279) `PathError`(310) `apply`(326) `applyImmutable`(409) `applyImmutableBatches`(419) `encoder`(566) `decoder`(666)。

### 1.4 v1.2.0「移除依赖」的真相 —— 包没有删

```bash
grep -rn -i "removed the dependency" /tmp/pi-up/v1.3.0 --include=*.md
# /tmp/pi-up/v1.3.0/packages/coding-agent/CHANGELOG.md:110:- Removed the dependency on `@earendil-works/chord`
```

**判定：只是 coding-agent 不再依赖，包本身健康存活。** 三条独立证据：

1. 该行位于 **coding-agent 自己的 CHANGELOG**，不在 chord 的 CHANGELOG 里。
2. `packages/chord/package.json:3` 版本为 `1.3.0`，且 `packages/chord/CHANGELOG.md:3` 有 `## [1.3.0] - 2026-10-15` 条目（该版本还发布了大量 breaking changes 与新增 `@earendil-works/chord/output`）—— **1.3.0 是活跃维护中的版本，不是废弃版本**。
3. 依赖方确实减少了：v0.85.1 有 6 个消费者（`agent`/`client`/`coding-agent`/`protocol`/`server` + 自身），v1.3.0 只剩 2 个（`durable`/`env`）。coding-agent 改挂 `pi-agent-core`+`pi-ai`+`pi-codemode`+`pi-mcp`+`pi-tui`（`packages/coding-agent/package.json` dependencies）。

**对结论的影响**：无。chord 必须按「存活的上游包」对待。

### 1.5 与 v0.85.1 的对照

| | v0.85.1 | v1.3.0 |
|---|---|---|
| src 规模 | 5,822 行 | 13,553 行（**2.33×**） |
| 描述 | `Application composition runtime for services, replicated state, RPC, and plugins` | `Typed host runtime for plugins, contracts, replicated state, and RPC over JSON links` |
| delta 形态 | 单文件 `src/delta/index.ts`，1,267 行 | 目录 `src/delta/`，合计 3,713 行（**2.93×**） |
| context | `src/context/` 121 行 | `src/context/index.ts` 133 行（基本未变） |
| 结构 | `api.ts` `bundler.ts` `facets/` `services/` `node/` | 扁平化为 `host.ts` `object.ts` `connection.ts` `reload.ts` … |
| 消费者 | `agent` `client` `coding-agent` `protocol` `server` | `durable` `env` |
| `durable` 包 | **不存在**（当时是 `client`/`protocol`/`server`/`session-backends`） | 存在，且是 chord 头号消费者 |

**趋势判断**：chord 整体在「宿主半边不断加厚」，而 delta 子层在 v1.x 被大幅重写（从 1,267 → 3,713 行，拆出 `diff.ts`/`tracker.ts`/`apply-immutable-trusted.ts` 等），**并新增了 `output` 子入口**（`packages/chord/CHANGELOG.md` v1.3.0 「Added」节：「`@earendil-works/chord/output` … **Moved from `@earendil-works/pi-durable`, which now uses them from here.**」）。

> ⚠️ 这条对 pi-py 很重要：`output.ts`（383 行）**本来就是 durable 的代码**，v1.2.0→v1.3.0 才搬到 chord。所以 pi-py 侧它不是「chord 的额外负担」，而是 durable 自己的组成部分。

---

## 2. 职责说明：报告 02 描述的验证与纠正

### 2.1 报告 02 的原描述

> 侦察报告 02 §8.2 / 行 540：「pi-py 还需要一个 `chord` 等价物（不可变 JSON 状态 + delta 追踪 + 观察者 + 事务）」

### 2.2 基于源码的纠正

**该描述对「durable 所消费的子集」基本正确，对「chord 这个包」严重不完整。**

chord 的真实职责（`README.md:3-11` 实读）是：

> Chord builds applications out of **objects** that live in several processes (a server, workers, browser tabs) and can be **hot reloaded** while they run.

即四大块：

| # | 职责 | 代表 API | 行数 | durable 是否使用 |
|---|---|---|---|---|
| 1 | **跨进程对象 + 契约 + RPC + 热重载 + 插件** | `createHost` `defineFacet` `remote` `local` `method` `ipcLink` `webSocketLink` `defineRegistry` | 8,117 | ❌ **完全不用** |
| 2 | **事务性不可变 JSON 追踪（delta）** | `track` `beginChange` `prepare` `adopt` `apply` `applyImmutable` `Op` | 3,115 | ✅ 核心依赖 |
| 3 | **可观察的复制状态** | `replicatedState` `ReplicatedState` `AttachedReplicatedState` `ReplicatedStateSource` | 423 | ✅ 核心依赖 |
| 4 | **调用上下文 / 有界输出 / JSON 基元** | `Context` `withAbortSignal` `awaitWithContext` `OutputBuffer` `Progress` `copyJson` `JsonValue` | 651 | ✅ 核心依赖 |

所以更准确的表述是：

> **chord = 一个跨进程对象宿主运行时；其中「事务性 JSON delta 追踪 + 复制状态 + 调用上下文 + 有界输出」这一自包含子层，才是 pi-durable 需要的部分。**

### 2.3 delta 子层的精确语义（`src/delta/README.md` 实读 + `tracker.ts` API 实读）

`tracker.ts` 的公开契约只有 4 个类型 + 5 个方法（`delta/tracker.ts:121-141`）：

```ts
interface Tracker<T> { readonly value: T; readonly revision: number;
  beginChange(): Change<T>; prepareReplace(value: T): Prepared<T>; adopt(prepared: Prepared<T>): void; }
interface Change<T>  { readonly state: Draft<T>; prepare(): Prepared<T>; abort(): void; }
interface Prepared<T>{ readonly base: T; readonly value: T; readonly ops: readonly Op[];
  readonly baseRevision: number; abort(): void; }
```

语义要点（**全部来自 `delta/README.md` 实读**，非推测）：

- **不可变性是所有权契约，不是强制**：`README.md:6-7` 明确写「Nothing is frozen or defensively copied, so an illegal mutation is not detected. **It silently corrupts state.**」—— JS 侧靠约定 + WeakMap/WeakRef 管理，不靠 `Object.freeze`。
- **生命周期**：`beginChange()` 在当前 revision 上开 overlay draft，**绝不修改 base**；draft 可跨 `await` 存活；`prepare()` 物化候选与 ops 但**不改变权威**；`adopt()` 校验并**指针交换**（`README.md:124-136`）。
- **并发竞争**：同一 revision 上可有多个 open/prepared change，**adopt 其中一个会让其余全部 stale**——open draft 失效、`prepare()` 抛错、prepared 竞争者被 `adopt()` 拒绝（`README.md:131-136`）。
- **op 是「精确但非规范」的**：`README.md:167-168`「Batches are exact but not canonical.」同一变更可用不同 tuple，大编辑集可能折叠为区域 splice、祖先 `s` 或根 `r`。**只有结果值有契约意义**（`README.md:94`）。
- **op 词表 7 种**（`README.md:157-165`）：`["r",value]` 整体替换、`["s",path,value]` 置位、`["d",path]` 删除、`["a",path,text]` 字符串追加、`["t",path,count]` 从字符串**前端**删除 `count` 个 **UTF-16 code unit**、`["p",path,index,remove,items]` 数组 splice、`["m",path,permutation]` 数组重排。
- **结构共享**：`README.md:188` 提到「A change inside a large flat array copies that array's pointer storage for the new revision」；未变更子树在 revision 间共享。
- **折叠阈值**（`delta/tracker.ts:112-113`）：`MAX_DELTA_OPERATIONS = 4_096`、`MAX_SIMPLE_OBJECT_NODES = 128`。
- **原型污染防护**（`README.md:170-177`）：tracker 绝不把 `__proto__`/`constructor`/`prototype` 作为 path 段；`apply` 系列遇之抛 `UnsafePathError`。

**给 pi-py 的直接结论**：这是一个**接口面极小（5 方法）、但实现极深（2,239 行 tracker）**的模块。

---

## 3. 真实使用方式：durable 到底怎么用 chord

### 3.1 消费方全貌

```bash
grep -rn "@earendil-works/chord" /tmp/pi-up/v1.3.0/packages/*/package.json
# packages/chord/package.json:2    (自身)
# packages/durable/package.json:146  "@earendil-works/chord": "1.3.0"
# packages/env/package.json:48       "@earendil-works/chord": "1.3.0"
```

`env` 只用 `Context`（`packages/env/src/list-files.ts:1`、`remote-env.ts:5`、`watch.ts:1`），可忽略。

**durable：164 处 import，跨 60 个 src 文件 + 大量 test。**

### 3.2 符号级依赖清单（脚本统计，见附录 A）

durable+env 从 chord 导入的**全部 35 个符号**，按次数降序：

```
 64  BACKGROUND_CONTEXT      57  Context            37  JsonValue
 16  Op                      13  Draft              10  copyJson
  8  withAbortSignal          5  applyImmutable      5  AttachedReplicatedState
  5  JsonRepresentation       5  withoutAbortSignal  3  awaitWithContext
  3  replicatedState          2  withCancel          2  characterEnd
  2  Path                     2  track               1  createContextKey
  1  withContextValue         1  utf8ByteLength      1  applyImmutableBatches
  1  overlap                   1  boundOutput         1  OutputBuffer
  1  OutputLimits             1  PROGRESS_BYTES_PER_SECOND  1  Progress
  1  NonEmptyPath             1  Change              1  Prepared
  1  Tracker                   1  ReplicatedStateSource
  1  ReplicatedStateSourceAttachment  1  ReplicatedStateSourceFrame
  1  apply
```

**35 个符号中，属于宿主半边的：0 个。** 没有任何 `createHost`/`defineFacet`/`remote`/`local`/`method`/`ipcLink`/`webSocketLink`。

子路径分布：chord 根入口 76 次、`chord/context` 72 次、`chord/delta` 18 次、`chord/output` 4 次。

### 3.3 关键调用点（`文件:行号`）

#### (A) 事务 / 文档状态 —— tracker 的**主战场**

`packages/durable/src/session/transaction.ts`（1,043 行，全文实读）：

| 行号 | 调用 | 作用 |
|---|---|---|
| `:1` | `import { type Context, copyJson, type Draft, type JsonValue } from "@earendil-works/chord"` | — |
| `:2` | `import { type Change, type Op, type Prepared, type Tracker, track } from "@earendil-works/chord/delta"` | 全部 delta 类型 |
| `:95` | `readonly tracker: Tracker<JsonObject>` | `LoadedDocument` 持有 tracker |
| `:160-161` | `change?: Change<JsonObject>; prepared?: Prepared<JsonObject>` | 事务内暂存 change/prepared |
| `:598-599` | `entry.change = loaded.tracker.beginChange(); return entry.change.state;` | **读文档 → 开 draft** |
| `:614` | `const tracker = track(value)` | 新建文档建 tracker |
| `:621-622` | `entry.change = tracker.beginChange(); return entry.change.state;` | 同上 |
| `:685` | `document.prepared = document.change.prepare()` | **提交时统一 prepare** |
| `:689` | `this.#abortChanges()` | 失败时统一 abort |
| `:710` | `if (loaded === undefined ? !plan.retire : prepared.ops.length > 0) tracker.adopt(prepared)` | **按 ops 是否为空决定 adopt** |
| `:711` | `else prepared.abort()` | 空 ops 则丢弃 |
| `:742` | `const ops = change.loaded === undefined ? EMPTY_OPERATIONS : change.prepared.ops` | 发布时取 ops |
| `:840-842` | `change.definition?.checkpointWhen?.(prepared.value, prepared.ops, info)` | **决定 delta 是否折叠为 base** |

**`prepared.ops` 是 durable 的持久化格式**（`transaction.ts:1019-1024`）：

```ts
const content =
  loaded.storedVersion < version
    ? ({ type: "document.change", id, content: { version, kind: "base",  value: prepared.value } })
    : prepared.ops.length > 0
      ? ({ type: "document.change", id, content: { version, kind: "delta", ops: prepared.ops } })
      : undefined;
```

即 **base + delta 链的 checkpoint 存储模型**。`checkpointWhen` 谓词与 `deltasSinceBase` 计数器（`types.ts:77-78`、`:728`）用于把 delta 链折叠回 base。

#### (B) 存储后端 —— ops 的**回放**

| 文件:行号 | 调用 |
|---|---|
| `storage/sqlite/storage.ts:2` | `import { apply, type Op } from "@earendil-works/chord/delta"` |
| `storage/sqlite/storage.ts:654` | `value = apply(value, parseJson<readonly Op[]>(revision.content))` —— 从最近的 `kind='base'` 行起，按 seq 顺序回放所有 `kind='delta'` 行（SQL 见 `:636-656`） |
| `storage/memory.ts:2` | `import { applyImmutableBatches, type Op }` |
| `storage/memory.ts:57` | `Generator<readonly Op[]>` —— 内存后端按批次回放 |
| `storage/jsonl/storage.ts:1` | 只用到 `Context`（JSONL 后端不做 delta 回放） |

#### (C) 观察者 / 视图 —— `replicatedState`

| 文件:行号 | 调用 |
|---|---|
| `harness/harness.ts:1` | `AttachedReplicatedState, Context, JsonValue` |
| `harness/harness.ts:2` | `withAbortSignal, withoutAbortSignal`（`chord/context`） |
| `harness/view.ts:1` | `AttachedReplicatedState, Context, JsonValue, replicatedState` |
| `harness/view.ts:3` | `applyImmutable, NonEmptyPath, Op, Path` |
| `harness/view.ts:118` | `return replicatedState(observer, { onError: ... })` |
| `harness/view.ts:237` | `const value = docOps.length === 0 ? mount.value : applyImmutable(mount.value, docOps)` |
| `harness/view.ts:249-266` | `prefixed(op, prefix)` —— **把文档级 op 提升为视图级 op**，逐 op 类型重写 path |
| `session/observation.ts:9` | `import type { Op }` |
| `session/observation.ts:19` | `export const RETIREMENT_OPERATIONS: readonly Op[] = [["r", null]]` |
| `session/observation.ts:25` | `class CommittedStateSource<T> implements ReplicatedStateSource<T>` |
| `session/observation.ts:226` | `advance(value: T, ops: readonly Op[], context: Context)` |
| `harness/task-graph.ts:1,3` | `replicatedState` + `applyImmutable, type Op` |
| `session/session.ts:1,3` | `replicatedState` + `type Op, track` |
| `harness/live.ts:1` | `Draft, JsonRepresentation, JsonValue` |

#### (D) 上下文 / 取消 —— 最高频依赖

`BACKGROUND_CONTEXT` 被 import 64 次，`Context` 57 次。典型：

| 文件:行号 | 用法 |
|---|---|
| `session/session.ts:2` | `awaitWithContext, BACKGROUND_CONTEXT, withoutAbortSignal` |
| `harness/scheduler.ts:2` | `awaitWithContext, withAbortSignal` |
| `harness/task-graph.ts:2` | `withoutAbortSignal`（在关闭超时路径屏蔽取消） |
| `harness/tool.ts:2` | `awaitWithContext` |
| `testing/storage-conformance.ts:2` | `BACKGROUND_CONTEXT` |

**这是 durable 所有异步 API 的取消语义载体**（每个 `Context` 参数都来自 chord）。

#### (E) 输出 / 截断 —— `chord/output`

| 文件:行号 | 用法 |
|---|---|
| `harness/tool.ts:4` | `boundOutput, OutputBuffer, OutputLimits, PROGRESS_BYTES_PER_SECOND, Progress` |
| `tools/read.ts:2` | `characterEnd` |
| `truncate.ts:1` | `utf8ByteLength` |
| `harness/tool.ts:3` | `overlap`（字符串拼接去重，用于流式输出合并） |

### 3.4 必需 vs 可选：能力分类

| 能力 | durable 依赖程度 | 判定 |
|---|---|---|
| **`Context` 抽象 + 取消派生**（`withAbortSignal`/`withoutAbortSignal`/`withCancel`/`awaitWithContext`/`BACKGROUND_CONTEXT`/`createContextKey`/`withContextValue`） | 121 次符号引用，遍布每个异步签名 | **必需，且是最高频依赖** |
| **`track`/`beginChange`/`prepare`/`adopt`/`Change`/`Prepared`/`Tracker`/`Draft`** | `session/transaction.ts` 整个提交路径 | **必需** |
| **`Op` + `apply`/`applyImmutable`/`applyImmutableBatches`** | 3 个存储后端 + view 组合 | **必需**（作为持久化格式） |
| **`replicatedState` + `ReplicatedStateSource*`** | `view.ts`/`observation.ts`/`task-graph.ts`/`session.ts`/`harness.ts` | **必需** |
| **`JsonValue`/`JsonRepresentation`/`copyJson`/`isJsonValue`** | 52 次引用 | **必需**（类型 + 深拷贝语义） |
| **`OutputBuffer`/`Progress`/`boundOutput`/`characterEnd`/`utf8ByteLength`/`overlap`** | 工具层输出截断 | **必需**（但只是小工具，与状态模型无关） |
| `diffRevisions` | **0 次** | **不需要**（`delta/diff.ts` 523 行可丢） |
| `encoder`/`decoder`（wire 编解码） | **0 次** | **不需要**（单进程无需 wire） |
| `revision-validator.ts` / `assertValidOp` / `assertSafePath` | 未直接 import（`revision-validator.ts` 在 chord 内亦无 importer，疑似死代码） | **不需要**（tracker 内部已用） |
| 宿主半边全部 | **0 次** | **不需要** |

### 3.5 重大发现：chord 一半代码无人消费

```bash
cd /tmp/pi-up/v1.3.0
grep -rn "@earendil-works/chord" packages/*/package.json
# 仅 durable + env (+ chord 自身)
```

对 chord 宿主半边的代表 API 做全仓库（排除 chord 自身）计数：

| 符号 | chord 之外使用数 |
|---|---|
| `createHost` | **0** |
| `defineFacet` | **0** |
| `defineRegistry` | **0** |
| `ipcLink` | **0** |
| `webSocketLink` | **0** |
| `memoryLinkPair` | **0** |
| `acceptWebSockets` | **0** |
| `hostNamed` | **0** |
| `local(` | **0** |
| `method(` | **0** |
| `remote(` | 8（经人工核对**全部是误报**：`codemode/test/deterministic.test.ts:23,45,370`、`env/src/ssh.ts:517,521`、`env/test/ssh.test.ts:250,260`、`codemode/src/runtime/remote.ts:231` 都是同名的本地函数） |

同时 durable 自身的链接/宿主使用数也是 0：

```bash
grep -rn "webSocketLink\|ipcLink\|memoryLinkPair\|createHost" packages/durable/src/   # 0 hits
```

**结论**：v1.3.0 里 chord 的宿主/RPC/插件/热重载半边（8,117 行）**在本仓库内没有任何消费者**。它要么服务于仓库外的应用，要么是历史遗留。无论如何，**pi-py 侧没有任何理由移植它**。

> 这也解释了 v1.2.0→v1.3.0 的包结构重组：`protocol`/`client`/`server`（v0.85.1 的 chord 宿主消费者）在 v1.2.0 被删除（报告 02 行 373-375），宿主半边的消费者随之消失，只剩下 durable 消费 delta 子层。

---

## 4. 三条路的可行性评估

> 共同前提：本节的「工作量」以**上游 TS 行为等价**为基准。所有路线都**不含** chord 宿主半边（零消费者，无移植价值）。

### 4.0 三条路的公共输入：必须复刻的孤岛

| 模块 | 行数 | Python 侧难度 |
|---|---|---|
| `delta/tracker.ts` | 2,239 | **高**（唯一真正难的部分） |
| `delta/index.ts` | 738 | 中（`apply` 系列 + op 校验；`encoder`/`decoder` 可丢） |
| `delta/apply-immutable-trusted.ts` | 128 | 低 |
| `delta/draft.ts` | 10 | 低（纯类型） |
| `state.ts` | 423 | 中（订阅序列化 + 100 深度积压 + 合并语义） |
| `output.ts` | 383 | 低-中（自包含，无依赖） |
| `json.ts` | 135 | 低（但须精确复刻 strict-JSON 校验规则） |
| `context/index.ts` | 133 | **低**（全文 133 行，已实读，逻辑直白） |
| **合计** | **4,189** | — |

测试侧参照（实读行数，命令见附录 A#8）：

| 测试文件 | 行数 |
|---|---|
| `delta-tracker/tracker.test.ts` | 1,507 |
| `delta.test.ts` | 506 |
| `replicated-state.test.ts` | 351 |
| `replicated-state-delivery.test.ts` | 248 |
| `delta-apply-immutable.test.ts` | 311 |
| `state.test.ts` | 260 |
| `state-diff.test.ts` | 256 |
| `state-init.test.ts` | 232 |
| `state-draft.test.ts` | 117 |
| `context.test.ts` | 89 |
| `json.test.ts` | 65 |
| `delta-clone.test.ts` | 53 |
| `state-value.test.ts` | 37 |
| `delta-tracker/retention.test.ts` | 34 |
| **小计（14 个文件）** | **4,066** |
| `state-fuzz.test.ts`（模糊测试） | 123 |
| **合计** | **4,189** |

> 注：测试合计 4,189 与 §4.0 的源码孤岛 4,189 行**数值相同纯属巧合**，两者互不相关。

---

### 4.1 路线 (a)：自建 Python 版 chord（限定孤岛子集）

**工作内容**

1. `pi_chord/json.py`（~150 行）— `JsonValue` 类型别名 + `copy_json`（含 strict-JSON 校验：拒绝非有限浮点、循环引用、非稠密数组、class 实例、`undefined`；`omit_undefined_properties` 选项）。**须精确复刻 `json.ts:33-60` 的规则**，否则 tracker 的「placement 校验」语义会漂移。
2. `pi_chord/context.py`（~140 行）— 133 行几乎可 1:1 翻译。Python 用 `contextvars` 或显式传参均可；**注意 `Context` 是显式传参值对象，不是隐式上下文**（`context/index.ts:8-13` 注释：「Immutable invocation-scoped values passed explicitly through operations」）——所以**不要**用 `contextvars` 隐式化，否则与上游语义不符。`_ContextValue` 链式查找（`:45-65`）直接照搬。
3. `pi_chord/delta/tracker.py`（~1,200–1,800 行）— **最难的一块**：
   - Draft overlay：Python 无 `Proxy`。可选方案：(i) `dict`/`list` 子类重载 `__getitem__`/`__setitem__`/`__delitem__`/`append`/`push`… 并记录脏路径；(ii) overlay 结构（`{path: value}` + 读时回退），在 `prepare()` 时物化。
   - **需要处理的 Python/JS 语义差**：
     - `["a"]`/`["t"]` op 的计数单位是 **UTF-16 code unit**（`delta/README.md:163`），Python `str` 是 code point。若保留 op 格式，须显式做 UTF-16 换算（含代理对），否则非 BMP 字符（emoji）会与上游不一致。**这是最容易踩的坑。**
     - 对象键顺序：JS 属性顺序 + `README.md:141` 的「Equality ignores key order and prototype」语义，Python `dict` 保序，需自行定义等价判定。
     - `__proto__`/`constructor`/`prototype` 保留段（`delta/index.ts:131`）：Python 无原型链，但 `__proto__` 是普通字符串键。**为保持 op 格式兼容须保留这套保留字**（否则同一编辑在两侧产生不同 op）。
     - draft handle 撤销：`prepare()`/`abort()` 后任何使用须抛错（`README.md:33`），Python 里用一个 `_revoked` 标志 + `__getattr__` 守卫即可。
   - `adopt()` 的竞争判定、`baseRevision` 校验、no-op 规范化（`README.md:138-143`）。
4. `pi_chord/delta/apply.py`（~350 行）— `apply`（可变，adopt payload）、`apply_immutable`（copy-on-write 路径复制）、`apply_immutable_batches`（单次 COW 作用域跨批共享）、`overlap()`（`delta/index.ts:87-112`，最长后缀=前缀探测，~30 行可直译）、`assert_valid_op`/`assert_safe_path`。
   - 结构共享在 Python 里天然可行：未修改子树直接复用对象引用（无需 `Object.freeze`）。
5. `pi_chord/state.py`（~450 行）— `replicated_state()` / `ReplicatedStateSource` / `AttachedReplicatedState`。订阅者串行化、最多 100 条积压后**只保留最新**（`state.ts:22-25` 注释：「overflow keeps only the newest pending value/context/delivery, so update sequences may skip」）、失败隔离不中断投递。这块语义密集，但无算法难度。
6. `pi_chord/output.py`（~400 行）— `OutputBuffer`（head/tail 窗口）、`Progress`（按大小节流提交）、`bound_output`、`sanitize_output`、`character_end`、`utf8_byte_length`。**自包含无依赖**，可独立开工。
7. **测试**：建议按上游测试清单逐条对照移植（`tracker.test.ts` 1,507 行为主）。

**工作量：M–L**（估 2,500–3,500 行 Python + 3,000–4,000 行测试）
- 分解：`context`+`json`+`output` 约 **S–M**（约 700 行，逻辑直白，可先交付）；`delta` 约 **L**（tracker 是唯一真正的技术风险点）；`state` 约 **M**。
- 若先把 `context`/`json`/`output` 落地（~1 周级），durable 的骨架就能立起来，tracker 可并行开发。

**风险**
- 🟡 **中**：tracker 的 op 生成要实现到「精确但非规范」的一致性——但注意 `README.md:94`「Don't infer meaning from op shape. Only the resulting value is contractual.」**只要结果值一致，op 形态允许不同**。这大幅降低了风险：pi-py 不需要逐 op 复刻 JS 实现，只需要 `apply(base, ops) == value` 且 delta 链能收敛。**唯一硬约束是 op 格式在 pi-py 内部自洽。**
- 🟡 **中**：UTF-16 计数（见上）。
- 🟢 **低**：性能。Python 的 COW / dict 复制比 JS 慢，但 durable 的负载是文档级状态，非热点。上游自己有 benchmark（`delta-traversal.bench.ts`）说明他们关心，但 pi-py 初期可不追平。

**对 durable 可移植性的影响：最有利。** `transaction.ts`、三个存储后端、`view.ts`、`observation.ts` 基本可逐行对照迁移；`prepared.ops`、`deltasSinceBase`、`checkpointWhen` 等概念全部保留。这是**唯一与「忠实镜像上游」决定一致的路线**。

---

### 4.2 路线 (b)：简化掉 —— 把 durable 裁到不依赖 chord

**关键认知纠偏**：这不是「删掉一个依赖」，而是**重写 durable 的存储格式与发布粒度**。因为 `prepared.ops` 深度嵌入 `transaction.ts`（1,043 行）、`memory.ts`、`sqlite/storage.ts`、`view.ts`、`observation.ts`、`session.ts`、以及 5 个 harness 模块。

**最小可行裁剪方案**

1. **用可变 dict + 提交边界深拷贝替代 tracker**：`tx.doc(token)` 返回一个普通可变 dict；提交时 `copy_json(value)` 作为新 revision。
2. **存储永远是 base**：`planDocument` 的 `:1019-1024` 三元式退化为恒取 `{kind:"base", value}`；`deltasSinceBase`/`checkpointWhen` 机制整体删除。
3. **发布用全值**：`view.ts:226-231` 已经有一条 full-value 回退分支（`docOps.push(["s", path, change.value])`，当 incarnation 或 version 变化时走这条路）。**方案 (b) = 永远走这条 else 分支。**
4. **`replicatedState` 降级为简单的 listener 集合**：去掉 `ReplicatedStateSource`/attachment/frame 机制，`CommittedStateSource` 直接用全值通知。
5. **保留 `Context`**：这块（133 行）无论如何都值得移植，它是 durable 所有异步 API 的取消语义载体，且与状态模型无关（**即便选 (b) 也应单独移植 `context.py`**）。

**会损失哪些行为**（逐条对照）

| # | 损失 | 严重度 | 说明 |
|---|---|---|---|
| 1 | **增量持久化** | 🔴 高 | 每次文档提交写整个文档 JSON，而非 `["s",["messages",3],"..."]` 级别的 op。长会话 transcript 的写放大与存储增长显著。`deltasSinceBase`/`checkpointWhen` 的折叠策略变得无意义。 |
| 2 | **观察者的增量帧** | 🟠 中 | `observer.advance(value, ops, context)` 的 `ops` 退化为全值 set。对 `harness/view.ts` 的「会话视图」（内含全部 entries + docs）而言，每次任一文档变更都会把整个视图路径的更新推送为全值。 |
| 3 | **结构共享** | 🟠 中 | 每次 revision 是全量深拷贝；上游是「未变更子树共享」。内存与 GC 压力上升。 |
| 4 | **并发 change 的 stale 竞争语义** | 🟢 低 | `adopt()` 拒绝陈旧竞争者、`prepare()` 后 draft 失效等（`README.md:131-136`）——可以用「版本号 + 锁」平价替代，行为差异对使用方不可见。 |
| 5 | **`prepared.value` vs `prepared.base` 的「候选可读」** | 🟢 低 | `prepare()` 后失败可继续读候选（`README.md:135-136`），(b) 中无对应物。durable 未依赖此点。 |
| 6 | **on-disk 格式与上游不兼容** | 🟢 低 | pi-py 无读取上游数据的需求。 |
| 7 | **no-op 规范化** | 🟢 低 | 「写回原值→空 ops→不发布」的优化消失，会产生「值未变但仍发布」的多余通知。**这是可观察的行为差异**（订阅者会收到一次值相同的投递）。 |

**工作量：M**（估 1,200–2,000 行改动）
- 表面看「删代码」是 S，但实际要动：`session/transaction.ts`（重写文档获取/提交路径）、3 个存储后端（查询与写入路径）、`harness/view.ts` + `session/observation.ts`（发布路径）、`session/session.ts`、`harness/task-graph.ts`、`harness/types.ts` 的 `checkpointWhen` 接口。**且这些改动彼此耦合**（存储格式与发布粒度是同一个决定）。
- 相比之下 (a) 是「新增一个包」，(b) 是「改造已有包的骨架」——(b) 的**回归风险更高**，因为它触碰 durable 的全部核心路径。

**风险**
- 🟠 **中高**：行为差异集中在持久化与发布两处，是 durable 最核心的机制；且**偏离用户「忠实镜像上游 v1.3.0」的既定决定**。
- 🟢 **低（长期）**：一旦上游 chord 继续演进（v0.85.1→v1.3.0 delta 层涨了 2.93×），(b) 之后的每一次上游同步都会产生**结构性冲突**，无法用「对照迁移」的方式跟进。

**对 durable 可移植性的影响：负面。** 上游 durable 的代码结构、测试用例、以及 `types.ts` 里的 `checkpointWhen`/`deltasSinceBase` 契约都将失去对应物，后续每个上游版本都要人工判断差异。

---

### 4.3 路线 (c)：引第三方

**Python 生态候选库盘点**（判断基于各库公开文档能力面；**标注「未验证」者为本次未取得一手文档**）

| 库 | 提供什么 | 缺什么（相对 chord/delta） | 判断 |
|---|---|---|---|
| **[pyrsistent](https://pypi.org/project/pyrsistent/0.15.7/)** | `PMap`/`PVector`/`PSet`/`PRecord`；**路径复制（path copying）结构共享**；`evolver()` 带「transaction like」语义 | ① 只覆盖 `PMap`/`PVector`，**不作用于原生 dict/list**——而 chord draft 操作的是**普通 JSON 值**，durable 全代码库用原生对象。要用它就得在 durable 每个边界 `freeze`/`thaw`，**全局数据模型改变**；② **不产出 op/delta**；③ 无 revision/adopt 协议；④ 无 no-op 规范化 | ❌ **不适用**（数据模型冲突是致命的） |
| **[rpds-py](https://pypi.org/project/rpds-py/2026.9.1/)** | Rust `rpds` 的 Python 绑定，持久化数据结构（`HashTrieMap`/`List`），性能优于 pyrsistent | 同 pyrsistent 的 ①②③④ | ❌ **不适用**（同上） |
| **immutables** | `Map`/`Set` 不可变映射 | 仅映射；无 op、无 tracker、无结构共享保证 | ❌ 不适用 |
| **[jsonpatch](https://pypi.org/project/jsonpatch/1.3/)** | RFC 6902 JSON Patch：`JsonPatch`/`make_patch`/`apply` | ① op 词表与 chord **不同**（`add`/`remove`/`replace`/`move`/`copy`/`test` vs chord 的 `r/s/d/a/t/p/m`）→ 存储格式与上游不一致；② `make_patch` 需要**两个完整树**做 diff，O(tree)，而 chord 是 draft 期间记录脏路径；③ 无 draft overlay、无 revision、无结构共享 | ⚠️ 只能提供 `apply` 的一半，且格式不兼容 |
| **[deepdiff](https://pypi.org/project/deepdiff/8.6.0/)** | 计算任意两个 Python 对象的差异（含 delta 输出） | 全树 diff，每次提交 O(tree) 遍历；无 draft、无结构共享、无 revision 协议 | ⚠️ 性能特征与 chord 相反 |
| **[evsrc](https://pypi.org/project/evsrc/)** 等 event-sourcing 框架 | 事件溯源 + 聚合 + 快照 | 编程模型是**领域事件 + 聚合根**，不是「通用 JSON 文档的事务性 delta」；且会引入框架级约定 | ❌ 模型不匹配 |
| `blinker` / `RxPY` | 观察者/信号基础设施 | 只解决 `replicatedState` 的订阅分发，且**不满足** chord 的串行化 + 100 深度积压只保留最新 + 失败隔离语义 | ⚠️ 最多替换 `state.ts` 的一小部分 |
| **pydantic** | 校验/schema（≈ typebox 角色） | 与状态追踪无关；pi-py 已在用 | ➖ 无关 |

**结论：没有任何单一库提供 chord/delta 的契约；没有任何组合能避免手写 tracker。**

更本质的问题：**(c) 的收益是负的。** 因为 chord 孤立子集的依赖闭包是 **0**（不依赖 typebox，不依赖任何第三方），引进 pyrsistent/jsonpatch 只会**增加**依赖，同时因为 op 词表与数据模型不匹配，**仍然要自己写 draft → ops → adopt 的主循环**。也就是说 (c) 会退化成「(a) + 一堆不适配的依赖」。

**工作量：M–L，但产出质量低于 (a)**
- 若强行用 `deepdiff`/`jsonpatch` 拼：(tracker 的主循环 ~600–900 行) + (格式适配层 ~300–500 行) + (freeze/thaw 或全树 diff 的性能损失)。**比 (a) 的直接移植更累，且结果与上游语义更远。**

**风险：高。** 额外的依赖面、op 格式与上游不可对照、性能特征相反、上游同步时无法对照。

**对 durable 可移植性的影响：负面。** 存储格式与上游永久分叉。

---

## 5. 建议

### 5.1 明确推荐：路线 (a)，但严格限定在「孤岛子集」

**推荐做法**

1. **新建独立包**（建议 `packages/pi-chord/`，发布名 `pi-py-chord`），只含 §4.0 的 4,189 行对应物：
   - `pi_chord/context.py`、`pi_chord/json.py`、`pi_chord/output.py`（**先做，S–M，无依赖，可立即验证**）
   - `pi_chord/delta/tracker.py`、`apply.py`（**主力工作，L**）
   - `pi_chord/state.py`（M）
2. **明确不移植**：宿主半边全部 8,117 行（零消费者）；`delta/diff.ts`（durable 零引用）；`encoder`/`decoder`（单进程无 wire 需求）；`cell.ts`/`types.ts`/`errors.ts`/`revision-validator.ts`。
3. **测试策略**：按上游测试清单对照移植（`tracker.test.ts` 1,507 行为主），并**保留 `state-fuzz.test.ts` 的模糊测试思路**——这是保证 tracker 正确性的最高性价比手段。
4. **op 一致性采用「宽容」策略**：利用 `delta/README.md:94`「Only the resulting value is contractual」，只强制 `apply(base, ops) == value` 与 delta 链收敛，**不追求逐 op 与 JS 实现一致**。这能省掉大量逆向工作。
5. **UTF-16 计数务必单独立测试**：`["a"]`/`["t"]` 作用于含 emoji/代理对的字符串时，Python 必须显式按 UTF-16 code unit 计数。

**为什么不是 (b)**：与用户已确认的「忠实镜像上游 v1.3.0」直接冲突；(b) 改的是 durable 最核心的存储与发布路径（回归风险高于 (a) 的新增包），且让后续每次上游同步都变成结构性冲突。

**为什么不是 (c)**：见 §4.3——孤岛依赖闭包为 0，引第三方只增负担不减工作，且 op 格式与数据模型双重不匹配。

**若排期不允许**：可接受的**降级顺序**是——先交付 `context.py` + `json.py` + `output.py`（让 durable 骨架立起来，约 700 行），tracker 作为唯一的硬骨头单独立项；**而不是** 改用路线 (b)。

### 5.2 如果最终仍选 (b)，与上游的行为差异有多大

**行为差异量化**：

| 维度 | 差异程度 |
|---|---|
| **公开 API 与语义** | **小**。`Session`/`Transaction`/`Storage` 的对外契约、原子性、序号、水合语义、取消语义均可保持。调用方基本无感（除了 no-op 仍会发布通知这一点）。 |
| **持久化格式** | **大且不可逆**。base-only，无 delta 链；`checkpointWhen`/`deltasSinceBase` 契约消失。 |
| **发布粒度** | **大**。观察者从「收到精确 op 批次」变为「收到全值 set」。`view.ts` 的 `prefixed()` op 重写逻辑（`:249-266`）失去意义。 |
| **性能特征** | **中-大**。提交成本从 O(编辑量) 变为 O(文档大小)；内存占用因失去结构共享而上升。 |
| **可对照性** | **大**。后续上游每版同步都需要人工判断，无法逐行对照迁移。 |

**一句话**：选 (b) 的话，pi-durable 的**对外行为**与上游基本一致，但**内部机制与数据格式**会永久分叉；「忠实镜像」在 durable 这一包上就不再成立了。

---

## 6. 无法确定的项

| # | 项 | 为什么无法确定 | 影响 |
|---|---|---|---|
| 1 | **宿主半边（8,117 行）为何零消费** | 本仓库内确实零引用，但无法排除它服务于**仓库外的下游应用**（如私有产品）。未查仓库外信息。 | 低。即便有外部消费者，pi-py 也不需要它。 |
| 2 | **chord 是否已进入维护模式** | v1.3.0（2026-10-15）仍在发版且有大量 breaking changes，但消费者从 6 个降到 2 个。无法判定这是「收敛到核心」还是「逐步退役」。 | 中。若 chord 未来被裁剪，pi-py 的移植目标会移动。**建议把本次移植锚定 v1.3.0 并在 `UPSTREAM_VERSION` 中记录。** |
| 3 | **`delta/revision-validator.ts`（75 行）是否死代码** | `grep -rn "revision-validator\|validateRevision" packages/chord/src/` 无结果，推断无人 import；但未做动态引用/构建产物核对。 | 极低（已归类为不需要）。 |
| 4 | **`state.ts` 的 100 深度积压/合并语义是否被 durable 依赖** | `state.ts:22-25` 定义了精确语义，但未逐条验证 durable 的订阅者是否**依赖**「序列可跳跃」这一行为。 | 中。若 (a) 移植时简化此处，可能引入隐蔽差异。 |
| 5 | **tracker 的 Proxy 性能特征在 Python 下的实际影响** | 上游 benchmark（`delta-traversal.bench.ts`，README 提到「20,000 hot nested reads ≈ 9 ms through the tracker vs 0.08 ms on raw objects」）是 JS 数据；Python 的等价实现慢多少未测量。 | 中。影响 (a) 的性能验收标准，但不影响可行性。 |
| 6 | **第三方库能力面的时效性** | pyrsistent/rpds-py/jsonpatch/deepdiff 的判断基于其公开文档；PyPI 页面在本次环境中被 bot challenge 拦截，部分信息来自搜索摘要与 GitHub README。**未做本地安装验证**（任务禁止安装依赖）。 | 中。(c) 的结论方向不会因此改变（数据模型与 op 格式不匹配是结构性的），但具体版本特性可能有过时。 |
| 7 | **pi-py 是否可接受新增一个 workspace 包** | 属产品/架构决定，非技术调研能确定。 | 低。也可把孤岛并入 `pi-py-durable` 内部模块。 |

---

## 附录 A：可复现命令

```bash
# 1. 包概况
cat /tmp/pi-up/v1.3.0/packages/chord/package.json
find /tmp/pi-up/v1.3.0/packages/chord/src -name "*.ts" | wc -l          # 31
find /tmp/pi-up/v1.3.0/packages/chord/src -name "*.ts" | xargs wc -l | tail -1   # 13553
find /tmp/pi-up/v1.3.0/packages/chord/test -name "*.ts" | xargs wc -l | tail -1  # 16334
cat -n /tmp/pi-up/v1.3.0/packages/chord/src/index.ts                    # 公开入口全文

# 2. v1.2.0「移除依赖」定位
grep -rn -i "removed the dependency" /tmp/pi-up/v1.3.0 --include=*.md
#   → packages/coding-agent/CHANGELOG.md:110  （在 coding-agent 的 changelog，非 chord）

# 3. 消费者
grep -rn "@earendil-works/chord" /tmp/pi-up/v1.3.0/packages/*/package.json
grep -rn "@earendil-works/chord" /tmp/pi-up/v1.3.0/packages/ --include=*.ts | awk -F/ '{print $2}' | sort | uniq -c
#   → 164 durable / 8 env / 1 chord

# 4. 符号级依赖（脚本见下）
python3 - <<'EOF'
import re, collections, pathlib
pat = re.compile(r'import\s+(type\s+)?\{([^}]*)\}\s+from\s+"@earendil-works/chord([^"]*)"', re.S)
counts, subpaths = collections.Counter(), collections.Counter()
for p in pathlib.Path("/tmp/pi-up/v1.3.0/packages").rglob("*.ts"):
    if "packages/chord/" in str(p): continue
    for m in pat.finditer(p.read_text(errors="replace")):
        subpaths[m.group(3) or "."] += 1
        for sym in m.group(2).split(","):
            sym = re.sub(r'^type\s+', '', sym.strip()).split(" as ")[0].strip()
            if sym: counts[sym] += 1
print(subpaths.most_common()); print(counts.most_common())
EOF

# 5. 宿主半边零消费验证
for s in createHost defineFacet defineRegistry ipcLink webSocketLink memoryLinkPair \
         acceptWebSockets hostNamed 'local(' 'method('; do
  echo "$s: $(grep -rn "\b$s" /tmp/pi-up/v1.3.0/packages/ --include=*.ts | grep -v '^/tmp/pi-up/v1.3.0/packages/chord/' | wc -l)"
done

# 6. 孤岛依赖闭包 = 0（无 typebox、无外部 import）
grep -rn 'from "' /tmp/pi-up/v1.3.0/packages/chord/src/delta/ \
     /tmp/pi-up/v1.3.0/packages/chord/src/context/ \
     /tmp/pi-up/v1.3.0/packages/chord/src/{json,output,state,errors}.ts | grep -v 'from "\.'

# 7. durable 不用的 delta 能力
grep -rn "diffRevisions" /tmp/pi-up/v1.3.0/packages/durable/     # 0 hits
grep -rn "diffRevisions\|revision-validator" /tmp/pi-up/v1.3.0/packages/chord/src/  # 仅 delta/index.ts:114 导出

# 8. 孤岛对应测试行数
cd /tmp/pi-up/v1.3.0/packages/chord/test
wc -l delta-tracker/tracker.test.ts delta-tracker/retention.test.ts delta.test.ts \
      delta-apply-immutable.test.ts delta-clone.test.ts state.test.ts state-draft.test.ts \
      state-value.test.ts state-init.test.ts state-diff.test.ts replicated-state.test.ts \
      replicated-state-delivery.test.ts context.test.ts json.test.ts | tail -1   # 4066
wc -l state-fuzz.test.ts                                                          # 123

# 9. errors.ts 只被宿主半边 + cell.ts + index.ts 引用（孤岛不引用）
grep -rn 'errors.ts"' /tmp/pi-up/v1.3.0/packages/chord/src/
```

## 附录 B：孤岛模块行数核对

```bash
cd /tmp/pi-up/v1.3.0/packages/chord
wc -l src/delta/tracker.ts src/delta/index.ts src/delta/apply-immutable-trusted.ts \
      src/delta/draft.ts src/context/index.ts src/json.ts src/output.ts src/state.ts | tail -1
# 4189 total
wc -l src/host.ts src/connection.ts src/object.ts src/reload.ts src/remote.ts src/link.ts \
      src/contract.ts src/manager.ts src/cluster.ts src/schema.ts src/wire.ts src/loader.ts \
      src/publish.ts src/declare.ts src/registry.ts src/websocket.ts src/node.ts | tail -1
# 8117 total
```

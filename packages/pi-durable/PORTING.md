# pi-durable 移植注记

对应上游：[`@earendil-works/pi-durable`](https://github.com/earendil-works/pi)（v1.3.0）

本包已交付**批次 A**、**批次 C₁**、**批次 D-1** 与**批次 C₂**：

* **批次 A**：「env 岛」（`src/env/`）与它的可执行规格（`src/testing/` 中与 env 有关的部分）
* **批次 C₁**：工具层里不依赖 `harness/` 的纯逻辑 —— `truncate.ts`、`search.ts`、
  `tools/{image,image-processor,path-utils,file-mutation-queue}.ts`、`images/{exif,index}.ts`
* **批次 D-1**：`harness/` 的 **define 层 + 工具执行的最小类型面**（`src/harness/`），
  用来解锁批次 C₂
* **批次 C₂**：`tools/{bash,read,write,edit,edit-diff,env,index}.ts` +
  `tool-search/`（1,325 行）——**四个编码工具与 ``tool_search`` 现在都能用了**

* **批次 B（进行中）**：存储层 = records + `Storage` 契约 + 内存/SQLite 双后端 + 一致性套件
  + 文档定义层 + **Session 阶段 1**（observation / forks）+ **阶段 2 全部 5 步**
  （`transaction.py` 1,043 行已完结）+ **阶段 3 全部 4 步**（`GuardedStorage` 失败守卫、单写线、`commit` 与提交发布、
  文档缓存逐出与三个只读观察者）；
  **Session 运行时（2,513 行）至此完结**——见〈批次 B〉一节

**批次 D 的其余部分**（`scheduler.ts` 1,953 行等，约 8,100 行）**仍未做**——
逐项清单见〈批次 D-1〉一节的「仍未做的部分」，别以为 D 做完了。

上游入口点与本地模块的对应关系：

| 上游 | 本包 | 上游文件 |
|---|---|---|
| `@earendil-works/pi-durable/env` | `pi_durable.env` | `durable/src/env/index.ts`（377 行） |
| `@earendil-works/pi-durable/env/node` | `pi_durable.env.node` | `durable/src/env/node.ts`（1226 行）、`env/node-watch.ts`（522 行） |
| `@earendil-works/pi-durable/testing` | `pi_durable.testing` | `durable/src/testing/{assertions,env-conformance,runner,types}.ts` |
| （根入口的一部分） | `pi_durable.search` | `durable/src/search.ts`（85 行） |
| （根入口的一部分） | `pi_durable.truncate` | `durable/src/truncate.ts`（174 行） |
| `@earendil-works/pi-durable/tools` | `pi_durable.tools` | `durable/src/tools/*.ts`（9 个文件） |
| `@earendil-works/pi-durable/tool-search` | `pi_durable.tool_search` | `durable/src/tool-search/*.ts` |
| `@earendil-works/pi-durable/images`（部分） | `pi_durable.images` | `durable/src/images/{exif,index}.ts` |
| （`harness/` 的 define 层，内部模块） | `pi_durable.harness` | `durable/src/harness/define.ts:6-17` + `harness/types.ts` 的窄提取 |
| （根入口的记录与契约） | `pi_durable.types` / `ids` / `errors` | `durable/src/types.ts`（存储子集）、`ids.ts`、`errors.ts` |
| `@earendil-works/pi-durable/storage/memory` | `pi_durable.storage.memory` | `durable/src/storage/{memory,scan}.ts` |
| `@earendil-works/pi-durable/storage/sqlite` | `pi_durable.storage.sqlite` | `durable/src/storage/sqlite/*`（除 cloudflare） |
| （文档定义层） | `pi_durable.documents` | `durable/src/documents.ts` |
| （条目种类） | `pi_durable.entries` | `durable/src/entries.ts` |

`env/decode.ts`（30 行）与 `env/line-scan.ts`（145 行）分别对应 `pi_durable.env.decode`
与 `pi_durable.env.line_scan`。

`pi_durable.harness` **不是**一个上游入口点：上游的 `harness/` 全部被根入口 `index.ts`
再导出（`index.ts:12-119`），本包因为只做了极小一片，暂不并入 `pi_durable` 顶层。

---

## JS→Python 通则（施工中**踩出来的**，不是推理出来的）

上面的〈有意偏离上游〉是**逐条**记录。这一节把它们压成**可执行的通则**——
下次遇到同形状的上游代码，直接照这里做，不必再踩一遍。
每条都注明是被哪条测试抓到的。

### R1 ⚠️ 凡上游用 `??=` 缓存 **Promise**，Python 侧缓存 **Task**

```ts
task.committedRead ??= this.#host.storage.task(id, this.#context);   // 缓存 Promise
return task.committedRead;                                            // 可重复 await
```
```python
pending = task.committed_read
if pending is None:
    pending = asyncio.ensure_future(storage.task(id, context))   # ← 必须是 Task
    task.committed_read = pending
return pending
```

**为什么**：JS 的 Promise 可以重复 `await`；Python 的**协程** `await` 第二次会
`RuntimeError: cannot reuse already awaited coroutine`。缓存裸协程会在「同一事务里读同一个
东西两次」时炸——而这恰好是上游写 `??=` 想省掉的那次重复读。

**抓到它的测试**：`test_task_reads_are_memoized_within_one_transaction`
（同时断言「只碰一次存储」与「读两次不炸」）。
**排查口诀**：看到上游 `x ??= <async call>`，就问「这个值会不会被 await 第二次」。

### R2 `queueMicrotask` → `loop.call_soon`，但**不收协程**

`call_soon` 只接受普通可调用对象；协程要用 `asyncio.ensure_future` 包成任务。
JS 的微任务在脚本块结束即排空，`call_soon` 需要事件循环转一轮——本包所有调用点都在
事件循环里，等价。**抓到它的测试**：`test_watch_start_is_never_inline`。

### R3 `Promise.withResolvers` → **惰性** `asyncio.Future`

JS 在任何地方都能建 Promise；Python 的 `Future` 必须绑定运行循环。所以**不在构造器里建**：
先把终态记在字段里，第一次取 `closed`/`done` 时再建；若此前已终结就直接返回已落定的 Future。
**抓到它的测试**：`test_watch_stop_is_idempotent_and_detaches`（终结早于首次 await 也正确）。

### R4 上游「过滤列 + JSON 记录」必须**同步**更新

`retired_at` 是投影列、`record` 是 JSON。只改列会让读取路径（读的是 JSON）以为文档还活着。
**抓到它的症状**：`Document N is missing a required base`（看起来像数据损坏，其实是投影不同步）。
SQLite 与内存两侧都要守这条。

### R5 分支间**不要复用变量名**（mypy 在首次赋值处定型）

```python
if write["type"] == "conversation":
    value = write["value"]      # mypy 把 value 定型为 ConversationRecord
elif write["type"] == "entry":
    value = write["value"]      # ← 报错：ConversationRecord 不能赋给 EntryRecord
```
每个分支用**各自的局部名**（`conversation` / `entry` / `task` / `submission`）。
同样地，`kind = write["type"]` 这种**给判别字段起别名**会**破坏 TypedDict 收窄**——
必须写成 `write["type"] == "..."` 就地比较。

### R6 孤立代理项：JS 的 UTF-16 有、UTF-8 没有

`"\ud800"` 这类孤立代理项在 JS 字符串里合法，**编码不成 UTF-8**，写进 SQLite 会
`UnicodeEncodeError`。策略：**索引列一律 JSON 转义**（纯 ASCII）；**记录列优先
`ensure_ascii=False`**（与上游 `JSON.stringify` 字节一致），遇到代理项再退回 `ensure_ascii=True`。
**抓到它的测试**：`test_keeps_indexed_string_identities_lossless`（一致性套件）。

### R7 非 raw docstring 里的 `\u` 是**转义序列**

写文档说明时不要写 `"""...\ud800..."""`（单反斜杠）——Python 在**编译期**就把它变成真的
孤立代理项，整个模块 import 即崩，而且报错指向的是**另一行**。
写 raw docstring（`r"""..."""`）或用双反斜杠。

### R8 `Number.isSafeInteger` → 显式排除 `bool`

`isinstance(True, int)` 在 Python 里是 `True`，直接 `isinstance(x, int)` 会把布尔当整数收下。

### R9 反向也一样：**别把 Python 的便利当成上游的语义**

`copy_json` 已经返回副本，再套一层 `copy()` 不只是多余——`copy` 还会撞上 `import copy`
的**模块名**（mypy: `Module not callable`）。照抄上游的 `copyJson(...)` 调用即可。

### R10 上游「延迟投递 + 溢出替换」都有**精确**语义，别凭直觉

* 已激活后的 `publish` 是**延迟**投递，只有 `activate()` 同步排空。
* 观察帧溢出**不是「只保留一帧」**：丢弃全部后换成一帧替身，**其后继续正常累积**。

两条都因为「按直觉写测试」先红过一次。
（`test_source_buffers_frames_before_activate_and_drains_synchronously`、
`test_watch_overflow_replaces_pending_frames_with_one`）

### R11 涉及 `asyncio.Lock` 的事务句柄必须**独立**

不可重入：事务回调里再用同一个门面抢同一把锁会**自锁**（施工时挂死 60s）。
把**另一个句柄**传给回调——这也正是上游 `database.ts:23-27` 的契约要求。

### R12 ⚠️ `??` 不是 `or`：**`0` / `""` / `false` 都是有效值**

```ts
const startedAt = candidate?.startedAt ?? value.startedAt ?? now(status === "running");
```
```python
started_at = prior.get("startedAt")
if started_at is None:              # ← 必须显式判 None
    started_at = value.get("startedAt")
if started_at is None:
    started_at = stamp(status == "running")
```

JS 的 `??` 只跳过 `null` / `undefined`；Python 的 `or` 还跳过 `0`、`""`、`False`、`[]`。
**凡上游用 `??` 的地方都不能直译成 `or`。** 时间戳 `0` 是最容易踩的实例：
用 `or` 会让「已盖好的 0」被当成「没值」而重新盖章。

**抓到它的测试**：`test_stamp_times_preserves_a_zero_timestamp`（第 4 步）。
同一族的还有 `options.background ?? false`——`false` 在 `or` 下也会被跳过，虽然那里
恰好结果相同，但**理由不同**，不能靠巧合。

### R13 ⚠️ `Promise.allSettled(...)` 是**先排空、再报错**——这是上游语义，不是缺陷

```ts
if (this.#pendingOperations.size > 0) {
    this.#abortChanges();
    await Promise.allSettled(this.#pendingOperations);   // ← 先等全部落定
    throw new Error("Session commit callback settled before its pending Tx operations");
}
```

`settle_success` 发现还有未决操作时，**先等它们全部落定**才抛错，而不是立刻抛。

⚠️ **不要"修"成 fail-fast**：上游的意图是「让已经发出去的存储调用跑完，别把它们悬在半空」。
真实的未决操作（存储调用）总会落定，所以它不会真的等下去。

**我是怎么发现的**：写测试时用一个**永不落定**的 Future 模拟「未完成的操作」，
结果 `settle_success` 挂死 60 秒被移到后台。判定结论是**测试不现实**，不是实现有 bug——
测试改成「会结束的未决操作」后正确报错，并断言 `pending.done()`。

**给后来者的判据**：如果看到 `allSettled` 似乎会挂，先问「上游是不是**故意**要先排空」。
这里的答案是要，所以别动它。

### R14 ⚠️ 上游「**同步**方法返回 Promise、把同步错误转成被拒绝的 Promise」——Python 不要 `raise`

```ts
doc(token, ...args): Promise<Draft<JsonObject>> {
    try { ... } catch (error) { return Promise.reject(error); }   // ← 不是 throw
}
```
```python
def doc(self, token, *args) -> Awaitable[Draft]:
    try:
        ...
    except Exception as error:
        return _failed_future(error)     # ← 已带异常的 Future，不是 raise
```

**为什么**：上游这些方法是**同步**的，调用方拿到的是一个 Promise。同步阶段的错误被转成
**被拒绝的 Promise**，这样调用方只需要一层 ``try/except`` 包住 ``await``，而不是内外各包一层。

**Python 侧若直接 ``raise``**，调用方的 ``await tx.doc(...)`` 会在**求值参数时**就炸，
错误位置与上游不同，调用方写在外面的 ``try`` 也包不住。

**抓到它的测试**：`test_failed_acquisition_is_a_rejected_awaitable_not_a_raise`
（断言 ``tx.doc(...)`` 本身**不抛**，``await`` 它才抛）。

注意 ``_failed_future`` 里要调一次 ``future.exception()``：否则 Future 未被 await 时
Python 会打「exception was never retrieved」警告。

### R15 ⚠️ `asyncio.ensure_future` 需要**运行中的事件循环**，`.then()` 不需要

上游 ``#enqueue`` 只做 ``this.#tail.then(job)``，所以 ``commit()`` / ``close()`` 这些
**同步**方法在**任何**上下文里都能调（包括没有事件循环的地方）。

Python 侧要保留「**急切调度**」这条性质（见下），就必须 ``asyncio.ensure_future(...)``，
而它要求当前线程有正在运行的事件循环。所以：

* ``SessionImpl.commit`` / ``read_on_line`` **必须在协程里调用**；
* 上游的对应方法没有这个限制。

这是**真实差异**，不是可以绕过的细节。之所以接受它：另一个方案 ``asyncio.Lock``
虽然哪里都能建，却**丢掉了急切调度**——而 ``close()`` 里就是「发了不管」
（``this.close(...).catch(() => {})``），没有急切调度那行代码永远不会执行。

**抓到它的方式**：设计写线时对比两种方案的性质，而不是事后被测试逼出来。
对照测试：`test_jobs_are_scheduled_eagerly`、`test_fire_and_forget_job_still_runs`。

### R16 ⚠️ 串行尾链 ≠ `asyncio.Lock`：三条性质要一起看

| 性质 | 上游 promise 链 | 串行尾链（本包） | `asyncio.Lock` |
|---|---|---|---|
| 顺序 | ✓ | ✓ | ✓（FIFO） |
| 失败只回调用方、**不断链** | ✓（`.then(ok, err)` 两边吞） | ✓（``_settle`` 吞 ``BaseException``） | ✓（``finally`` 释放） |
| **急切调度**（调用方不 await 也会跑） | ✓ | ✓ | ❌ **会丢** |
| 可在无事件循环处构造 | ✓ | ❌（R15） | ✓ |

**结论**：选串行尾链。丢掉的只有「无事件循环也能构造」，而它换来的是急切调度——
后者是 ``close()`` 正确性所依赖的。

**不可重入**：作业内部再 ``_enqueue`` 会自锁（内层等外层、外层等内层）。
上游的 promise 链**有同样的性质**，所以这不是移植引入的——是设计约束，
上游文档也明说「回调里只通过 ``tx`` 读」（``types.ts:946-948``）。

**取消**：Python 的 Task 可被取消。``_settle`` 把 ``CancelledError`` 也吞掉以保证**链不断**
——与上游 ``.then(ok, err)`` 一致。

### R17 ⚠️ 「提交成功」与「Session 仍健康」是**两件事**

``#publish`` 跑在**存储提交之后**（``session.ts:739``）。内部监听器（``observeCommits``）
抛出时走的是 ``contained(..., (error) => this.fail(error))``——而 ``fail()``
**只记录失败、不抛**（``session.ts:210-218``）。

于是那一次 ``commit()`` **照常返回它的结果**，尽管 Session 已经被标记为失败。

**我一开始写错了测试**（断言 ``commit`` 会抛），跑出来才发现上游不是这样。
现在测试断言三件事：提交返回结果 ✓、``session.failure`` 已设 ✓、``closed`` 落定为 failed ✓。

**为什么这是对的**：提交已经**持久化**了。把「持久化成功」报成异常，会让调用方以为没写成
而重试——那才是真正的破坏。内部监听器的失败只需要让 Session 失败（后续调用拒绝），
不该改写已经发生的事实。

**推论**：任何写在存储提交**之后**的失败路径，都不能把该次提交变成异常。

### R18 ⚠️ 替身（monkey-patch）类方法时用**保存 + 还原**，**不要** `del`

```python
original = Transaction.adopt
Transaction.adopt = exploding_adopt
try:
    ...
finally:
    Transaction.adopt = original      # ← 不是 `del Transaction.adopt`
```

Python 的类属性存在**类的 `__dict__`** 里。``del Transaction.adopt`` 会把**真实方法**
从类上永久删掉——**同一个测试会话里后面所有用例**都会 ``AttributeError``。

我踩到过：一条测试用 ``del`` 还原，导致紧接着 6 条用例全部失败，
报错还是 ``'Transaction' object has no attribute 'adopt'``，看起来像实现坏了。

JS 侧没有对应陷阱（``obj.method = original`` 覆盖回来即可，``delete obj.method`` 删的是实例属性）。
**更稳的做法**：用 ``pytest`` 的 ``monkeypatch.setattr``，它自动还原。

### R19 ⚠️ 上游的**降级路径**要先问「这个危险在 Python 里存在吗」

上游 ``#scheduleDocumentSweep`` 有一段降级：``setTimeout`` 在 Node 里会**吊住进程**，
所以要先 ``unref()``；「定时器无法 unref 时（Cloudflare Workers）就不保留定时器，
改为每次提交结束时逐出」。

**Python 的 ``asyncio.TimerHandle`` 没有 ``unref``——但也不需要**：
``asyncio.run()`` 在主协程结束后就关循环，挂起的定时器不会吊住进程。
也就是说**那条判据在这里恒为假**，照抄会得到一段永远不触发的死代码。

**做法**：保留同一个降级**形状**，把判据换成 Python 侧**真实存在**的那一个：
**当前没有运行中的事件循环**（``asyncio.get_running_loop()`` 抛 ``RuntimeError``）。
**后果要写清**：没有事件循环时，过期文档改由**每次提交结束**时的扫除回收，
而不是按时回收——状态始终正确，只是内存回收晚一步。

⚠️ 施工时踩到：**两条路都要过这个判据**。「立刻排」（``delay <= 0`` → ``call_soon``）
同样需要事件循环，只给 ``call_later`` 那条加保护会漏。
（``test_timers_degrade_when_there_is_no_running_loop``）

**通则**：遇到上游为某个**平台危险**写的降级路径，先判断该危险在 Python 是否成立——
不成立就用 Python 侧真实存在的危险**替换判据**，而不是删掉降级、也不是照抄一段死代码。

### R20 ⚠️ 传「失败记录」还是「错误本身」，逐字核对上游

```ts
closeSession(failure?: { readonly error: unknown }): void {
    this.#terminate(failure === undefined
        ? { reason: "session_closed" }
        : { reason: "session_failed", error: failure.error });   // ← 取 .error
}
```

我在阶段 1 把它写成了 ``error: failure``——把**包裹字典**当成了错误本身。
症状：观察者拿到的 ``error`` 是 ``{"error": RuntimeError(...)}``，
断言「同一错误」时对不上身份。

**为什么没在阶段 1 被发现**：那时没有 Session，没人调用 ``close_session(failure)``；
阶段 3 第 4 步的 ``test_watch_doc_ends_with_session_failed`` 才第一次真的传进去。
**通则**：**跨阶段的接口**（阶段 1 定义、阶段 3 才用）要把「参数是包裹还是本体」在两边都写清，
不然要等到接线时才暴露。

## R 系列总览（R1–R20）

**哪些是上游语义（必须照做），哪些是本端选择（可以改但要写清代价）。**

| # | 主题 | 性质 | 一句话 |
|---|---|---|---|
| R1 | `??=` 缓存 Promise → 缓存 **Task** | **上游语义**（JS Promise 可重复 await） | 缓存裸协程第二次 await 会炸 |
| R2 | `queueMicrotask` → `call_soon`（不收协程） | 机制映射 | 协程要 `ensure_future` 包 |
| R3 | `Promise.withResolvers` → **惰性** Future | 机制映射 | Future 必须绑运行循环 |
| R4 | 过滤列与 JSON 记录**必须同步** | **上游语义** | 只改列会让退休文档看起来还活着 |
| R5 | 分支间不复用变量名；判别字段别起别名 | 本端选择（mypy 限制） | 起别名会破坏 TypedDict 收窄 |
| R6 | 孤立代理项：索引列 JSON 转义，记录列按需退回 | **上游语义**（UTF-16 vs UTF-8） | JS 能带 `\ud800`，UTF-8 不能 |
| R7 | 非 raw docstring 里的 `\u` 是转义序列 | 本端陷阱 | 编译期变成真代理项，import 即崩 |
| R8 | `Number.isSafeInteger` → 排除 `bool` | 机制映射 | `isinstance(True, int)` 是 True |
| R9 | 别把 Python 的便利当上游语义 | 本端陷阱 | `copy(copy_json(...))` 撞模块名 |
| R10 | 延迟投递与溢出替换有**精确**语义 | **上游语义** | 溢出不是「只保留一帧」 |
| R11 | `asyncio.Lock` 事务句柄必须**独立** | 机制映射 | 不可重入会自锁 |
| R12 | `??` **不是** `or`（`0`/`""`/`false` 有效） | **上游语义** | 时间戳 `0` 会被 `or` 当成没值 |
| R13 | `allSettled` 是**先排空再报错** | **上游语义**（不是缺陷） | 别「修」成 fail-fast |
| R14 | 同步方法返回**被拒绝的 Promise**，不要 `raise` | **上游语义** | 否则错误位置与上游不同，外层 `try` 包不住 |
| R15 | `ensure_future` 需要运行中的循环，`.then()` 不需要 | **本端选择**（代价） | 换来 R16 的急切调度 |
| R16 | 串行尾链 ≠ `asyncio.Lock`（四条性质） | **本端选择** | 锁会丢掉「不 await 也会跑」 |
| R17 | 「提交成功」与「Session 仍健康」是两件事 | **上游语义** | 提交已持久化，报成异常会让调用方重试 |
| R18 | 替身类方法用保存+还原，**不要** `del` | 本端陷阱 | `del` 会把真实方法从类上永久删掉 |
| R19 | 降级路径先问「这个危险在 Python 存在吗」 | **本端选择** | 判据换成「没有运行中的循环」 |
| R20 | 「失败记录」还是「错误本身」要逐字核对 | **跨阶段接口陷阱** | 传错包裹层，身份断言就对不上 |

**其中 11 条是上游语义**（R1、R4、R6、R10、R12、R13、R14、R17，以及 R2/R3/R11 的机制映射），
**9 条是本端选择或本端陷阱**。每条都注明**是被哪条测试抓到的**——
这样后来的人能判断它是否仍然成立。

### R21 ⚠️ 排依赖顺序的方法论 —— **四个实证，三个面（第三面有两种表现）**

排批次顺序时，**只有一张图是不够的**。这条通则被同一个坑咬了三次，每次换一个面：

#### 第一面：**运行时图 ≠ 混合图**（`import type` 被擦除）

TS 的 `import type` 编译后**擦除**，所以「A 依赖 B」在图上看是真的、运行时却不存在。
**用混合图排实施顺序会误判层级。**

**实例**：混合图里 `types.ts` 依赖 `view.ts`/`usage.ts`/`task-graph.ts`，而 `view.ts` 又依赖
`generation`/`live`……像一个绕不出去的环。排除 `import type` 后，**`types.ts` 的运行时依赖是零**。

**两张图各有用途，不要混用**：

| 图 | 怎么取 | **用来做什么** |
|---|---|---|
| **混合图** | ``grep -oE 'from "\./[a-z-]+\.ts"' *.ts`` | 「这个概念上依赖谁」——排**阅读顺序**、评估**改动影响面** |
| **运行时图** | ``grep -vE '^\s*import type' "$f" \| grep -E '^\s*import' \| grep -oE …`` | 「Python 里谁必须先被 import」——排**实施顺序**、找**真循环** |

**判据**：**排实施顺序看运行时图；评估改动影响看混合图。**

#### 第二面：**不可切的单元可以出现在任何粒度上**

第一反应会以为「不可切」是**文件级**的现象（SCC）。**不是**——同一个性质会在**不同粒度**上重现：

| 粒度 | 实例 | 为什么不可切 |
|---|---|---|
| **文件** | SCC 四文件 | 环 |
| **类** | `TaskScheduler`（63 方法、约 1640 行） | 所有方法读写**同一套 `#` 私有状态**——抽走一个就要连带抽走那套状态 |
| **一组共享状态的方法** | （类的进一步细分） | 同上，只是粒度更小 |

**识别方法四（**客观的否决信号**）**：**类型检查器报出一串「看不到兄弟成员」。**

当你为了「好写」而做一个重构（抽 mixin、拆对象、挪方法），
而 `mypy` 报出 `"X" has no attribute "_y"`（X 看不到**兄弟方法/私有字段**）——
**那不是写法问题，是你在切一个不该切的东西。**

**实例**：我为 `TaskScheduler` 造了一个 `_C1Mixin`，想让 `TaskScheduler(_C1Mixin)` 继承它。
**mypy 报 26 个 `"_C1Mixin" has no attribute "_load_scopes"`**——
**那 26 个错误就是「这个类共享一套私有状态」这条判据在替我说话。**

> **判据**：**重构冲动 + 一串「看不到兄弟成员」= 停手，你在切不可切的东西。**

**⚠️ 全称条件最容易写成存在条件**

**实例（`finishToolRound` 的 `terminate` 判据）**：

```
slots.length > 0
  && slots.every(slot => slot.taskId !== undefined && controls.get(slot.taskId)?.terminate === true)
```

> **「每一轮调用（含没有任务的）都必须要求终止」——不是「任一」。**

**三重条件缺一不可**，而**错法不报错**（只是**少终止了一次**）——
于是表现为「偶尔该停没停」，而你会在别处找原因。

**判据**：**写 `every` / `all` 时，回头数一遍条件**；`some` / `any` 与 `every` / `all` **在只有一个元素时结果相同**——
**测试必须用"有多个且顺序/完整性有影响"的输入。**

**⚠️ 移植含嵌套作用域的函数时，先列出所有同名但语义不同的变量**

**实例（`startToolRound`）**：外层 `const tools = (await runtime.agent(context)).tools`
是**工具定义**，内层 `const tools: TaskId[]` 是**任务 id** —— **同名**。
**JS 的块级作用域让它合法；Python 同名赋值就是覆盖。**

**判据**：**它们单独看都合理，只有对照两处定义才发现是两样东西。**
本端改名：`definitions`（工具定义）/ `task_ids`（任务 id）。

**判据一（抽走什么）**：**问「抽走它要连带抽走什么」**——
* 只抽走**代码** → 可以切；
* 要连带抽走**状态** → 那是**拆分协作对象**，属**结构偏离**，不是切片。

**判据二（数出簇调用 + 数出簇字段写入）**——**可量化**，比"看着像一块"强得多：

> **判「一个簇能不能切出来」= 数两样东西：出簇的【方法调用】**和**出簇的【字段写入】**。
> * **两者都少** → **能切**
> * 任一**多**、或**两个簇互相回指** → **不能切**

⚠️ **只数方法调用是不够的——我自己就在这上面栽过一次。**

**实证（`scheduler.ts` C3 归属图）**：C3 的出簇**方法调用只有 2 个**（都是定时器助手
`#scheduleReconcile`），于是我先判它「基本自足」。

**但数了字段写入就翻了**：

| C3 写的字段 | 次数 | 属于谁 |
|---|---:|---|
| `#runnable` | 5 | **C4/C6 的工作队列** |
| `#intent` | 3 | **C4/C6** |
| `#finalizeChecks` | 3 | **C4/C6** |
| `#waiters`（读） | 3 | **C4/C6** |

**`#track` 是那个同时维护「归属图」与「工作队列」的中枢方法。**
⇒ **C3 切出去要连带 4 个字段** → 那是**拆分状态**，属结构偏离。

#### ⚠️ 通则：**近似判据必须说明它漏掉什么**

> **判「可切 / 不可切」时，任何近似判据都必须说明它漏掉什么。**

**三次翻车的完整实证链**（同一个形态，覆盖了三个不同方向）：

| # | 近似 | **漏掉的那一半** | 实例 |
|---|---|---|---|
| 1 | 只画「**环内**」的图 | **出环的前向依赖** | `generation → tool`（`tool.ts` 不在 SCC 里） |
| 2 | 只数**出簇方法调用** | **出簇字段写入** | C3：2 次调用 vs **4 个字段** |
| 3 | 按**文件位置**判断 | **「声明在最前、实现到最后」** | `TaskRuntime`（声明在 `types.ts`、实现在 `scheduler.ts`） |

**三次的共同形态**：**用一个便宜的信号代替完整核实，而那个信号恰好覆盖了问题的一半。**

**而根本判据始终是判据一**：**「抽走它要连带抽走什么」**。
**近似判据只是它在某个方向上的投影——而投影会丢维度。**

#### ⚠️ 不可切的具体成因：**单个方法横跨两个概念**

比「63 个方法共享状态」更准确的说法是：

> **`#track` 是那个同时维护「归属图」与「工作队列」的中枢方法。**

它一次写 `#live`/`#settled`/`#below`（图）**和** `#runnable`/`#intent`/`#finalizeChecks`/`#waiters`（队列）。
**一个方法横跨两个概念时，任何按概念切的分法都会把它劈开**——而劈开它就要把它的状态也劈开。

**⇒ 判「类能不能拆」时，先找「横跨两个概念的方法」**：找到几个，就知道会劈开几处。

**⇒ 结论：`TaskScheduler` 的簇里没有一个是可单独切出来的。**
**判据一的「抽走它要连带抽走什么」比判据二更根本**——
判据二只是判据一的一个**便宜的近似**，而**近似的代价就是会漏**（漏掉的正是「字段写入」这一半）。

**实证（`scheduler.ts`，6 簇 62 方法）**：

| 簇 | 出簇调用 | 判定 |
|---|---|---|
| C3 归属图（20 方法，394 行） | **2**，且都是定时器助手 `#scheduleReconcile` | ✅ 基本自足 |
| C4 排空/预留（8 方法） | 3，全指向 C6 | ❌ 依赖 C6 |
| C6 调用执行（15 方法，557 行） | ~30，其中 `#step`/`#terminate` **回指 C4** | ❌ **与 C4 互相回指** |

**⇒ 只有 C3 能单独切出来；C4+C6 必须一起。**

⚠️ **但"能切"不等于"该切"**：**抽走一个类的一部分，会把「不变量」从局部推理变成分布式推理**。
上游把 `#live`/`#below`/`#edges` 的一致性**放在一个类里唯一维护**——
拆出去不是"多写几行"，而是**让一条本可局部推理的性质变成要跨对象协商**。
**⇒ 用真结构偏离去换"调度上的方便"，方向是错的**：**调度的困难是我们自己的问题，不该由代码结构承担。**

#### 第二面的原例：**强连通分量是不可再切的单元**

运行时图里会出现**真正的环**（第一面排除 `import type` 之后才看得见）。
ESM 靠**提升与活绑定**容忍它，**Python 不行**。

**实例（批次 D）**：`{generation, live, compaction, submissions}` 是一个 1,688 行的 SCC
（`generation ↔ live`、`generation → compaction → submissions → generation`）。
**不能按文件顺序硬排**——要么整体做，要么把**回边**改成函数内延迟导入
（判据：**那个符号是否只在函数体内使用**；若出现在默认参数/装饰器/类体就不能延迟）。

#### 第三面：**「谁需要它的形状」≠「它写在哪」** —— 两种表现

这一面被咬了**两次**，方向相反。

**表现 A：漏掉出环的前向依赖。**

处理完第二面之后容易以为「SCC 内部关系搞清了就能排序」。
**不够**：SCC 成员还有**指向环外**的前向依赖，而它们**不在环内那张图里**。

**实例**：我按 `live → generation → compaction → submissions` 排好 SCC 内部顺序，
核查时才发现 **`generation` 还有一条出环的前向依赖 `generation → tool`**，
而 `tool.ts`(911) **根本不在 SCC 里**。**方案漏了它。**

**表现 B：以为某个东西「早就做完了」。**

`TaskRuntime` **声明在 `types.ts`**（一直被我当成 L0「纯数据」），
但它是 `scheduler.ts` 与**每一个任务定义**（`ToolTask` / `GenerationTask` / `CompactionTask`）
之间的**共享契约**——所以它必须在 `tool.ts` **之前**落地，尽管它的**实现**在最后一步。

* 按**文件位置**：它在 `types.ts` → L0 → 看起来"做完了" ❌
* 按**谁需要它的形状**：它是任务体系的中枢契约 → 必须在 `tool.ts` 之前 ✅

**统一判据**：

> **排顺序看「谁需要它的形状」，不看「它写在哪」。**
> **声明位置是「阅读顺序」的线索，不是「实施顺序」的线索。**

（同一个东西可以**声明在最前面、实现到最后**——`TaskRuntime` 就是这样：
契约要早定，实现要晚写。）

#### 落位应用：**同一个原则的第三个面**（三次，结论都不是「按名字归类」能得出的）

「谁需要它的形状」不只决定**顺序**，也决定**东西住在哪个模块**。
契约必须住在**它依赖的类型之后**——而**按名字归类**会给出相反（且错误）的答案。

| 契约 | 按名字归类会放哪 | **实际落位** | 为什么不能按名字 |
|---|---|---|---|
| `Tx` | `types.py`（上游就在那） | **`session/tx.py`** | 签名引用 `DocToken`，而 `documents.py` **运行时导入** `types.py` → 放进去就循环导入 |
| `DocumentObserver` | `types.py` | **`session/tx.py`** | 同上（引用 `DocToken`） |
| `TaskRuntime` | `types.py`（上游就在那） | **`harness/runtime.py`** | 它同时要 `session/tx.py` 与 `harness/types.py` 两边，而**那两个谁都不导入谁** → 必须住第三个模块 |

**三次的结论都不是「按名字归类」能得出的**——这正是它值得单列的原因：
**归类看的是「它叫什么」，落位看的是「它依赖谁」。**

#### 三面合成一句

> **排依赖顺序 = ① 只取运行时边（第一面）→ ② 把强连通分量当成单元处理（第二面）
> → ③ 对每个单元取它的完整运行时依赖，包括出环的那些（第三面）。**
>
> 任何一步只做了前两面，都会漏东西。

### R22 ⚠️ `from __future__ import annotations` 会让 `TypedDict.__required_keys__` **恒为空**

```python
from __future__ import annotations
class A(TypedDict, total=False):
    x: Required[int]
    y: NotRequired[str]
A.__required_keys__   # frozenset()  ← 空的！
A.__optional_keys__   # frozenset({'x', 'y'})  ← 连 Required 的也在里面
```

没有 `from __future__ import annotations` 时结果是正确的 `{'x'}` / `{'y'}`。

**原因**：PEP 563 把注解留成**字符串**，`TypedDict` 在类创建时不去求值，
于是识别不出 `Required[...]` 包装。

**影响面**：**只影响运行期自省**。mypy **不受影响**（它会求值字符串注解），
所以 CI 是绿的、类型是收窄的——但任何**运行期**读必填性的代码
（校验器、JSON Schema 生成、测试断言）都会读到**错的答案**。

**做法**：运行期要判必填性，用
``get_type_hints(Td, include_extras=True)`` 再看有没有 ``Required`` / ``NotRequired`` 包装。

**抓到它的测试**：``test_r22_future_annotations_defeat_required_keys``
（先断言 ``__required_keys__`` **是空的**——把这个坑本身钉住——再断言正确的读法）。

#### 第二次生效（批次 D）

给 ``ConversationRetryPolicy.maxAgentDelayMs`` 加 ``NotRequired`` 之后，``__required_keys__``
**立刻变得不可靠**——同一个模块里，没有 ``NotRequired`` 的 ``CompactionPolicy`` 属性是对的，
加了的那一个就不对了。**第二次生效比第一次更有说服力：它说明这不是一次性陷阱。**

解法同上：``get_type_hints(Td, include_extras=True)`` 再看有没有 ``Required`` / ``NotRequired`` 包装。

### R23 ⚠️ 「no-op 的开关」要自己补上它本该做的事，并写清**为什么**它是 no-op

chord 的 ``CopyJsonOptions(omit_undefined_properties=True)`` **刻意是 no-op**：
JS 只丢 ``undefined``、**保留 ``null``**；Python 的 ``None`` 同时表示两者。
真去「删掉值为 ``None`` 的键」会把**合法的 JSON ``null`` 一起删掉**——那是语义分叉。

**做法**：**在构造处直接不写那个键**，而不是指望开关删。
本包两处都已经这么做：``session/transaction.py`` 的 ``head``/``byTaskId``
（有测试断言 ``"head" not in first``），以及 ``harness/usage.py`` 的
:func:`~pi_durable.harness.usage.usage_json`（可选计数器为 ``None`` 时整个键不写）。

**推论**：一个**被刻意做成 no-op** 的开关是**文档**，不是实现——
看到它就要去问「它本该做什么、为什么不做」，然后把该做的事放到调用方。

### R24 ⚠️ chord 的 `Draft` **递归**包装每个嵌套容器，而 `copy_json` **拒绝** Draft

```python
draft = await tx.doc(InboxDoc, 1)
draft["items"].append({"id": 1, "content": [{"text": "s"}]})
type(draft["items"][0]["content"])   # Draft  ← 不是 list！
copy_json(draft["items"][0]["content"])
# TypeError: Value contains a non-JSON Draft; expected strict JSON
```

`copy_json` 要的是**严格 JSON**，它**不会**替你展开 Draft。取嵌套值必须显式
``.to_json()``（递归展开）。

**JS 没有这一步**：上游的 ``copyJson`` 顺着 Proxy 自然展开，所以 ``inbox.ts`` 里
``item.content`` / ``item.entry`` 直接能用。

#### 这条通则的**存在理由**：为什么内存后端不报

| 后端 | 表现 |
|---|---|
| **SQLite** | ``json.dumps`` 抛 ``Object of type Draft is not JSON serializable`` ← **暴露** |
| **内存** | 只做 ``copy.deepcopy``，Draft 是合法对象，**一路通过** ← **不暴露** |

所以**「两个后端都跑」在这里不是形式，是唯一的暴露途径**。
同类问题（序列化边界上的类型泄漏）都应该按这条排查：**只要某个后端会真的序列化，
它就能看见另一个后端看不见的东西。**

#### 可操作的判据

> **每一处「从草稿读出嵌套值再交给别处」都要补 ``to_json()``。**

本包在 ``harness/inbox.py`` 里放了一个 :func:`~pi_durable.harness.inbox._plain`
（有 ``to_json`` 就调，否则原样返回），在三个交接点用上它：
``apply_boundary`` 的 ``item["entry"]`` 与 ``item["content"]``、
``has_queued_reset`` 的 ``item["entry"]``。

**排查口诀**：看到 ``draft[...][...]``（**两层以上**下标）的结果要传出去，就先问
「它现在是什么类型」。

### R25 ⚠️ 「按 mode 选择」与「按位置放置」是两件事

``apply_boundary`` 先按 mode 挑出下标，然后 **``sort`` 一遍**再放置
（``inbox.ts:96-97``）。所以**放置顺序是收件箱位置顺序**，
而**不是**「steer 一律在 follow-up 前面」。

我按直觉写成「steer 先」，测试红了才发现。**看代码里的 ``sort`` 就要问它在排什么。**

### R26 ⚠️ 条目里的消息是 **JSON dict**，不是 pydantic 对象

条目从存储读回来就是 **JSON**，所以 ``entry["model"]`` 里是普通 dict
（``{"role": "user", "content": [...]}``）。上游 TS 里 JSON 对象**就是** ``Message``，
所以它直接写 ``message.role``；Python 侧必须按 **dict** 取。

两种形状的**字段名都是 snake_case**（pi_ai 的 ``model_dump()`` 输出与属性名一致：
``stop_reason`` / ``tool_call_id`` / ``tool_name`` / ``is_error``），所以代价只是
「dict 取还是属性取」。

**做法**：本包在 ``harness/context.py`` 放了一个 :func:`~pi_durable.harness.context._field`
（dict 就 ``.get``，否则 ``getattr``），**同时接受两种形状**——因为本地刚构造的
pydantic 对象与刚从存储读回的 dict 会混在同一条流水线上。

**并且：合成出来的消息也必须是 dict**（:func:`~pi_durable.harness.context.missing_result`）。
上游那边「普通对象就是 ``ToolResultMessage``」，Python 侧若返回 pydantic 对象，
下游会拿到**混着两种形状**的列表。

**同类判据（把 R26/R27 与批次 B 串成同一条）**：
**凡是「从存储读出记录 / 跨包传递对象 → 按 TS 接口字段访问」的地方，
Python 都要先确定它是 dict 还是对象，再选对取法。**

批次 B 的记录选 ``TypedDict``（运行期就是 dict）正是同一个理由。

#### 第二种情形：**跨包传递**（不只从存储读）

同一个坑有两条来路：

| 来路 | 实例 | 症状 |
|---|---|---|
| **从存储读回** | ``entry["model"]`` 里是 JSON dict | 代码写了 ``message.role`` → ``AttributeError`` |
| **跨包传递**（pi_ai ↔ pi_durable） | 给 ``pi_ai.transcript.get_current_tools()`` 传 camelCase 的 **dict** | **静默返回空列表**——不报错，只是结果少一项 |

**判据**：拿到一个「按 TS 接口描述」的值时，先问**它是 dict 还是对象**。
本包的做法是 ``_field()`` 两种都认；但**传给别的包的函数时不能"两种都认"**——
对方的实现固定按属性（或按键）读，只能传对形状。

#### 两个容易写错的语义细节（施工时各红过一次）

**① 孤儿工具结果是被「丢弃」，不是被「接上」。**

```python
messages = [a1(calls=[c1]), a2, result(c1)]
```

``result(c1)`` **没被任何助手看到**——``order_tool_results`` 只在「下一条助手消息之前」
找结果，而它落在第二条助手之后。所以：``a1`` 的 ``c1`` 算**缺失**（合成一条），
而那条真实结果作为**孤儿被丢掉**。最终是 **1** 条 toolResult，不是 2 条。
**判据**：结果只对「它前面最近的那条助手」有意义。

**② `find_baseline` 的返回值是**三态**，不能压成两态。**

上游 ``Baseline = Message | null | undefined``：

* ``Message`` —— 找到了基线；
* ``null`` —— **不可能**有基线（已经见过一条 system 消息，或范围内就没有 ``pi.system`` 条目）；
* ``undefined`` —— **还没有定论**，更晚的条目可能才是基线。

本包用 ``_NO_BASELINE`` 哨兵对应 ``undefined``、``None`` 对应 ``null``，
``extend_range`` 里按 ``is not _NO_BASELINE`` 决定「沿用上一个基线」还是「在新条目里再找」。
**把 ``undefined`` 并进 ``null`` 会让增量读提前放弃找基线**——
一眼看不出错，只会在「先追加普通条目、再追加 system 条目」的序列上表现成基线丢失。

### R27 ⚠️ JSON 往返之后不能用 `is` 比对象身份

``lead_with_system`` 要判断「这条 system 消息是不是基线」。上游写 ``system !== baseline``（引用比较）。
但 Python 侧 ``baseline`` 与 ``messages`` 里那条可能是**两份相等的 dict**——
``is not`` 会判成「不同」，于是**基线被误判为非基线**，在它前面有助手消息时就不会被提到最前。

**做法**：``system is not baseline and system != baseline``——先试身份，再比值。
（本包在 ``context.py`` 里就是这么写的。）

### R28 ⚠️ 「不为未被消费的部分猜形状」**≠**「已知形状也要截断」

窄提取原则的**目的**是「**不要猜形状**」。所以判断标准不是「有多少消费者」，
而是「**这个形状是否已知**」：

| 情形 | 做法 |
|---|---|
| 形状**未知**（要靠实现才能定） | **不写**。先做实现，形状随之定下来 |
| 形状**已知**（上游有完整定义），只是当前消费者只用一部分 | **按完整形状写**。截断会得到一个**会被下一个消费者推翻**的形状 |

#### R28 是**两条判据的顺序检查**（不是"形状已知就写"）

1. **形状已知吗？** 未知 → **不写**（**情形一**）。先做实现，形状随之定下来。
2. **这门语言能表达它吗？** 不能 → **不写**（**情形三**）。
3. 两条都过 → **按完整形状写**（**情形二**）。

**实例（情形二，批次 D）**：``RegistrySnapshot`` 的完整形状是已知的（``types.ts:377-387``，6 个方法），
而 ``agent.ts`` 只用 2 个。**按 6 个写**——只写 2 个会得到一个 ``registry.py`` 必然推翻的形状，
那才是这条原则想避免的东西。

#### 🆕 情形三：**形状已知，但这门语言里没有对应物可表达**

> **「形状已知」是「定义它」的**必要**条件，不是**充分**条件。**

强行造出来得到的是「**看起来更贴合上游、实际不提供任何保证**」的别名——
**正是 R23 那类刻意的 no-op 的近亲**（看起来支持、实际只支持一部分）。

**首例：`SessionDocToken<T>` / `ConversationDocToken<T>`（批次 D）**

上游靠**类型参数**把 scope 编进类型：
``DocToken<T, CommonDocDefinition<T> & {scope: "session"}>``。
本包的 ``DocToken`` 把 scope 放在 ``definition`` 里、由 ``check_record_scope()`` 在**运行期**校验。

⇒ **Python 里没有对应物**。造 ``SessionDocToken = DocToken`` 这样的别名**不提供任何静态保证**，
只会让后人以为「scope 在类型上被检查了」。

**对照**：同一个闭包里，``TypedEntry<D>`` 走的是**另一条路**——给**别名 + 说明**
（``TypedEntry = EntryRecord``），并把收窄明确交给运行期的 ``EntryToken.is_()`` / ``as_typed()``。
**区别在于：``TypedEntry`` 的别名文档说清了"保证在运行期"，而 scope 别名会让人误以为保证在类型上。**

反之，``Settings`` / ``HarnessSettings`` / ``PromptSection`` 是**彼此咬合**的一组，
单独抽一个出来定形状就是猜——**等它们一起做**。

### R29 ⚠️ JS 的「有值 / `null` / `undefined`」是**三态**，Python 里不能压成两态

上游很多地方靠这三态区分「改掉 / 清掉 / 不动」或「有 / 不可能有 / 还没有定论」。
Python 的 ``None`` 只表示一个值，**压成两态会静默丢语义**。

| 上游三态 | 语义 | 本包做法 | 实例 |
|---|---|---|---|
| 有值 / **`null`** / **`undefined`** | 替换 / **清除** / **不改** | 「不设该键」= ``undefined``；``None`` = ``null`` | ``AgentChange`` |
| ``Message`` / **`null`** / **`undefined`** | 找到 / **不可能有** / **还没有定论** | ``_NO_BASELINE`` 哨兵 = ``undefined``；``None`` = ``null`` | ``find_baseline`` |

#### R29 的第二面：**断言也不能把三态压成两态**

R29 原来讲**实现**。它同样适用于**测试**：

```python
assert result.is_error is False   # ❌ 它实际是 None（上游的 undefined）
assert result.is_error is not True  # ✅ 「未设」不是「假」
```

**失败方式特别像"实现错误"**：`assert x is False` 在 `x is None` 时**红**，
于是你会去查实现——而**实现是对的、断言是错的**。**这次伪装得最像，因为断言看起来完全合理。**

**判据**：**当一个字段的默认值是 ``None``/``undefined`` 时，断言写「它不是 True」，不要写「它是 False」。**

**两种哨兵做法**：① 用「键存不存在」表达 ``undefined``（``AgentChange``）；
② 用一个模块级哨兵对象表达 ``undefined``（``find_baseline`` 的 ``_NO_BASELINE``）。

**失败后果**：``AgentChange`` 压成两态 → 「**清除**」变成「**不改**」；
``find_baseline`` 压成两态 → 增量读**提前放弃找基线**（只会在「先追加普通条目、再追加 system 条目」
的序列上表现成基线丢失）。**都一眼看不出错。**

### R30 判必填 / 可选**以类型签名为准，不以注释为准**

``ConversationRetryPolicy`` 的 ``maxAgentDelayMs`` 上游签名是 ``maxAgentDelayMs?: number``，
但注释里只是顺带提到。我第一版按注释判成必填，**测试红了才改**。

**上游「可选性只体现在注释里」的情况不少见**——判之前先看**类型签名**（``?`` / ``| undefined``），
注释只用来理解含义。

### R31 ⚠️ chord 的 `Draft` 不是 `list`/`dict`：`isinstance`、`in`、`set()` **三处都会错**

`Tx.doc()` 返回的是 chord 的 **`Draft`**（一个包装对象/树），不是普通容器。
JS 侧没有这个问题——``Draft`` 是 **Proxy**，`Array.isArray` / `in` / `Set` 对它的行为
与对真实数组一致。Python 侧必须逐处适配：

| 操作 | JS（Proxy） | Python（Draft） | 正确做法 |
|---|---|---|---|
| `Array.isArray(draft.items)` | ✅ 数组上是 `true` | ❌ `isinstance(draft, list)` 是 `False` | 再看 ``draft.is_list`` |
| `name in draft.items`（**值**查找，`Array.includes`） | ✅ 值查找 | ❌ `__contains__` 对列表做的是**下标记**检查（`0 in arr` → True、`"read" in arr` → False）——**这是忠实镜像 JS 的 `in`** | 先 `list(draft)` 再判断 |
| `new Set([...draftItems])` | ✅ 引用身份 | ❌ 值相等，且 `ToolRegistration` 含 `parameters: dict` → `TypeError: unhashable type: 'dict'` | 用 `id(x)` 建集合 |

**三处我各踩了一次，症状各不相同**：

* 漏 `is_list` → 数组形状被当成对象形状，报 `TypeError: list indices must be integers, not str`；
* 用 `in` 做值查找 → **不报错**，只是「已存在的名字」被当成不存在，结果**多追加了一次**；
* 用 `set()` → `TypeError: unhashable type: 'dict'`。

⚠️ **第二处最危险：它静默出错**。批次 D 的 `add_tools` 就是这么把 `read` 追加了两遍。

**判据**：**凡是把 `Tx.doc()` 的返回值当容器用，先问「我要的是下标语义还是值语义」**，
并按上表选对写法。

#### 按**症状**索引：本仓至今的「静默出错」清单

按机制归类不好查——**调试时你看到的是症状**。所以这里按症状列：

| 症状 | 机制 | 实例 |
|---|---|---|
| **结果多了一项**（重复追加） | `Draft` 的 `in` 是**下标记**检查，不是值查找 | ``add_tools`` 把 `read`` 追加了两遍（R31） |
| **结果少了一项** | 跨包传了 dict，对方按**属性**读 | ``get_current_tools()`` 收到 camelCase dict → **空列表**（R26） |
| **结果少了一项** | 把 ``undefined`` 并进 ``null`` | ``find_baseline`` 提前放弃找基线（R29） |
| **值没被清掉** | 三态压成两态 | ``AgentChange`` 的「清除」退化成「不改」（R29） |
| **顺序悄悄不对** | ``Map.set`` 更新已存在的键**保持原位**，不该「先删再赋」 | ``replay_sections``（会改变 ``plan_sections`` 的全删重加判断） |
| **成员/能力少了几项** | **漏了 `extends` 的父接口** | ``TaskRuntime`` 漏 `DocumentObserver`/`DocumentReader` → `watch_doc`/`snapshot` 全没继承到 |

#### R24 的后续实证（批次 D，共 4 个）

| # | 位置 | 症状 | 机制 |
|---|---|---|---|
| 1 | `inbox.apply_boundary` | sqlite 的 `json.dumps` 抛 `Object of type Draft is not JSON serializable` | `Draft` 是包装对象 |
| 2 | `tool.nested_result` | `copy_json` 抛 `Value contains a non-JSON ToolDiagnostic` | **`ToolDiagnostic` 是 dataclass** |
| 3 | `tool.append_tool_result`（`data`） | 同上 | 要进存储的东西必须是严格 JSON |
| 4 | `tool.append_tool_result`（`model`） | **只有 sqlite** 抛 `TypeError` | **pydantic 对象不是 JSON** |

**#2–#4 都是「不是 dict 就别想进 JSON」这同一件事**，
提炼出的做法：新增 :func:`~pi_durable.harness.tool.diagnostic_json`（dataclass → 严格 JSON），
消息用 ``model_dump(exclude_none=True)``。

**四个实证里，内存后端全部一路通过。**
**⇒ 「两个后端都跑」不是形式，是唯一能让它们暴露的途径。**

**共同点不是机制，是症状**：**不报错、结果少一项/多一项/顺序不对**。
所以**两个后端都跑**、**断言"没有副作用"**（如「第二次一次提交都没有」）、
**互印证的正反两条测试**（如 R32 的分叉语义）是对付它们的通用手段。

### R32 ⚠️ 同族文档的分叉语义**故意不一致**，不要「统一」

四个 ``pi.*`` 文档里有三个是 ``fork: "initial"``、一个是 ``fork: "asOf"``：

| 文档 | 分叉语义 | 为什么 |
|---|---|---|
| ``pi.provider`` | ``initial`` | 沿用父的身份会让提供方把两次会话**认成同一个** |
| ``pi.usage`` | ``initial`` | 子会话**不该背**父会话的花费 |
| ``pi.inbox`` | ``initial`` | 排队输入属于**那个**对话，不该被复制过去再放一遍 |
| **``pi.agent``** | **``asOf``** | 子会话**本该继承**父的模型/扩展/工具选择，否则每次分叉都要重选 |

**不要为了「一致性」把 ``pi.agent`` 改成 ``initial``**。
本包有两条**互相印证**的测试：一条钉住 ``pi.agent`` 是 ``asOf`` 并列出同族对照表，
另一条断言同族另外三个都是 ``initial``。

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| 1 | **原生文件监听**：`fs.watch()` + 每目录一个 watcher，Windows 与不可靠文件系统回退到轮询（`env/node-watch.ts:182-186`、`412-467`） | **只有纯轮询**（`pi_durable.env.watch.NodeFileWatcher`，`mode` 恒为 `"polling"`） | Python 标准库没有 `fs.watch()` 对等物。详见下节 |
| 2 | `AbortSignal` / `AbortController` 由运行时提供（`chord/src/context/index.ts:15-17,83-104,110-128`） | 在 `pi_chord.context` 内自带一份最小实现 | Python 没有等价物；语义按上游实际用到的部分实现（`aborted`/`reason`/`add_listener`/`remove_listener`/`AbortSignal.any`） |
| 3 | `Promise<T>` + `await` | `async def` + `await` | Python 无「返回挂起值」的普通函数，故 `awaitWithContext(promise, ctx)` → `await await_with_context(awaitable, ctx)` |
| 4 | 联合类型 `{paths} \| {overflow:true} \| {error}`、`{ok:true,value} \| {ok:false,error}` | 冻结 dataclass：`WatchPaths`/`WatchOverflow`/`WatchError`、`Ok`/`Err` | Python 无结构化联合；用 `isinstance` 判别，`Result` 仍是泛型别名 |
| 5 | 公开字段 camelCase（`mtimeMs`、`exitCode`、`spillPath`、`onChange`…） | snake_case（`mtime_ms`、`exit_code`、`spill_path`、`on_change`…） | PEP 8；类名保持上游拼写（`NodeExecutionEnv`、`NodeFileWatcher`、`LineScanner`） |
| 6 | 对象字面量参数（`{startLine, endLine}`、`{maxLines}`、`{noFollow}`…） | 冻结 dataclass（`LineRangeOptions`、`TextLinesOptions`、`OpenBinaryReaderOptions`…） | 保留字段名与默认值；比 `dict` 可被 mypy strict 校验 |
| 7 | `node:fs/promises` | `os` / `pathlib` / `aiofiles` 无——直接用 `os.*` + `asyncio` | 标准库对等 |
| 8 | `node:child_process.spawn(..., {detached})` + `process.kill(-pid)`（`env/node.ts:803-809`、`274-303`） | `asyncio.create_subprocess_exec(..., start_new_session=True)` + `os.killpg` | 进程组语义一致 |
| 9 | `node:crypto` 的 `randomUUID` / `createHash("sha256")` | `uuid.uuid4()` / `hashlib.sha256` | 标准库对等 |
| 10 | 溢出日志用 `createWriteStream` + `drain` 背压（`env/node.ts:741-800`） | 普通文件对象同步写入（无背压暂停） | Python 的 `asyncio` 子进程管道没有等价的「暂停/恢复」；同步写入本地临时文件比子进程产出更快。已登记 `ASYNC230` per-file 豁免 |
| 11 | `TextDecoder` / `TextEncoder` | `codecs` 增量解码（`errors="replace"`）+ `str.encode` | WHATWG 语义：非法序列 → U+FFFD；解码出的文本长度与上游一致 |
| 12 | `TextEncoder` 把孤立代理项写成 U+FFFD | 写路径用 `errors="replace"` 编码 | 保持「文件操作不抛异常」的契约 |
| 13 | `runner.ts` 适配 Vitest/Jest（`describe`/`it`/`expect`） | 适配 pytest（`pytest.mark.parametrize` + `env_conformance_cases()`） | 运行器不同；用例集合、名字、超时（watch 用例 30 s）与上游逐条一致 |
| 14 | `LineScan` 类型声明在 `env/index.ts:141-157`，`line-scan.ts` 只做 `import type` | `LineScan` 定义在 `pi_durable.env.line_scan`，由 `pi_durable.env` 再导出 | Python 的运行时构造无法引用「正在初始化的包」；公开导入路径不变 |
| 15 | 版本随上游（`durable/package.json` = 1.3.0） | 本包 `1.3.0`，与仓库其余包（`0.85.1`）不同 | 本包直接镜像 v1.3.0 源码，没有 0.85.1 对应物；已在汇报中作为开放问题提出 |
| 16 | `node:fs/promises` 是**真异步** I/O（`await readFile`…），`env/node.ts` 全程 `async` | async 方法内直接调用 `os`/`open`/`os.scandir` 等**同步** stdlib | Python 的 stdlib 没有与 `fs/promises` 对等的可等待文件 API；与 `pi-coding-agent/tools.py` 的既有取舍一致（工具执行快，不值得 `asyncio.to_thread`）。真正需要并发的地方（子进程 I/O、定时器、等待）仍然是异步的。`uv run ruff check --select ASYNC packages/pi-durable/src packages/pi-chord/src` 全绿，因此**不需要**登记 per-file 豁免 |
| 17 | `findBashOnPath()` 起 `which`/`where` 子进程找 bash（`env/node.ts:192-200`） | `shutil.which("bash")` | 结果等价，省一次进程；`/bin/bash` 优先与候选顺序保持上游一致（`env/node.ts:217-259`） |
| 18 | `fs.rm(path, {recursive:false})` 对目录抛 `ERR_FS_EISDIR`，该 code 不在映射表内 → 归为 `unknown`（`env/node.ts:118-141`、`1179-1193`） | 显式判定目录 → `FileError("is_directory", …)` | 上游的码表里本来就有 `is_directory` 这一项，且它在各平台必须一致：macOS 上 `unlink(dir)` 返回 EPERM 而不是 EISDIR。语义更明确，且不依赖平台 |
| 19 | `waitForChildProcess` 的「退出后 100 ms 空闲即收尾」+ spill 背压联动（`env/node.ts:305-384`） | `asyncio.wait` 的空闲超时收尾（`_drain_streams`） | 语义相同：子进程退出后最多再等 `EXIT_STDIO_GRACE_MS`（100 ms）无新数据就收尾，避免继承了管道的孙进程把 `exec` 拖住。背压联动随第 10 条一起去掉 |
| 20 | `Result<TValue, TError>` 用判别联合 | `Ok[TValue] \| Err[TError]` 冻结 dataclass，参数**协变** | 协变让 `Result[ShellExecResult, ExecutionError]` 同时是 `Result[object, BaseException]`，conformance 的 `errorCode()` 这类helper 才能统一签名（见 `env/__init__.py` 注释） |

### 批次 C 的偏离

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| 21 | `utf8ByteLength` 有两条路径：有 `Buffer` 时走 `Buffer.byteLength`，否则手写 UTF-16 码元循环（`chord/src/output.ts:272-294`） | 只有一条：:func:`pi_chord.output.utf8_byte_length` 的手写循环 | Node 的 `Buffer` 是运行时全局，Python 无对应分支。上游那条测试（`durable/test/env-truncate.test.ts:23-45`）专门断言**无 Buffer** 路径与 `TextEncoder` 结果一致，正是本包采用的那条 |
| 22 | `toBase64` 按 8 KiB 分块构建二进制字符串再 `btoa`（`tools/image-processor.ts:46-53`），为绕开 JS 调用栈与 Durable Object 128 MB 堆上限（`CHANGELOG.md:16`，issue #10923） | :func:`base64.b64encode` 一次到位 | `b64encode` 直接吃 `bytes`，没有这两个约束；**输出逐字节相同** |
| 23 | `formatSize` 用 JS `toFixed(1)`（`truncate.ts:64-66`） | :func:`_js_to_fixed_1` 用 `Decimal(float).quantize(ROUND_HALF_UP)` | JS 规范规定并列时「取更大的 n」（十进制四舍五入），Python `f"{x:.1f}"` 是二进制 round-half-even。二者在 `x=1.25` 这类可精确表示的并列点分叉：JS `"1.3"` vs Python `"1.2"`。`Decimal(float)` 取 double 的精确十进制值后可逐位对齐 |
| 24 | `truncateHeadOf` 读 `lines[0]`（`truncate.ts:120`） | `lines[0] if lines else 0` | 契约（`truncate.ts:86-89`）要求 `prefix` 是整段或足够长，所以合法调用不会走到空 `lines`。JS 在契约外会抛 `TypeError`；Python 侧取「无首行 = 0 字节」，行为更明确且不改变合法调用的结果 |
| 25 | `file-mutation-queue.ts` 用 `Promise` 链（`previous.then(() => done)`，`55:41-47`） | `asyncio.Future` 作槽位，`finally` 里 `set_result` + 条件删除 | 语义相同：先占槽位再 await（中间无挂起点，故无插队）；`done` 一定 resolve，故链条不会 reject。删除前比对身份，避免删掉后来者的槽位 |
| 26 | `path-utils.ts` 用 `new Set(variants)` 去重且保序（`path-utils.ts:26`） | `dict.fromkeys(variants)` | 两者都保首次出现顺序；Python 3.7+ dict 有序 |
| 27 | `image.ts` 全程用 `bytes[index] ?? 0`（越界 → 0） | 显式 `_byte_at(data, index)` 辅助函数 | **必要**而非风格：Python 的负下标是「从尾部数」，直接下标会得到完全不同的结果（如 `bytes[-1]`）。上游依赖 `undefined ?? 0` |
| 28 | `image_dimensions` 返回 `{width, height} \| undefined`（`image.ts:79-82`） | 返回 `tuple[int, int] \| None` | Python 无结构化联合；元组够用且不需要新类型 |
| 29 | `exif.ts` 的 `u32` 用 `>>> 0` 归一到无符号 32 位（`exif.ts:10-13`） | 按位与 `& 0xFFFFFFFF` | Python 整数无上限，掩码即等价语义 |
| 30 | `tool-search/tool-search.ts:63` 的 `execute` 回调 | **未移植** | 依赖 `harness/define.ts` 的 `defineTool`（值依赖）与 `harness/types.ts` 的 `ToolRegistration`；属批次 D |
| 31 | `images/index.ts` 的 `createPhotonImages(wasm)`（Photon = Rust→WASM） | :func:`pi_durable.images.create_pillow_images()`，Pillow 实现，**无参数** | 已获 Lead 批准作为**可选 extra** `pi-py-durable[images]`。上游要先 `initSync({module: wasm})` 把 WASM 装进进程（`images/index.ts:33`），Pillow 是普通扩展模块，没有这一步。惰性导入：缺 extra 时 `import pi_durable.images` 仍可用，调用 processor 才抛带安装提示的 `ImportError`（`images/index.py`） |
| 32 | `createPhotonImages` 在同一进程里复用第一次的 `wasm`（`images/index.ts:22`） | 无对应状态 | Pillow 没有「初始化 + 复用」这一步；每次 :func:`require_pillow` 拿到的都是同一个已加载模块（Python 的 import 缓存天然做到这点） |
| 33 | 把图像编码成 JPEG 时 Photon 的 `get_bytes_jpeg(quality)`（`images/index.ts:107`） | `PIL.Image.save(format="JPEG", quality=q)`，非 RGB/L 模式先 `convert("RGB")` | JPEG 无 alpha 通道；Photon 内部同样要落 RGB。**编码器不同（Photon vs libjpeg-turbo），所以字节不会相同**——这是行为等价而非字节等价，属已知项，见〈批次 C 已知差异〉 |
| 34 | 5..8 方向用**手写像素搬运**（`images/index.ts:119-139`） | Pillow 的 `Image.Transpose.*` 标准转置 | 上游手写是因为 Photon 的 `rotate` 在直角旋转时会掉像素（`images/index.ts:120-121`）。Pillow 的转置无损，无需绕开。映射为 5→TRANSPOSE、6→ROTATE_270、7→TRANSVERSE、8→ROTATE_90，已用**逐像素**用例对齐上游公式 |
| 35 | `Math.round` 用于目标尺寸（`images/index.ts:66`） | `_js_round` = `math.floor(value + 0.5)` | JS 的 `Math.round` 在 `.5` 处向 `+Infinity` 取整，Python `round` 是 round-half-even |
| 36 | `images/node.ts` + `images/node-worker.ts` 把 Photon 放进 `worker_threads`，空闲 30 s 退出以释放不会收缩的 WASM 内存（`images/node.ts:15-18`、`115-123`） | **未移植** | Python 侧若需要「不阻塞事件循环」应对应线程池/进程池，而不是移植 worker 消息协议（`PrepareRequest`/`PrepareReply`）。属批次 C 未完成项 |

### 批次 D-1 的偏离

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| 37 | **`ToolExecutionApi` 是 ``harness/types.ts:205-292`` 的 20+ 成员接口**，并继承 `DocumentObserver` / `DocumentReader`（`harness/types.ts:205`） | **只声明 C₂ 实测用到的 7 个成员**：`env`、`models`、`output_window`、`output`、`retained_output`、`diagnostic`、`agent` | **这是本端最重要的偏离**：本端 `ToolExecutionApi` 是**窄接口，不是上游那个类型**。判定依据是实测 `grep -rnoE 'api\\.[a-zA-Z_]+' tools/ tool-search/` → 12 处 / 7 个成员 / 无解构。**省略的 12 个成员**（`taskId`、`conversationId`、`callId`、`registry`、`details`、`commit`、`memo`、`createTask`、`getTask`、`waitForTask`、`conversation`、`executeTool`）与它继承的两个 trait 全部依赖**批次 B 的 L0 内核**（`Tx` / `Task` / `TaskRecord` / `ConversationHandle` / `DocumentObserver` / `DocumentReader`），本段无法也不应实现。省略项已在 `harness/types.py` 的模块文档逐条列出 |
| 38 | `ToolExecutionApi<TDetails extends JsonValue>` 带泛型（`harness/types.ts:205`），参数只被 `details(value: TDetails, …)` 使用（`harness/types.ts:286`） | **不带泛型** | `details()` 在本段省略，泛型参数就没有使用者。留一个没人用的类型参数只会误导。批次 D 补回 `details()` 时一并恢复泛型 |
| 39 | `ToolRegistration<TParameters, TDetails> = Tool<TParameters> & {…}` —— **继承 pi-ai 的 `Tool`**（`harness/types.ts:293-297`） | 冻结 dataclass，把继承**摊平**成 `name` / `description` / `parameters` 三个同名字段 | pi-ai 的 `Tool` 是 pydantic 模型，而本类型要携带可调用的 `execute`。字段与语义完全一致，只是不再共用基类。**注意**：pi-ai 的 `Tool.to_json_schema()` 会去掉顶层 `title`，上线给模型时仍需经它转换 |
| 40 | `TSchema`（typebox）+ `Static<TParameters>` 在类型层提取 args 类型（`harness/types.ts:294`、`308`） | `parameters: Mapping[str, Any]`（JSON Schema）+ 无约束 `TypeVar` `TArgs` | Python 没有类型级提取。`TArgs` 由工具自己声明（冻结 dataclass / pydantic / TypedDict 皆可），mypy 从 `execute` 的注解推断——已用正反两个类型探针验证（正向通过、把字段用错时报 `attr-defined`）。**代价**：JSON Schema 与 `TArgs` 是两个来源，需工具作者保持一致；用 pydantic 的 `model_json_schema()` 可消除这个风险 |
| 41 | `defineTool()` / `defineExtension()` 是**恒等函数**（`harness/define.ts:6-17`，`return tool` / `return extension`），存在只为 TS 推断泛型 | **关键字构造器** `define_tool(...)` / `define_extension(...)` | Python 没有这个推断需要，写成构造器让函数有实事可做（构造 + 默认值）。调用形状与上游「传具名字段对象字面量」一一对应，C₂ 调用点保持视觉平行 |
| 42 | `harness/define.ts` 共 6 个函数（44 行） | 只移植 `defineTool` / `defineExtension` | 其余四个需要批次 B 的 L0 内核：`hook()`（`define.ts:29-31`）→ `HooksOf` → `Task`；`section()`（`20-26`）、`wrapTool()`（`34-36`）、`wrapSection()`（`39-44`）→ `PromptSection` → `PromptInput` → `DocumentReader` |
| 43 | `Extension` 有 `name` / `tools` / `sections` / `hooks` / `wraps` / `tasks` 六个字段（`harness/types.ts:365-375`） | 只有 `name` / `tools` | `sections` → `PromptSection`、`hooks` → `HookRegistration`、`wraps` → `Wrap`、`tasks` → `AnyTask`；前三个会拖进 L0 内核。C₂ 的两处调用（`tools/index.ts:31-36`、`tool-search.ts:90-92`）都只用 `name` + `tools` |
| 44 | `Agent` 有 9 个字段（`harness/types.ts:447-466`） | 只有 `model` / `tools` / `callable` / `deferred` | 省略 `thinkingLevel` / `extensions` / `sections` / `instructions` / `cwd`：`sections` 需要 `PromptSection`，`extensions` 需要完整的 `Extension`。C₂ 只读 `model`（`tools/read.ts:201`）与 `deferred`（`tool-search.ts:67`）；`tools`/`callable` 是 `ToolRegistration` 序列，不引入新依赖，故一并保留 |
| 45 | `Models` 是 pi-ai 的接口，有 8 个方法（`harness/types.ts:215`） | `Protocol`，**只声明 `get_model`** | C₂ 只用到 `api.models.getModel(provider, modelId)`（`tools/read.ts:202`）。其余 7 个（generation / compaction 用）随批次 D 后续一起加 |
| 46 | pi-ai 的 `ToolResultMessage["content"]`（`Usage` / `TextContent` / `ImageContent`） | 直接复用 pi-ai 的 `TextContent` / `ImageContent` / `Usage` | **本段为此新增了 `pi-py-ai` 依赖**，与上游一致：`durable/package.json:60-67` 把 `@earendil-works/pi-ai` 列为运行时依赖，`harness/types.ts:2-16` 从它引入 `Tool` / `Usage` / `ToolResultMessage`。经 Lead 批准（方案 A） |

### 1. node-watch：为什么只有纯轮询

先看上游的真实语义需求（`env/node-watch.ts`）：

* 类的文档已经写明**它本来就是快照差分**：原生事件只是触发一次去抖重扫，「改动是两次
  快照之差加上事件路径」，所以替换文件、重命名/重建祖先、整目录连内容一起出现，
  都不依赖操作系统发出哪些事件（`node-watch.ts:133-138`、`284-299`、`301-309`）。
* `polling` 模式是上游**一等公民**，有完整的独立实现：`#schedulePoll`（`228-234`）、
  `#scan`（`317-405`）、`#diff`（`301-309`）、`#reported`（`311-315`）。
* 轮询额外做一件事以对抗时间戳粒度：最近 5 s 内、≤256 KiB 的普通文件再比一次内容
  sha256（`node-watch.ts:54-58`、`334-345`）。**本包保留了这个哈希比较**。

因此本包把上游 `mode: "polling"` 这条路径完整移植，并让它成为唯一路径。取舍：

* **得到**：无第三方依赖（`watchfiles`/`watchdog` 都不需要）；与上游轮询模式语义逐条相同
  （含排除规则、目录预算、祖先身份、`#reported` 归属规则）。
* **失去**：延迟从「原生事件（约 2 s 内）」变成「一个轮询周期（默认 2000 ms，上游
  `DEFAULT_POLL_MS`）」。`FileWatcher.mode` 恒为 `"polling"`，调用方若据此切换策略会
  看到不同分支——这是**唯一对调用方可见的行为差异**。测试用例用 25 ms 间隔以免每个用例
  干等 2 s；**出厂默认仍是 2000 ms**，并已用默认值单独跑过全部 9 个 watch 用例（全部通过）。
* **不再需要**（随原生路径一起去掉，均有上游行号）：`FSEVENTS_SETTLE_MS` 的 macOS 补偿
  （`46-52`）、`#reconcileWatchers` 的每目录 watcher 与 inode 身份校验（`407-467`）、
  `#onEvent`/`#onLinkedFileEvent`（`480-495`）、`statfs` 不可靠文件系统探测
  （`60-74`、`510-522`）、`DEBOUNCE_MS` 去抖（`46`）、切换到轮询时补发的
  `{overflow: true}`（`469-478`）。
* 上游定义但本包不再可达的常量与平台差异（Windows 轮询、Linux 每目录 watcher）随之消失。

### 未实现（与上游 `node.ts` 一致，非遗漏）

* **`listFiles`**：接口里是可选能力（`env/index.ts:245-255`），上游的 `NodeExecutionEnv`
  **没有实现**它（`env/node.ts` 全文无 `listFiles`）。本包同样不实现，并把
  `NodeExecutionEnv.list_files` 显式设为 `None`，让类型系统记录这一事实。
  因此 env-conformance 中依赖它的 5 个用例（`env-conformance.ts:484-588`）默认不启用。
* **`window`（输出窗口）**：接口声明了它（`env/index.ts:318-331`），但上游本地环境的
  `feed()` 只上报 `{stream}`，从**不**设置 `skipped`（`env/node.ts:834-854`）；窗口是给
  远程/慢链路环境用的。本包照此实现：接受 `window`，不据它丢弃输出。
  env-conformance 的窗口用例（`env-conformance.ts:438-472`）因此与上游一样逐字节交付。

---

## 批次划分

**批次 A（本包已交付）**

* `env/index.ts`、`env/decode.ts`、`env/line-scan.ts`、`env/node.ts`、`env/node-watch.ts`
* `testing/assertions.ts`、`testing/env-conformance.ts`、`testing/runner.ts`、`testing/types.ts`
* `chord/src/context/index.ts`（作为独立包 `pi-chord`）

**批次 B（未移植，明确不在本批）**

* `testing/storage-conformance.ts`（1663 行）、`testing/storage-benchmark.ts`（489 行）
  —— 见 `durable/src/testing/index.ts:4-17` 的导出
* `durable/src/` 其余部分：`documents.ts`、`entries.ts`、`errors.ts`、`harness/`、
  `session/`、`storage/`、`tasks.ts`、`types.ts`、`codemode/`
* `chord/src/` 其余部分：`host.ts`、`contract.ts`、`object.ts`、`delta/`、`link.ts`、
  `state.ts`、`remote.ts`、`connection.ts` 等（"chord 的宿主半边"，本批**不**引入）
* `chord/src/json.ts`：**本批不需要**。env 岛对它的引用为 0（`grep -rn "json" durable/src/env`
  无命中）；消费它的是 `testing/storage-conformance.ts:1` 与 `storage-benchmark.ts:1`
  的 `import type { JsonValue }`，属于批次 B。

**批次 C₁（本包已交付）**

即原「批次 C」里不依赖 `harness/` 的那一半。已交付 **794 行**上游源码（8 个模块）：
其中 **709 行在批次 C 范围内**（`tools/` 325 + `truncate.ts` 174 + `images/` 210），
另 **85 行**是依赖项 `search.ts`（在原报告里属「顶层共享」，不属批次 C 的 2,204 行）。

| 上游文件 | 行数 | 本包模块 |
|---|---:|---|
| `truncate.ts` | 174 | `pi_durable.truncate` |
| `tools/image.ts` | 182 | `pi_durable.tools.image` |
| `tools/image-processor.ts` | 58 | `pi_durable.tools.image_processor` |
| `tools/path-utils.ts` | 30 | `pi_durable.tools.path_utils` |
| `tools/file-mutation-queue.ts` | 55 | `pi_durable.tools.file_mutation_queue` |
| `images/index.ts` | 140 | `pi_durable.images.index`（Pillow，可选 extra） |
| `images/exif.ts` | 70 | `pi_durable.images.exif` |
| `search.ts`（批次 C 的依赖） | 85 | `pi_durable.search` |

批次 C 的 2,204 行账目（`tools/` 1,551 + `truncate.ts` 174 + `images/` 除 cloudflare 380
+ `tool-search/` 99），**C₁ 交付后**：

| | 行数 |
|---|---:|
| 已交付（范围内） | 709 |
| 已交付（依赖项 `search.ts`） | 85 |
| **批次 C₂，前置已就绪（批次 D-1）** | **1,325** |
| 未移植的 Node 专属件（`images/node.ts`、`node-worker.ts`） | 165 |
| 非代码（`images/photon-wasm.d.ts`） | 5 |
| 明确排除（`images/cloudflare.ts`，不在 2,204 内） | 10 |

### 批次 C₂ 的符号需求（批次 D-1 的验收基准）

上游文件与它们需要的符号（**本表就是 D-1 的验收清单**）：

| 上游文件 | 行数 | 需要的符号 | D-1 是否覆盖 |
|---|---:|---|---|
| `tools/edit-diff.ts` | 500 | 无 harness 依赖；需 `diff`(npm) → `difflib` | — （不需要 D-1） |
| `tools/read.ts` | 312 | `defineTool`、`ToolRegistration`、`ToolExecutionApi`、`ToolExecutionResult`、`ToolDiagnostic` | ✅ 全部 |
| `tools/bash.ts` | 184 | `defineTool`、`ToolRegistration`、`ToolExecutionApi`、`ToolExecutionResult` | ✅ 全部 |
| `tools/edit.ts` | 144 | `defineTool`、`ToolRegistration` | ✅ 全部 |
| `tool-search/tool-search.ts` | 92 | `defineTool`、`defineExtension`、`ToolRegistration` | ✅ 全部 |
| `tools/write.ts` | 39 | `defineTool`、`ToolRegistration` | ✅ 全部 |
| `tools/index.ts` | 39 | `defineExtension` + 四个 `create*Tool` | ✅ 全部 |
| `tools/env.ts` | 8 | `ToolExecutionApi` | ✅ |
| `tool-search/index.ts` | 7 | 转发 `tool-search.ts` | ✅ |

**C₂ 在 `ToolExecutionApi` 上用到的成员（实测 12 处 / 7 个，无解构）**：

| 成员 | 用处 | D-1 |
|---|---|---|
| `api.env` | `tools/env.ts:6-7`、`bash.ts`、`read.ts`、`write.ts`、`edit.ts` | ✅ |
| `api.output(chunk, skipped?)` | `tools/bash.ts:104`（3 处） | ✅ |
| `api.output_window` | `tools/bash.ts:107`（2 处） | ✅ |
| `api.retained_output()` | `tools/bash.ts:126` | ✅ |
| `api.models.get_model(...)` | `tools/read.ts:202` | ✅ |
| `api.diagnostic(...)` | `tools/bash.ts:117` | ✅ |
| `api.agent(context)` | `tools/read.ts:201`（读 `.model`）、`tool-search.ts:67`（读 `.deferred`） | ✅ |

**结论：C₂ 的 1,325 行符号需求已 100% 覆盖。** 未覆盖的只有 `api` 的其余 12 个成员、
`models` 的其余 7 个方法、`Agent`/`Extension` 的其余字段，以及本段**不打算**覆盖的批次 D 主体
（见下）。这些都不被 C₂ 的任何一行使用。

**C₂ 实现时的名字映射**（本包函数按 PORTING.md 偏离 #5 走 snake_case；
类名保持上游拼写）：

| 上游符号 | 本包符号 | 备注 |
|---|---|---|
| `defineTool` | `define_tool` | **关键字构造器**，不是恒等函数（偏离 #41） |
| `defineExtension` | `define_extension` | 同上；只有 `name` / `tools`（偏离 #43） |
| `ToolRegistration` | `ToolRegistration` | `Generic[TArgs]`；`parameters` 是 JSON Schema dict |
| `ToolExecutionApi` | `ToolExecutionApi` | **窄接口，7 个成员；不带泛型**（偏离 #37、#38） |
| `ToolExecutionResult` | `ToolExecutionResult` | `output` 用 pi-ai 的 `TextContent` / `ImageContent` |
| `ToolDiagnostic` | `ToolDiagnostic` | 冻结 dataclass；`api.diagnostic(ToolDiagnostic(...))` |
| `ToolRegistration.prepareArguments` | `ToolRegistration.prepare_arguments` | C₂ 施工时发现 D-1 漏了这个字段（`edit` 用它做参数修复），已补 |

另外两个 C₂ 会用到的辅助类型：`ToolControl`（`tool-search` 的 `addTools` → `add_tools`）、
`ToolOutputLimits`（`bash` 的 `retain: "tail"`）。

### 批次 C₂（本包已交付）

**1,325 行全部落地**，9 个文件逐条状态：

| 上游文件 | 行数 | 本包模块 | 状态 |
|---|---:|---|---|
| `tools/edit-diff.ts` | 500 | `pi_durable.tools.edit_diff` + `pi_durable.tools.jsdiff` | ✅ 已交付（见下方差异清单） |
| `tools/read.ts` | 312 | `pi_durable.tools.read` | ✅ 已交付 |
| `tools/bash.ts` | 184 | `pi_durable.tools.bash` | ✅ 已交付（`bash` + `powershell`） |
| `tools/edit.ts` | 144 | `pi_durable.tools.edit` | ✅ 已交付 |
| `tool-search/tool-search.ts` | 92 | `pi_durable.tool_search.tool_search` | ✅ 已交付 |
| `tools/write.ts` | 39 | `pi_durable.tools.write` | ✅ 已交付 |
| `tools/index.ts` | 39 | `pi_durable.tools`（`create_coding_tools` / `CodingTools`） | ✅ 已交付 |
| `tools/env.ts` | 8 | `pi_durable.tools.env` | ✅ 已交付 |
| `tool-search/index.ts` | 7 | `pi_durable.tool_search` | ✅ 已交付 |

**没有跳过的文件，也没有半成品。** 另新增两个辅助模块：`pi_durable.tools.args`
（pydantic schema 助手）与 `pi_durable.tools.jsdiff`（npm `diff` 的 stdlib 替身）。

### `edit-diff`：行为等价，非字节等价（差异清单）

上游这 500 行里**只有 2 处**用 npm 的 `diff` 包
（`edit-diff.ts:367` 的 `createTwoFilesPatch`、`:382` 的 `diffLines`），其余约 360 行是纯文本处理。
本包用 stdlib `difflib.SequenceMatcher` 替这两个原语，实现放在
:mod:`pi_durable.tools.jsdiff`，**没有自己重写 diff 算法**。

**对拍方法**：`tests/parity/generate-golden.js` 用**真 `diff@8.0.4`**（上游 pin 的版本，
`durable/package.json:63`）生成 golden 语料，`tests/test_edit_diff_parity.py` 逐条比对。
重跑方式（需要 node + npm）：

```bash
mkdir -p /tmp/p && cd /tmp/p && npm install diff@8.0.4
cp <repo>/packages/pi-durable/tests/parity/generate-golden.js .
node generate-golden.js > <repo>/packages/pi-durable/tests/parity/golden.json
```

**实测结果（36 条语料：27 条小语料 + 9 条 100–1200 行的大语料）**：

| 指标 | 结果 |
|---|---|
| `diff_lines` 与 `Diff.diffLines` 形状一致 | 35/36 |
| `create_two_files_patch` 与 `Diff.createTwoFilesPatch` **字节等价** | **35/36** |
| **patch 往返（主验收，对应上游 `test/tools.test.ts:369` 的 `applyPatch`）** | **36/36** |

**唯一差异：`interleaved`（移动行）。** 输入 `a b c d e` → `b a d c e`，两种合法编辑脚本：

| | 顺序 |
|---|---|
| jsdiff（Myers） | `-a` `b` `-c` `+a` `d` `+c` `e` |
| 本包（Ratcliff-Obershelp） | `+b` `a` `-b` `+d` `c` `-d` `e` |

两者编辑距离相同（各 2 删 2 增），**只是并列时的取舍不同**。它的 patch 仍能被正确应用
（已实测），所以是**记录项而非缺陷**。

**差异归类**（供未来判断影响面）：

| 改动类型 | 是否可能分叉 | 例子 |
|---|---|---|
| 单点 insert / delete / replace | 否 | 26 条小语料里全部一致 |
| 多个不连续的独立改动 | 否 | `two-changes-far`、`many-lines` |
| 行尾差异（CRLF、无末尾换行） | 否 | `crlf`、`no-trailing-newline`（`\ No newline at end of file` 也逐字一致） |
| Unicode（CJK、emoji） | 否 | `unicode`、`emoji` |
| **把行搬到别处 / 交换行序** | **是**，可能选出另一个等价脚本 | `interleaved` |

**为什么不影响 `edit` 工具的正确性**：`edit` 的替换位置来自 `fuzzy_find_text` 的**精确偏移**，
与 diff 无关；`generate_diff_string` / `generate_unified_patch` 只产出给用户看的展示文本与
patch。所以分叉只影响「移动行」场景下展示出来的 diff 形状，**改出来的文件内容始终正确**
（由 `test_tools_edit_write.py` 的内容断言保证）。

**`autojunk`**：显式传 `autojunk=False`。实测在本语料上 `True`/`False` 结果相同，
但该启发式**按文档会**改变超过 200 元素序列的结果，所以关掉它以免依赖运气。
语料里专门放了 100/201/250/500/1200 行与一个高频重复行用例来盯这一点。

### `parameters` 的来源：pydantic 一个来源两用

上游 typebox 的 `Type.Object({...})` 既是运行时 JSON Schema，又通过 `Static<typeof schema>`
在类型层抽出 `args` 类型——一个来源两用。本包用 pydantic 达到同样效果：

```python
class WriteToolInput(BaseModel):
    path: str = Field(description="Path to the file to write (relative or absolute)")
    content: str = Field(description="Content to write to the file")

WRITE_SCHEMA = json_schema_of(WriteToolInput)   # pi_durable.tools.args
```

:func:`~pi_durable.tools.args.json_schema_of` 做两处对齐 typebox 的加工：去掉顶层
`description`（pydantic 会把模型 docstring 放进去）与所有层级的 `title`（pydantic 自动生成）。
**这消除了原偏离 #40 的「两个来源」风险**——模型就是 `TArgs`，schema 由它派生，不可能不同步。

为此 `pi-py-durable` 新增了 **`pydantic>=2.7`** 直接依赖（经 Lead 批准；与 `pi-ai` /
`pi-agent-core` / `pi-coding-agent` 的写法一致）。


**(1) 未移植的 Node 专属件 —— 165 行**

| 上游文件 | 行数 | 说明 |
|---|---:|---|
| `images/node.ts` | 130 | 把 Photon 放进 `worker_threads`，空闲 30 s 退出以释放不会收缩的 WASM 内存（`images/node.ts:15-18`、`115-123`）。Python 侧对应线程/进程池，不是移植 worker 协议 |
| `images/node-worker.ts` | 35 | worker 消息协议（`PrepareRequest` / `PrepareReply`），随 `node.ts` 一起去掉 |

**(2) 明确排除 —— 10 行**

| 上游文件 | 行数 | 原因 |
|---|---:|---|
| `images/cloudflare.ts` | 10 | Wrangler 的 `CompiledWasm` 导入规则（Workers 不能在运行时编译 WASM），Python 无对等物。**按任务要求排除** |

### 批次 C 已知差异（行为等价，非字节等价）

* **JPEG / PNG 编码字节**：上游用 Photon 的编码器，本包用 Pillow 的（libjpeg-turbo / zlib）。
  同一张图、同一质量参数下**输出字节不同**，但可观察行为（尺寸、MIME、能否解码、
  是否落在 base64 预算内、``resized`` / ``converted_from`` 的语义）一致。
  因此 `tools/images` 的移植**不能**做字节级回归，只能做行为级。* **`images/node.ts` 的「不阻塞事件循环」**：上游靠 worker thread 达成；本包的
  :class:`~pi_durable.images.index._PillowImages` 在调用线程上同步执行，一张
  12 兆像素照片会占住事件循环约 1.5 s（上游对这个量级有明确说明，
  `images/index.ts:29`）。需要非阻塞时应由调用方放进线程池。


---

## 批次 B（部分交付）：存储内核

上游批次 B 是 **7,972 行 / 10–14 人周**（`recon/06-durable-breakdown.md` §6），本包交付了
它**可独立验收的那一半的一半**：records + `Storage` 契约 + 内存参考后端。
**一致性套件、Session 内核、SQLite 后端未做**——逐项见下。

### 已交付

| 上游文件 | 行数 | 本包模块 | 内容 |
|---|---:|---|---|
| `types.ts`（存储子集） | — | `pi_durable.types` | 记录类型、查询、`StorageWrite`、`Storage` 协议 |
| `ids.ts` | 11 | `pi_durable.ids` | ID / 序列号的信任边界 |
| `errors.ts` | 57 | `pi_durable.errors` | `ReadAfterWrite` / `StorageRequestError` / `SessionFailed` / `ConversationBusy` |
| `storage/scan.ts` | 40 | `pi_durable.storage.scan` | 游标解析（`scan_start` / `next_cursor`） |
| `storage/memory.ts` | 864 | `pi_durable.storage.memory` | `MemoryStorage`，含文档物化与增量重放 |
| **`testing/storage-conformance.ts`** | **1,663** | `pi_durable.testing.storage_conformance` | **`Storage` 的规范口径** |
| `storage/sqlite/database.ts` | 38 | `pi_durable.storage.sqlite.database` | `SqliteExecutor` / `SqliteDatabase` 契约 |
| `storage/sqlite/schema.ts` | 69 | `pi_durable.storage.sqlite.schema` | 9 表 / 16 索引的标识符历史 |
| `storage/sqlite/migrations.ts` | 140 | `pi_durable.storage.sqlite.migrations` | DDL + 原子迁移 |
| `storage/sqlite/storage.ts` | 957 | `pi_durable.storage.sqlite.storage` | `SqliteStorage` |
| `storage/sqlite/node.ts` | 216 | `pi_durable.storage.sqlite.node` | stdlib `sqlite3` 适配器 + PRAGMA |
| **`documents.ts`** | **208** | `pi_durable.documents` | 定义校验、逻辑地址解析、语义/版本检查、物化与迁移 |
| **`entries.ts`** | **34** | `pi_durable.entries` | `define_entry` + 6 个内置条目种类 |
| **`session/observation.ts`** | **292** | `pi_durable.session.observation` | 提交观察桥：`CommittedStateSource` / `CommittedWatch` |
| **`session/forks.ts`** | **97** | `pi_durable.session.forks` | 分叉时的文档复制选择 |
| **`session/transaction.ts`** | **1,043** | `pi_durable.session.transaction` | **全部 5 步已交付**：提交变更、表读取、写入与装配、任务状态机、文档与 `DocumentPlan` |

**主验收：一致性套件 24 条用例，对内存后端与 SQLite 后端各跑一遍 = 48 条全过。**
另有 `tests/test_storage_memory.py`（37 条补充行为测试）与
`tests/test_storage_sqlite_file.py`（5 条**真实文件**集成测试）。

⚠️ **上游是 24 条用例，不是 25 条。** 早先按 `grep -c 'createCase('` 得到 25，其中 1 次是
**函数定义**（``storage-conformance.ts:90``）；实际调用点是 18 处内联 + 6 处多行 = **24**。
本包的名字与顺序与上游逐条一致（用例名就是验收口径，改名会让「全过」失去意义）。

### ⚠️ 未完成项（**批次 B 还没做完**）

| 上游文件 | 行数 | 状态 | 说明 |
|---|---:|---|---|
| `session/session.ts` | 1,081 | ❌ 未做 | Session 内核（单写线、文档缓存逐出、失败即终局） |
| `session/transaction.ts` | 1,043 | ❌ 未做 | 事务与 `Tx` |
| `session/observation.ts` | 292 | ❌ 未做 | 提交观察与 `watchDoc` |
| `session/forks.ts` | 97 | ❌ 未做 | 会话分叉 |
| `storage/sqlite/cloudflare.ts` | 192 | 🚫 排除 | Cloudflare Durable Object 的 `ctx.storage`，Python 无对等物 |
| ~~`documents.ts`~~ | ~~208~~ | ✅ 已做 | 见上表 |
| ~~`entries.ts`~~ | ~~34~~ | ✅ 已做 | 见上表 |
| `types.ts` 的其余部分 | — | ❌ 未做 | `Session` / `Tx` / `TaskRuntime` / `TaskDefinition` 等契约 |

### Session 运行时：**分 3 阶段**，阶段 1 已交付

| 阶段 | 上游 | 行数 | 状态 |
|---|---|---:|---|
| **1** | `session/observation.ts` | 292 | ✅ 已交付（26 条测试） |
| **1** | `session/forks.ts` | 97 | ✅ 已交付 |
| 2 | `session/transaction.ts` | 1,043 | ✅ **完结**（89 条测试） |
| 3 | `session/session.ts` | 1,081 | ✅ **完结**（76 条测试，含**失败即终局**） |

阶段 2 内部按**耦合度从松到紧**又切了 5 步：

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | `applySubmissionChange` + `settleSubmission` / `placeSubmission` | ✅ |
| 2 | 表读取（`conversation` / `entry` / `task` / `scan*` / `latestHeadMarker` / `submission` / `submissionByRequest`） | ✅ |
| 3 | 写入暂存与装配（`createConversation` / `forkConversation` / `appendEntry` / `createSubmission` / `settleSuccess` / `stagedTasks`） | ✅（27 条测试） |
| 4 | 任务状态机（`createTask` / `setTask` / `_stampTimes` / `stagedTasks`） | ✅（22 条测试） |
| 5 | 文档（`doc()` / `retireDoc()` / `DocumentPlan` / `adopt`） | ✅（18 条测试） |

**没有留桩**：第 3–5 步的方法**不存在**，而不是「存在但抛 `NotImplementedError`」——
后者会让人以为能用。

### 阶段 1 的 JS 特有机制 → Python 等价设计（**含语义差异**）

上游这三样在 Python 没有直接对应物，**没有静默替换**，逐条列出：

| # | 上游机制 | Python 等价 | 语义差异 |
|---|---|---|---|
| 60 | `queueMicrotask`（`observation.ts:108`、`242`） | `loop.call_soon(...)` | JS 的微任务队列在脚本块结束时就会排空；`call_soon` 需要事件循环真的转一轮。本模块的 `publish` 调用点全在事件循环里，所以两者等价。⚠️ `call_soon` **不接受协程**，所以 `CommittedWatch._drain`（协程）用 `asyncio.ensure_future` 包成任务 |
| 61 | `Promise.withResolvers<T>()`（`observation.ts:161`） | **惰性** `asyncio.Future` | `Promise.withResolvers` 在任何地方都能建；`Future` 必须绑定事件循环。所以 `CommittedWatch` **不在构造器里建 Future**：终态先记在 `_end`，第一次取 `closed` 时在当时的运行循环上建；若此前已终结就直接返回已落定的 Future。可观察结果相同（终态单次且先于任何 await） |
| 62 | `AbortSignal`（`observation.ts:171-172`、`206-213`） | `pi_chord.context.AbortSignal` | **无需适配**：`add_listener` / `remove_listener` / `aborted` 同名同义。注意 Python 侧属性是 `aborted`（属性）而非 `aborted()` |
| 63 | chord 的 `ReplicatedStateSourceAttachment.snapshot` 是**普通字段** `{value, cursor}` | `pi_chord` 里它是 **property**，返回 `ReplicatedStateSnapshot(value, cursor)` | 形状适配，无行为差异 |
| 64 | `activate()` 必须**同步**排空已缓冲帧 | 同 | 无差异（这是 chord 的契约要求，`state.py` 明确写了） |
| 65 | `RETIREMENT_OPERATIONS` 是 `[["r", null]]`（可变数组） | `(("r", None),)` 元组 | 本包统一用不可变序列；内容是同一个 Op |

### ⚠️ 阶段 2 发现的耦合陷阱（**新偏离 #66**）

| # | 上游 | 本包 | 原因 |
|---|---|---|---|
| 66 | `task.committedRead ??= storage.task(...)` —— 记忆化一个 **Promise** | 记忆化 `asyncio.ensure_future(...)` 产出的 **Task**，**不是**协程 | JS 的 Promise 可以重复 `await`；Python 的协程 `await` 第二次会 `RuntimeError: cannot reuse already awaited coroutine`。直接把协程存进 map 会在「同一事务里读同一个任务两次」时炸——这正是上游用 `??=` 想省掉的那次重复读 |

这条是**施工时被测试抓到的**（`test_task_reads_are_memoized_within_one_transaction`），
不是推理出来的。凡是上游「记忆化一个 Promise 以便重复等待」的地方都要按这条处理。

**阶段 1 发现的两处行为细节**（写成了测试钉住）：
1. **已激活后的 `publish` 是延迟投递**（走 `call_soon`/微任务），`activate()` 时才同步排空。
2. **观察帧溢出的时机**：`advance` 时若 `len(pending) >= 100`，**丢弃全部待投递帧**并换成一帧「替换」（默认最新值），其后继续正常累积——不是"只保留一帧"。

### 第 5 步的一处**接缝**：`TransactionHost.load` 上游由阶段 3 提供

上游 ``TransactionHost`` 是个**接口**，由 ``SessionImpl`` 实现；其中 ``load``（冷加载一个文档）
与 ``cached`` / ``install`` / ``evict`` 都在 ``session.ts:823-852``——**那是阶段 3**。
第 5 步的 ``_acquire()`` 必须调它们，所以本包给 ``TransactionHost`` 提供了**默认实现**：

* 语义与 ``session.ts:823-852`` 一致（版本不匹配就重新加载、按需迁移、装入缓存）；
* **但缓存是无界的**——上游的保留期与逐出定时器（``session.ts:96-104``、``115-125``）
  属于阶段 3。默认实现只保证**语义正确**，不保证内存有界。

阶段 3 的 ``Session`` 会用自己带回逐出的缓存覆盖它。这一点在代码里也标了。

### 阶段 3 内部也切了 4 步

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | **`GuardedStorage` + 失败即终局** | ✅（22 条测试 × 2 后端） |
| 2 | **单写线**（`read_on_line` / `_enqueue`） | ✅（9 条测试） |
| 3 | `commit` 与提交发布（`subscribe_commits` / `subscribe_close` / `observe_commits`） | ✅（21 条测试） |
| 4 | 文档缓存逐出（保留期 + `asyncio.TimerHandle`）+ `snapshot` / `document_state` / `watch_doc` | ✅（24 条测试） |

**没有留桩**：``watch_doc`` / ``snapshot`` / ``document_state`` **不存在**，而不是
「存在但抛 ``NotImplementedError``」——它们依赖第 4 步的文档缓存逐出。

### Session 阶段 3 已完结

| 上游 | 行数 | 内容 |
|---|---:|---|
| `session/transaction.ts` | 1,043 | `Transaction` / `Tx`：约 30 个方法、草稿、任务创建与提交结算 |
| `session/session.ts` | 1,081 | `SessionImpl`：单写线、`GuardedStorage` 失败守卫、文档 tracker 缓存与逐出、提交发布 |

**为什么没做**：这是全包最复杂的一块（私有字段、`Promise.withResolvers`、`setTimeout` 逐出定时器、
promise 链式写线、chord tracker 缓存、约 50 个方法的 `Session`/`Tx` 协议）。
按「做不完就停在干净边界、不留半成品」，我**没有开始**它——写下半个 `session.py`
比不写更糟：它会被误认为可用。

**已交付的定义层是它真正的前置**：`documents.py` 的
:func:`~pi_durable.documents.resolve_address` / :func:`~pi_durable.documents.address_id` /
`check_record_scope` / `check_record_version` / `materialize_document` 正是
`session.ts:4-11` 导入的那五个函数（外加 `AnyDocToken`）。运行时的第 1 步就是
`new Map<string, LoadedDocument>()` 用 `addressId` 做键。

**下一步建议**：先做 `observation.py`(292) 与 `forks.py`(97)——它们不依赖写线；
再做 `transaction.ts`(1,043)（`Tx` 有 `Storage` 契约可对照，且一致性套件已钉住存储语义）；
最后做 `session.ts`(1,081) 的写线与失败守卫。**「失败即终局」的专门测试属于最后一步**，
因为它要求 `GuardedStorage` 与写线同时在位。

### 相对上游的偏离（批次 B）

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| 47 | 记录是 TS 对象类型，运行时就是普通对象 | 记录用 `TypedDict`，运行时就是普通 `dict` | 记录**持久化形式就是 JSON**（`storage/sqlite/migrations.ts:28`）；`TypedDict` 让深比较、`deepcopy` 解除关联、JSON 序列化三件事都自然。这是偏离 #6（对象字面量 → 冻结 dataclass）的**有意例外**：那条针对*选项对象*，记录是*持久化数据* |
| 48 | `Storage.entry()` 有两个重载：`entry(id)` 与 `entry(conversationId, id)` | 拆成 `entry(id)` 与 `entry_in_conversation(conversation_id, id)` | Python 没有重载。语义与上游完全一致（第二个只在会话祖先范围内可见时返回） |
| 49 | `Id<Kind>` 是擦除品牌 + `TaskId<Result>` 带结果类型参数 | `NewType` 风格的别名；`TaskId` 退化为裸别名 | `NewType` 不支持泛型。运行期两者都是裸 `int`，与上游一致 |
| 50 | `clone()` 要处理「无原型对象」并用 `Object.defineProperty` 绕开 `__proto__` 原型 setter（`memory.ts:94-114`） | `copy.deepcopy` | Python 的 `dict` 没有原型链，`d["__proto__"]` 就是普通键。**语义等价且更简单**；一致性套件里 *detaches prototype-like JSON keys* 那条用例断言的性质已由 `test_storage_memory.py` 覆盖 |
| 51 | `prepareCommit` 会 `freeze()` 深冻结留存写入（`memory.ts:116-120`） | 只做**解除关联**（deepcopy），不冻结 | Python 没有 `Object.freeze` 的对等物。可观察语义（改调用方那份不影响已提交状态）保持一致 |
| 52 | JS 的 `Number.NEGATIVE_INFINITY` / `POSITIVE_INFINITY` 作扫描边界 | Python `float("-inf")` / `float("inf")` | 二分助手因此收 `float` 边界 |

### 相对上游的偏离（批次 B 续：一致性套件与 SQLite）

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| 53 | 用例体像 JS 对象字面量那样自由导航记录（`{...record, status: "done", answer: id}`） | 用例体收到的 `storage` 句柄标注为 `Any` | TypedDict 判别联合无法表达这种动态展开。**公开签名仍是 `Storage`**；套件的价值在运行时验证（24 条实跑），不在对用例体本身做静态检查 |
| 54 | `Diff`… 无关。SQLite 适配器用 node:sqlite + 串行操作队列模拟异步（`node.ts:31-120`） | stdlib `sqlite3` + 一把排他锁 + **独立的事务句柄** | Python 的 `sqlite3` 本身就是同步的。关键点是 `transaction()` 里必须把**另一个句柄**传给回调：同一把锁不可重入，事务内再抢锁会自锁（施工时踩到过） |
| 55 | JS 字符串是 UTF-16，可携带**孤立代理项**；SQLite 存 UTF-8 | 记录列：优先 `ensure_ascii=False`（与上游字节一致），遇到孤立代理项退回 `ensure_ascii=True`；索引列一律 JSON 转义 | 孤立代理项**无法**编码成 UTF-8，直接写入会 `UnicodeEncodeError`。一致性用例 *keeps indexed string identities lossless* 正是用它们验证索引不丢信息 |
| 56 | `retired_at` 是投影列，`record` 是 JSON | **两者必须同步更新** | 读取路径读的是 `record` 列；只改列会让文档看起来仍然存活（施工时踩到过，表现为「missing a required base」） |
| 57 | `Storage` 契约要求「回调失败先回滚再以同一错误抛出；回滚也失败则用**不同**错误」 | `ExceptionGroup` / `BaseExceptionGroup` 二选一 | Python 的 `ExceptionGroup` 只接受 `Exception`；`BaseException`（如取消）走 `BaseExceptionGroup`，两条路都保留「回滚失败要有不同错误」的语义 |
| 58 | `busy_timeout` 由 node:sqlite 的 `timeout` 选项提供（`node.ts:190`） | `sqlite3.connect(timeout=...)` | 语义相同 |
| 59 | `storage/sqlite/cloudflare.ts`（192 行） | **明确排除** | 依赖 Cloudflare Durable Object 的 `ctx.storage`，Python 无对等物 |
| 67 | **失败即终局**：第一个 Storage 错误让 Session 关闭自己，后续调用与 waiter 全部以同一错误 reject | 逐字保留语义 | 两个豁免照抄：`StorageRequestError`、调用方上下文已取消的读取。`commit`/`mintId` **永不豁免** |
| 68 | `#failed = Promise.withResolvers<never>()`（永不 resolve，只 reject） | 惰性 `asyncio.Future`（R3） | 同一个 Future 可被多个 waiter await，它们拿到**同一个** `SessionFailed` |

### `pi-storage-sqlite` 并入方案（✅ **已由 Lead 执行**）

> **状态：已执行。** 根 `pyproject.toml` 的三处依赖/mypy 路径已改，`packages/pi-storage-sqlite/` 已改成
> 转发壳（只导出新名字 + 对旧名字发 `DeprecationWarning`），`uv.lock` 已重生成。
> 下列判定与顺序保留为**决策记录**。

前提已满足：`pi-durable` 有 **SQLite 后端**（`pi_durable.storage.sqlite`），
且它通过同一套一致性套件（24/24）。

**1. 逐项判定 `packages/pi-storage-sqlite/`（846 行）**

| 成员 | 判定 |
|---|---|
| `database.py` 的 `SqliteDatabase.exec`/`execute`/`query_one`/`query_all`/`run` | ❌ **不复用**——本包已按上游契约实现了 `SqliteExecutor`（`exec`/`run`/`get`/`all`）与 `SqliteDatabase`（`transaction`/`close`），语义更完整（含回滚失败要用不同错误的约定） |
| `database.py` 的 `transaction` | ❌ 同上；本包的实现已满足上游 `database.ts:18-34` 的契约 |
| `database.py` 的 `open_database`（连接 + PRAGMA） | ❌ 已由 `open_node_sqlite_database` 取代，且 PRAGMA 与上游 `node.ts:194-196` 逐条对齐（WAL / synchronous / wal_autocheckpoint / busy_timeout） |
| `database.py` 的 `configure_database` / `ensure_migrations_table` / `apply_migrations` + `migrations.sql` | ❌ 作废：绑定的是已被上游**整体删除**的 session/entry 模型 |
| `storage.py`（316 行） | ❌ 作废：与上游新 `Storage` 的 15 个方法名**唯一重合的是 `close`** |

**结论修正**：task-23 时我判断「`database.py` 的连接+执行+事务骨架（约 100 行）可留」。
SQLite 后端做完后，实测结论更彻底：**那约 100 行也不值得留**——本包已经有一份**按上游契约写的、
经过 24 条一致性用例验证的**等价实现，保留旧骨架只会形成第二套方言。

**2. 发行名处置：推荐「转发壳」**

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）转发壳** | `pi-storage-sqlite` 发最后一个版本：`__init__.py` 转发到 `pi_durable.storage.sqlite`，并 `warnings.warn(DeprecationWarning)` 指明新路径 | 已有用户的 `import pi_storage_sqlite` 不会静默失效 |
| B 删除 | 从 workspace 移除，不发新版 | 已发布版本撤不回，用户只看到「不再更新」 |
| C 保留 | 继续维护旧模型 | **不可行**：旧模型对应的上游包已被删除，没有可对齐的上游 |

⚠️ 若选 A，**不能简单 `from pi_durable.storage.sqlite import *`**：旧包的
`SqliteSessionRepo` / `SqliteSessionStorage` 在新模型里**没有对应物**，转发它们会给出
「看起来能用但其实语义不同」的名字。建议壳里只做两件事：
① 导出新的 `SqliteStorage` / `open_node_sqlite_storage`；② 对旧名字发
`DeprecationWarning` 并指向 `PORTING.md` 的迁移说明。

**3. 需要改动的根文件清单（交 Lead 统一应用）**

| 文件 | 改动 |
|---|---|
| `pyproject.toml`（根） | `[project.dependencies]` 移除 `"pi-py-storage-sqlite"` |
| `pyproject.toml`（根） | `[tool.uv.sources]` 移除 `pi-py-storage-sqlite = { workspace = true }` |
| `pyproject.toml`（根） | `[tool.mypy].files` 与 `mypy_path` 移除 `packages/pi-storage-sqlite/...` 两条 |
| `packages/pi-storage-sqlite/`（整目录） | 按方案 A 改成转发壳，或按 B 删除 |
| `README.md`（根） | 包清单里移除 / 改写 `pi-storage-sqlite` 一行 |
| `uv.lock` | 改完跑 `uv lock` 重新生成 |

**4. 建议的并入顺序**

1. 先改 `packages/pi-storage-sqlite/` 为转发壳（或删除）；
2. 再改根 `pyproject.toml` 的三处；
3. `uv lock && uv sync --all-packages --all-extras`；
4. `uv run pytest` 全绿（当前基线 **1353 passed**；并入后应基本持平，差额只来自被删掉的旧测试）；
5. 更新根 `README.md` 的包清单。

**5. 并入之后 `pi-durable` 是否自洽**

是。`SqliteStorage` 只依赖 stdlib `sqlite3`（无需新增依赖），并通过一致性套件验证。
`packages/pi-durable/pyproject.toml` 无需为并入再加依赖。

## 批次 D 的分步计划（按**运行时**依赖排序）

依赖图取自 **运行时导入**（见 R21）。括号里是上游行数。

### 分层

| 层 | 文件 | 行数 |
|---|---|---:|
| **L0** 无运行时依赖 | `types.ts`(812)、`context.ts`(333)、`agent.ts`(314)、`inbox.ts`(168)、`prompt.ts`(158)、`usage.ts`(72)、`util.ts`(61)、`define.ts` 余 4 函数(44)、`provider.ts`(39)、`json.ts`(31) | **2,032** |
| **L1** 只依赖 L0 | `scheduler.ts`(1,953)、`tool.ts`(911)、`task-graph.ts`(236) | **3,100** |
| **SCC** 运行时**强连通分量** | `generation.ts`(715)、`compaction.ts`(464)、`live.ts`(268)、`submissions.ts`(241) | **1,688** |
| **L2+** 顶层装配 | `events.ts`(506)、`harness.ts`(470)、`view.ts`(267)、`registry.ts`(114) | **1,357** |
| | | **8,177** |

### 建议的步序

| 步 | 内容 | 行数 | 状态 |
|---|---|---:|---|
| **D-a** | `json.ts` + `util.ts` | 92 | ✅ 已交付（22 条测试） |
| **D-b** | `types.ts` 的**纯数据契约**部分 + `provider.ts` + `usage.ts` | 812 中的约 200，另加 111 | ⚠️ **部分交付**（46 条测试）；接口形状那半未做 |
| **D-c** | `inbox.ts` | 168 | ✅ 已交付（21 条测试） |
| **D-c²** | `context.ts` 的**解锁件**：`ContextView`（纯数据契约） | 10 | ✅ 已交付（+3 条测试） |
| **D-c³** | `context.ts` 正文 | 333 | ✅ 已交付（32 条测试） |
| **D-c⁴** | `agent.ts` 的**数据契约**（`AgentState`/`AgentChange`/三个 policy） | 约 110 | ✅ 已交付（+6 条测试） |
| **D-c⁵** | `agent.ts` 正文 | 314 | ✅ 已交付（41 条测试） |
| **D-c⁶** | `prompt.ts` | 158 | ✅ 已交付（21 条测试） |
| **D-e⁰** | `tasks.ts`（顶层，`define_task`） | 8 | ✅ 已交付（5 条测试） |
| **D-e¹** | `task-graph.ts`（**不依赖被阻塞的 `tool.ts`**） | 236 | ✅ 已交付（18 条测试） |
| **D-e²** | `tool.ts` 的 **Slice A**（叶子层） | 约 390 | ✅ **已交付**（46 条测试） |
| **D-e³** | `tool.ts` 的 **Slice B**（`ToolTask` 本体） | 约 520 | ⏭ 等 `TaskScheduler`（Slice B） |
| **D-f¹** | `scheduler.ts` 的 **Slice A**（模块级层） | 约 265 | ✅ **已交付**（34 条测试） |
| **D-f²** | `scheduler.ts` 的 `TaskScheduler` 类（63 方法） | 约 1640 | 🚧 **进行中（剩 14 个方法）** |

#### `TaskScheduler` 进度（按能力簇）

| 簇 | 方法 | 状态 |
|---|---:|---|
| 核心类型 + **40 字段状态模型** + `__init__` | — | ✅ |
| **C1** 公共 API（`join` 已落地） | 1/7 | 🚧 |
| **C2** 对账链 | 9/9 | ✅ |
| **C3** 归属图 | 22/22 | ✅ |
| **C4** 决议 | 5/7（`_drain`/`_reserve` 等 C6） | 🚧 |
| **C5** 检视 | 1/3（`index_sizes` 已落地） | 🚧 |
| **C6** 调用与阶段执行 | 9/15（生命周期 4 + 门 2 + **提交状态链** 3：`_commit_state`/`_validate_wait`/`_wait_cycle`） | 🚧 |

**剩余 14 个（按名字）**：

| 簇 | 剩余方法 |
|---|---|
| C6（7） | `_start` / `_run` / `_decide` / `_run_abort` / `_step` / `_terminate` / **`_runtime`** |
| C4（2） | `_drain` / `_reserve` |
| C1（3） | `open` / `resume` / `abort` / `wait_for_task` / `wait_for_idle` / `abort_conversation` |
| C5（2） | `inspect` / `_inspect_task` |

⚠️ **`_runtime`（222 行）依赖 `_commit_state`（本轮已落地）**——所以下一步就是它。

**这 19 个是同一块**：`_start` 起调用 → `_run`/`_step`/`_decide` 跑阶段 → `_runtime` 是门面（222 行）
→ `_terminate`/`_commit_state` 写终局 → `_drain`/`_reserve` 驱动 → C1 是外壳。**互相咬合，必须一起做。**
| D-c | `agent.ts` + `context.ts` + `inbox.ts` + `prompt.ts` | 973 | ❌ |
| D-d | `task-graph.ts` + `define.ts` 余 4 函数 | 280 | ❌ |
| D-e | `tool.ts`（**C₂ 的接口面，形状要谨慎**） | 911 | ❌ |
| D-f | `scheduler.ts`（单文件最大） | 1,953 | ❌ |
| D-g | **SCC 四文件**（需先解决 Python 循环导入） | 1,688 | ❌ |
| D-h | `view.ts` + `registry.ts` + `events.ts` + `harness.ts` | 1,357 | ❌ |

### ✅ 两处前置缺口已由 Lead 立项并交付

| 缺口 | 状态 |
|---|---|
| `uuidv7()` | ✅ task-37 交付（`pi_ai.uuid`）—— `provider.py` 已落地 |
| `JsonRepresentation<T>` | ✅ task-38 交付（`pi_chord.json`）—— `usage.py` 已落地 |

⚠️ **`JsonRepresentation` 是 sound 上界，不是全等近似**：Python 侧就是 `JsonValue`，
所以它**更宽、不可下标、无 `undefined` 分布**（见 chord 的 PORTING.md J7）。
移植规则：上游的 `JsonRepresentation<X>` 直接写成 `JsonRepresentation`（去下标）。

### ⚠️ 批次 D 欠账清单（**保持更新**，收尾时一眼可见）

**`types.ts` 的接口形状那一半**（暂缓，阻塞于尚未建立的 `DocumentReader` / `TaskRuntime` / `Harness`）：

`Harness`、`Conversation`、`ConversationWatch`、`Registry`、`RegistryReader`、`RegistrySnapshot`、
`PromptInput`、`PromptSection`、`HookRegistration`、`Wrap`、`Submission`、`ConversationHandle`、
`AgentState`、`AgentChange`、`TaskInspection`、`HarnessInspection`、`ContextView`、`HarnessOptions`、
`Settings`、`HarnessSettings`、`EnvTarget`、`HookApi`、`HookResult`、`GenerationHooks`、
`ToolHookCall`、`ToolHooks`、`CompactionHooks`、`CompactionPolicy`、`ProgressPolicy`、
`InstalledTool`（`RegistrySnapshot.tools()` 的元素形状）

**D-c 已完结**（D-c 的全部文件都已交付）

#### ⚠️ `agent.ts` 需要的**最小面**（已核实，逐项）

`agent.ts` 需要 **9 个**在「接口形状那一半」里的类型。其中 **5 个是纯数据，本轮已交付**；
剩下 **4 个是接口**，需要**你确认形状**再写：

| 需要 | 性质 | 状态 |
|---|---|---|
| `AgentState` | 纯数据 | ✅ 已交付 |
| `AgentChange` | 纯数据（**三态**：替换 / `None` 清除 / 不设不改） | ✅ 已交付 |
| `ConversationRetryPolicy` | 纯数据 | ✅ 已交付 |
| `CompactionPolicy` | 纯数据 | ✅ 已交付 |
| `ProgressPolicy` | 纯数据 | ✅ 已交付 |
| **`Settings`** / **`HarnessSettings`** | 接口（`Settings` 的 10 个字段全是**已解析**值） | ❌ 需确认 |
| **`RegistrySnapshot`** | **Protocol，6 个方法**；但 `agent.ts` 只用到 **2 个**（`installed()` / `extension()`） | ❌ 需确认 |
| **`PromptSection`** | 数据 + 一个 `render(input, context)` 方法 | ❌ 需确认 |
| `ConversationStreamOptions` | 数据，但含 `Transport` / `CacheRetention`（pi_ai 的类型，需先核实） | ❌ 需确认 |

**已按 R28 处理**（两处都是「情形二：形状已知、只是消费者少」）：

* `RegistrySnapshot` 按上游**完整形状**定义 6 个方法（实现留给 `registry.py`）；
  它带出的 `PromptSection` → `PromptInput` → `DocumentReader` 一条链一并定义。
* `Settings` / `HarnessSettings` / `ConversationStreamOptions` —— 完整形状都能从上游读出，
  所以**不等 `registry.py`**，直接定义。

⚠️ `ConversationStreamOptions.transport` 的取值是上游的字面量联合
（``ai/src/types.ts:144``：``"sse" | "websocket" | "websocket-cached" | "auto"``），
但 **pi_ai 目前没有导出 `Transport` 这个名字**。这里写**内联字面量**——
形状精确，只是没有名字；**不在别的包的范围里造一个同名类型**。

⚠️ `DocumentReader.snapshot_as_of` 对应的 ``SessionImpl.snapshot_as_of`` **尚未落地**——
那是**实现缺口**，不是形状未知。

**`context.ts` 的依赖已核实就绪——它不需要 `TaskRuntime`**：
`ContextView`（已交付）+ `SessionImpl.read_on_line`（批次 B 已交付）+
`Storage.scan_entries` / `entry` / `find_latest_head_marker`（批次 B 已交付）+
`SystemEntry`（已交付）+ pi_ai 的 `Message` 族。
⚠️ 但它**确实需要** `SessionImpl` 这个**具体类**（上游以类型方式导入），
所以 `context.py` 会第一次把 `session/` 与 `harness/` 接在一起。
**D-d ~ D-h 未做**：`task-graph.ts`(236)、`define.ts` 余 4 函数(44)、`tool.ts`(911)、
`scheduler.ts`(1,953)、**SCC 四文件**(1,688)、`view.ts`+`registry.ts`+`events.ts`+`harness.ts`(1,357)

### ⚠️ D-a 当时发现的两处缺口（已解决，留档）

| 缺口 | 影响 | 说明 |
|---|---|---|
| `uuidv7()` —— 上游从 ``@earendil-works/pi-ai/utils/uuid`` 取 | 阻塞 `provider.ts`(39) | **`pi_ai` 里没有** `utils/uuid`（`import pi_ai.utils.uuid` → `ModuleNotFoundError`）。它在队友的包范围里，我没有绕过去自己实现 |
| `JsonRepresentation<T>` —— chord 的类型 | 阻塞 `usage.ts`(72) | **`pi_chord.json` 里没有**这个符号。它是「T 的 JSON 表示」，Python 侧大概率就是 `dict[str, Any]`，但那属于 chord 的范围 |

另外 `provider.ts` 还依赖 `TaskRuntime`（在 D-b 的 `types.ts` 里），所以它表面是 L0、
实际被 D-b 阻塞——**这也是 R21 的同类问题：分层要看真正的依赖，不看文件位置**。

### ⚠️ `scheduler.ts`(1953)：**一个类，按函数粒度切不开**

它是全批次最大的单文件，但结构是**一个类 + 一层模块级东西**：

| 部分 | 规模 | 能不能切 |
|---|---|---|
| `TaskScheduler` 类 | **63 个方法、约 1640 行** | ❌ **切不开**——全部共享 `#` 私有状态（`#live`/`#below`/`#edges`…） |
| 模块级层 | 约 265 行 | ✅ 类型 + 不碰那套状态的纯函数 |

**为什么类切不开**：抽走任何一个方法，都要**连带抽走它读写的那套私有状态**——
那不是「切片」，是**把类拆成几个协作对象**，是**结构偏离**。
（这与 SCC 那次同源：**不可切的单元**，只是这次在**类**这一级。）

**⇒ 切口**：

* **Slice A（已交付）**：模块级层 —— 类型 + 纯函数，**可独立验收** ✓
* **Slice B**：`TaskScheduler` 类**整体**（63 个方法）——一个不可再切的单元。
  届时用 **`TaskRuntime` 的契约测试**钉住它（就像 `Tx` 那次断言 `Transaction` 满足契约）——
  而且**本切片落地后必须重判那两条延迟边**（见上）。

#### Slice A 顺带补齐的两个契约

| 契约 | 上游 | R28 判定 |
|---|---|---|
| **`RegistryReader`** | `harness/types.ts:389-394`，**2 个方法** | **情形二**（形状已知）→ 定义 |
| **`TaskSchedulerOptions`** | `scheduler.ts:151-197`，15 个字段**全是回调** | **情形二** → 定义 |

`RegistryReader` 的 `subscribe` **同步**是语义的一部分：调度器靠它被唤醒去重新考虑被阻塞的任务，
异步通知会让「发布了但没人被叫醒」变成一个**时序 bug**。

### SCC 四文件的**拆分方案**（已核实，可拆——`live → generation → compaction → submissions`）

**结论：可以拆，用 3 处函数内延迟导入即可把环压成单向，然后按文件做。**

#### 环的精确形状（运行时边 + 具体符号）

| 边 | 具体符号 | 方向 |
|---|---|---|
| `generation → live` | `endRun`、`LiveDoc`（+ 仅类型 `LiveState`/`ToolSlot`） | 前进 |
| `generation → compaction` | `createCompaction`、`estimateContext`、`selectCut` | 前进 |
| `live → generation` | `convertPartial` | **回边** |
| `compaction → submissions` | `admitSubmission` | **回边** |
| `submissions → generation` | `startRun` | **回边** |
| `submissions → live` | `LiveDoc` | 前进 |

两个环：`generation ↔ live` 与 `generation → compaction → submissions → generation`，
外加 `submissions → live` 把它们连成一个 SCC。

#### 拆法：选 `live → generation → compaction → submissions` 这个顺序

只把**回边**改成函数内导入（**3 处**），其余保持顶层导入：

```python
# live.py
def some_function(...):
    from pi_durable.harness.generation import convert_partial   # 回边，延迟

# generation.py
def some_function(...):
    from pi_durable.harness.compaction import create_compaction, estimate_context, select_cut  # 回边，延迟

# compaction.py
def some_function(...):
    from pi_durable.harness.submissions import admit_submission  # 回边，延迟
```

`generation → live`、`submissions → generation`、`submissions → live` 保持**顶层**导入。

#### **可行性已核实**：这 6 个符号**全部只在函数体内使用**

逐个查过（行号见下），**没有任何一个出现在模块级**（默认参数、装饰器、类体）——
否则不能延迟。

| 符号 | 使用处 | 是否函数内 |
|---|---|---|
| `convertPartial` | `live.ts:259` | ✅ |
| `createCompaction` | `generation.ts:171,183,499` | ✅ |
| `estimateContext` | `generation.ts:329` | ✅ |
| `selectCut` | `generation.ts:337,493` | ✅ |
| `admitSubmission` | `compaction.ts:437` | ✅ |
| `startRun` | `submissions.ts:224,239` | ✅（**保持顶层**） |

#### ⚠️ 环的**最终归属**：最小割是 **2 条边**，不是我原方案的 3 条

四块都到位（Slice A/B 合计）之后**逐条重判**，结论是原方案**多延迟了一条**：

| 边 | 原方案 | **最终** | 为什么 |
|---|---|---|---|
| `generation → compaction` | 延迟 | **顶层** ✅ | 不是环上的边——`compaction` **不**导入 `generation`。Slice A 已按顶层写 |
| `live → generation` | 延迟 | **延迟**（必要） | `generation`（Slice B）要 `live` 的 `endRun`/`LiveDoc` → **真二环** |
| `submissions → generation` | 延迟 | **延迟**（必要） | 它打破三环 `generation → compaction → submissions → generation` |
| `compaction → submissions` | 延迟 | **顶层** ✅ | 三环已被上面那条打破，这条不必再延迟 |

**⇒ 最小割 = 2 条边**：`live → generation`（真二环）+ `submissions → generation`（打破三环）。

**判据**：**打破一个环只需要切一条边。** 原方案在同一个三环上延迟了两条（`compaction → submissions`
与 `submissions → generation`），**其中一条是多余的**。四块齐全后逐条重判才看得出来——
**这就是为什么「回边能不能还原」必须等两侧都到位再判，不能提前定。**

#### ⚠️ 通则：**「环的最小割」是全局性质，只能在环上所有节点都到位后判定**

分步实施时，你**看不到完整的环**——所以你**只能先多延迟几条**。但那些是**临时决定**，
**不是最终结构**：

> **打破一个环只需要切一条边。** 但在只做了一半的时候，你不知道哪一条是"那一条"——
> 所以你会多切几刀。
>
> **⇒ 环上所有节点都到位后，必须做一次全局重判**，把多余的延迟还原成顶层导入。
> **不要让临时决定沉淀成永久结构。**

**实证（批次 D，SCC 四块）**：原方案延迟了 **3** 条边；四块齐全后重判，最小割是 **2** 条：

| 边 | 原方案 | 最终 | 原因 |
|---|---|---|---|
| `generation → compaction` | 延迟 | **顶层** | **根本不在环上**——`compaction` 不导入 `generation` |
| `live → generation` | 延迟 | 延迟 | 真二环 |
| `submissions → generation` | 延迟 | 延迟 | 打破三环 |
| `compaction → submissions` | 延迟 | **顶层** | 三环已被上面那条打破，这条多余 |

**这与「延迟导入是结构妥协、不是终点」是同一件事的两半**：
那半说「**要记得撤**」，这半说「**什么时候才能判**」——**两者缺一，妥协就会永久化**。

#### 代价（要登记的偏离）

1. **3 处函数内延迟导入**——本端新增的偏离，上游是顶层 ESM 导入（靠提升与活绑定容忍环）。
2. **失败时机后移**：环上若真有问题，上游在 **import 时**就报，本包在**首次调用**时才报。
   代价可接受（模块一定齐备），但要写进文档，免得后人以为「这里懒加载是为了性能」。
3. 每次调用多一次 `sys.modules` 查表——可忽略，但**不要**把它当成性能手段。

**替代方案（不推荐）**：把共享小件抽到第三个模块（如 `LiveDoc` 抽出去）。
那会**改变文件结构**，偏离比延迟导入更大；而且环的成因是**双向调用**（`live` 调 `generation`
的 `convertPartial`、`generation` 调 `live` 的 `endRun`），抽类型解决不了。

#### 建议的子步序（做完一步停一次）

| 步 | 文件 | 行数 |
|---|---|---:|
| **SCC-1** | `live.ts` | 268 | ✅ 已交付（24 条测试） |
| **SCC-2a** | `generation.ts` 的 **Slice A**（叶子层） | 约 180 | ✅ **已交付**（21 条测试） |
| **SCC-2b** | `generation.ts` 的 Slice B（`GenerationTask` 本体） | 约 535 | ⏭ 等 `scheduler.py` |
| **SCC-3a** | `compaction.ts` 的 **Slice A**（叶子层，纯函数） | 约 210 | ✅ **已交付**（32 条测试） |
| **SCC-3b** | `compaction.ts` 的 Slice B（`CompactionTask` 本体） | 约 250 | ⏭ 等 `scheduler.py` |
| SCC-2 | `generation.ts` | **715** |
| SCC-3 | `compaction.ts` | 464 |
| SCC-4 | `submissions.ts` | 241 |

`generation.ts` 是单文件最大的一块，建议它单独占一步。

#### ✅ SCC-1 的那个缺口**已经补上**（留档）

SCC-1 当时说「`settle_scheduler_outcome` 的运行任务分支要 `generation.convert_partial`，
在 SCC-2 落地前测不了」，并留了延迟导入 + `type: ignore`。

**`generation.py` 的 Slice A 一到，两件事都做掉了**：

* `live.py` 里那行 `# type: ignore[import-untyped]` **已删除**（mypy 报 `unused-ignore` 才发现它过期了）
* `tests/test_harness_live.py` 补了两条测试：**一次出错运行的完整清理**
  （留下 aborted 助手条目 + 结算输入 + 清掉呈现）与**不拥有该运行的任务不越权**

**这正是「延迟导入是结构妥协、不是终点」的兑现**——缺口按计划关掉了，没有沉淀。

#### ⚠️ `convert_partial` 又踩了一次 **R24**（第 5 个实证）

`live` 是 chord 的 `Draft`，**两层下标**读出来的还是 `Draft`，而 `copy_json` 要严格 JSON：

```
TypeError: Value contains a non-JSON Draft; expected strict JSON
```

**R24 的判据「每一处『从草稿读出嵌套值再交给别处』都要补 `to_json()`」直接命中**。
加了 `_plain()`（与 `inbox.py` 同款助手）。

#### ⚠️ SCC-1 落地后的一个**直接后果**（按方案接受）

`live.py` 的 ``settle_scheduler_outcome`` 在**运行任务**那条分支上调
``generation.convert_partial``——而 ``generation.py`` 是 **SCC-2**。所以：

* 延迟导入、``type: ignore`` 与全部注释**已就位**；
* 但**那条分支在 SCC-2 落地前测不了**。SCC-1 的 24 条测试覆盖了其余全部分支
  （工具任务、压缩任务、其它种类忽略、以及全部纯函数）。

**这是拆分的代价，不是遗漏**：上游那里是顶层导入，四个文件在同一个模块图里同时成立。
拆开之后「先做的那一块」必然有一条边指向「还没做的下一块」。
**SCC-2 一到，删掉那行 ``type: ignore`` 并补一条测试即可。**

`live.py` 里 ``end_run`` 的 ``tx`` 收成 ``Any``：上游签名是 ``Tx``（约 30 个方法的事务面），
那是「接口形状那一半」里尚未定义的——本模块只用到 ``settle_submission``，
按 **R28 情形一**（形状未知就不写）先不定义它。

### ⚠️ SCC-2 **被阻塞** —— 我的 SCC 方案漏了两条**出环**的前置

**开 SCC-2 前的依赖核查（逐项）发现：`generation.ts` 不能在这一步做**，
因为它的运行时依赖里有**两个我方案里没有的模块**。

#### `generation.ts` 的运行时依赖逐项

| 依赖 | 状态 |
|---|---|
| chord / `entries.ts` / `env` | ✅ |
| `agent.ts`(`addTools`) / `inbox.ts` / `json.ts` / `live.ts` / `prompt.ts` / `provider.ts` / `usage.ts` | ✅ |
| **`tasks.ts`**（顶层，**8 行**） | ❌ **未做** |
| **`tool.ts`**(911) —— `appendToolResult` / `harnessError` / `ToolTask` | ❌ **未做（D-e，且不在 SCC 内）** |
| `compaction.ts`(464) —— `createCompaction` / `estimateContext` / `selectCut` | ⏸ 方案内**回边延迟**（SCC-3） |
| pi_ai `utils/retry` | ✅（`pi_ai.retry`） |
| pi_ai `utils/transcript` | ✅（`pi_ai.transcript`） |
| **pi_ai `utils/overflow`(`isContextOverflow`)** | ❌ **缺口** |

#### 我方案里的错在哪

我的 SCC 分析**只看了环内的边**，所以 `live → generation → compaction → submissions`
这个顺序对**环内**是对的；但 `generation` 还有一条**出环的前向依赖** `generation → tool`，
而 `tool.ts`(911) 根本不在 SCC 里。**方案漏了它。**

**R21 的教训在这里又出现了一次，只是换了个面**：我只画了「环内」的图，
没画「这个文件**全部**运行时依赖」的图。**排某一步的顺序时，要看它的完整依赖，
不只是它与同组文件的关系。**

#### ⚠️ R26 在本片是**反向**的：下游要**对象**，我们手里是 **dict**

`pi_ai.estimate` 的 `estimate_message_tokens` / `calculate_context_tokens` 要读**属性**
（`usage.total_tokens`），而 `view.contributions` 里的消息是**从存储读回的 JSON dict**。
不转就会报：

```
TypeError: unsupported operand type(s) for *: 'dict' and 'int'
AttributeError: 'dict' object has no attribute 'total_tokens'
```

**做法**：`_as_message()` / `_context_tokens()` 用 pydantic 的 `TypeAdapter` 还原。

**⚠️ 这是 R26 的另一个方向，值得记清**：

| 方向 | 情形 | 做法 |
|---|---|---|
| **我们收** | 存储给的是 dict，代码想按对象读 | `_field()` **两种都认** |
| **我们给**（本片） | 下游（别的包）按**属性**读 | **必须**转成对象——不能「两种都认」 |

**统一判据**：**跨包传值时服从对方的形状；包内自用时才两种都认。**

#### ⚠️ `tool.ts` 为什么按**可测性**切，而不是按代码量

``ToolTask`` 的三个 phase（``call`` / ``execute`` / ``abort``）都收 ``runtime: TaskRuntime``，
而 **``TaskRuntime`` 的实现住在 `scheduler.ts`**——关键路径上靠后好几步。

**⇒ `ToolTask` 本体的「可测性上限」由 `scheduler.ts` 决定。** 这不是实现问题，是**依赖顺序决定的**。

所以切口选在**叶子层**：Slice A 顺着文件自己的依赖方向（核心 → 叶子，从不反向），
**两片之间没有环，也就不需要任何延迟导入**。

**为什么不用「手写假 `TaskRuntime`」提前测 Slice B**：那测的是**契约的用法**，
不是**契约的满足**——与 `test_transaction_class_satisfies_the_tx_contract` 正好相反
（那个之所以有价值，是因为它断言**已存在的真实实现**满足契约）。
**实测排在 `scheduler.py` 到位之后补。**

#### 为什么本轮先做了 `task-graph.ts`

`tool.ts` 只有 **2 个被阻塞点**（`validateToolArguments` 在 ``tool.ts:250``、
TypeBox 的 `Compile` 在 ``557-574``），但这两点分别在 `validate()` 与
`structuredOutputError()` 里，而 `ToolTask` 的 `run()` 依赖前者——
**911 行的文件没法"只做没被阻塞的那半"而不留半成品**。

所以本轮改为交付**同一关键路径之外、且不依赖那两个符号**的完整模块：
`task-graph.ts`(236)。它是 `harness.ts` 与 `scheduler.ts` 的前置，
按运行时图属 L1（只依赖 `util`），依赖已全部就绪。

#### 修正后的关键路径

```
tasks.ts(8)  ──┐
               ├─→ tool.ts(911) ──┐
pi_ai 缺口 ────┘                  ├─→ generation.ts(715)
compaction.ts(464) ───────────────┘
live.ts(268) ✅
```

**建议顺序**：`tasks.ts`(8) ✅ → **`tool.ts`(911，D-e)** → SCC-3 `compaction.ts`(464)
→ SCC-2 `generation.ts`(715) → SCC-4 `submissions.ts`(241)。

⚠️ 注意 **`compaction` 与 `generation` 互为依赖**（环），所以先做 `compaction` 时它到
`submissions` 的那条边要延迟、`generation` 到它那条边是**前进边**；到了 `generation` 时反过来。
**两条边都要按方案延迟**，这是方案里已经写好的。

#### 已在本轮交付的解锁件

| 文件 | 行数 | 状态 |
|---|---:|---|
| `tasks.ts` → `pi_durable.tasks`（`define_task` / `DefinedTask`） | 8 | ✅ 已交付（5 条测试） |

**它同时解锁 `tool.ts` 与 `generation.ts`**（两者都运行时导入 `defineTask`）。
本包**照上游一样不校验**定义内容——完整 `TaskDefinition` 的 `phases`/`abort`/`migrate`/`hooks`
在任务运行时才被用到，那是 scheduler；凭空发明更严的规则会把合法定义在这里就拒掉。

#### `tool.ts` 还有一个**移植决策**要你定：TypeBox

`tool.ts` 用 **`Compile` from `typebox/compile`**（``tool.ts:13,557,573``）来校验
``structuredOutputSchema``。TypeBox 是 JS 库，**Python 没有对等物**。可选：
① 用 pydantic 的 ``model_validate``（但 schema 是 JSON Schema 而非 Python 类）；
② 用 ``jsonschema`` 包做校验；
③ 用自己的校验器（`ToolRegistration.parameters` 已经是 JSON Schema）。

**这是「本端选择」，需要你定；我不猜。**（属于 R28 情形一：形状/方案未定就先不写。）

## 契约落地登记（R28 情形二）

| 契约 | 上游 | 规模 | 落位 | 状态 |
|---|---|---|---|---|
| **`Tx`** | `types.ts:787-895` | 31 成员 → 本包 **17 个名字** | `session/tx.py` | ✅ **已交付**（10 条测试） |
| **闭包里不涉及 `Models` 的 7 个小件** | 见下 | 7 个契约 | `types.py` / `session/tx.py` / `harness/types.py` | ✅ **已交付**（14 条测试） |
| **`TaskRuntime` 本体** | `types.ts:170-240` | 24 成员 → 22 名字 + 3 继承 | **`harness/runtime.py`** | ✅ **已交付**（+7 条测试） |
| ~~`SessionDocToken<T>`~~ | `types.ts:104` | — | — | ⛔ **已评估、故意不引入**（R28 **情形三**） |
| ~~`ConversationDocToken<T>`~~ | `types.ts:105` | — | — | ⛔ **已评估、故意不引入**（R28 **情形三**） |

> ⛔ **上面两条是「判定不做」，不是「欠账」。**
> 原因：本包 scope 在**运行期**由 `check_record_scope()` 校验，
> 造类型别名**不提供静态保证**（R28 情形三）。
> **不是遗漏** —— 后人查闭包表时看到它们，不必去「补齐」。
> （同类的还有 `TaskDocToken` 与三个 family 版，共 6 个。）

### `TaskRuntime` 的落位（R21 第三面的**第三次**落位应用）

它同时需要 `session/tx.py` 的 `DocumentObserver` 与 `harness/types.py` 的
`HookRunner`/`ConversationHandle`/`RegistrySnapshot`/`Agent`/`Settings`/`DocumentReader`/`SettledTask`，
所以必须住在**两者之后** → **`harness/runtime.py`**（新模块）。

**按名字归类会把它放进 `types.py`（上游就在那）——那是错的**，
因为 `harness/types.py` 与 `session/tx.py` 谁都不导入谁。

#### ⚠️ 我漏过它继承的两个父接口（测试抓到）

上游是 `interface TaskRuntime extends DocumentObserver, DocumentReader`。
我第一版只写了 `class TaskRuntime(Protocol)`，于是
**`watch_doc` / `snapshot` / `snapshot_as_of` 一个都没继承到**。

**漏父接口不会报错**——它只会让**使用方拿不到方法**（`generation.ts` 要调 `runtime.context()`，
`tool.ts` 要调 `runtime.entry()`/`commit()`，`live` 相关要 `watch_doc`）。

**判据**：**移植一个 `extends A, B` 的接口时，先把父接口的成员数也数一遍**——
`dir()` 出来的数量对不上，就是漏了父接口。

### 7 个小件的落位（按「契约住在它依赖的类型之后」）

| 契约 | 上游 | 落位 | 为什么在这 |
|---|---|---|---|
| `RunningTask` | `types.ts:137` | `types.py` | 只依赖 `TaskRecord`/`TaskState`（同在 `types.py`） |
| `NextTaskState` | `types.ts:145` | `types.py` | 同上 |
| `TypedEntry` / `TypedEntryDraft` | `types.ts:357-362` | `types.py` | 别名，依赖 `EntryRecord`/`EntryDraft` |
| `DocumentWatch` | `types.ts:909` | `types.py` | = `WatchHandle[...]`，`WatchHandle` 在 `types.py` |
| **`DocumentObserver`** | `types.ts:914-935` | **`session/tx.py`** | ⚠️ 引用 `DocToken` → **不能**进 `types.py`（`documents.py` 运行时导入它） |
| `HookRunner` | `types.ts:158` | `harness/types.py` | harness 契约 |
| `ConversationHandle` | `harness/types.ts:127` | `harness/types.py` | harness 契约 |

#### ⚠️ 两个**故意不引入**的：`SessionDocToken` / `ConversationDocToken`

它们出现在我最早的闭包清单里（当时判为「情形二 → 定义」），
**但那个判断被我自己在实现 `Tx` 时推翻了**——理由已写在 `session/tx.py` 的模块文档里：

> 上游靠**类型参数**把 scope 编进类型（`DocToken<T, ... & {scope: "session"}>`）。
> 本包的 `DocToken` 把 scope 放在 `definition` 里、由 `check_record_scope()` 在**运行期**校验。
> **造六个只在类型上不同的别名是噪音，不是契约。**

**判据**：**「形状已知」是「定义它」的必要条件，不是充分条件**——
还要问「**这门语言里有没有对应物能表达它**」。没有对应物时强行造出来，
得到的是「看起来更贴合上游、实际不提供任何保证」的别名，**正是 R23 那类刻意的 no-op 的近亲**。

（同上：`TypedEntry<D>` 给的是**别名 + 说明**，不是新类；收窄由 `EntryToken.is_()` 承担。）

### `Tx` 的落位为什么不在 `types.py`（R21 第三面在**落位**上的应用）

`Tx` 的签名引用 `DocToken` / `DocFamilyToken`，而 **`documents.py` 运行时导入 `types.py`（L0）**。
把 `Tx` 放进 `types.py` 会造成**运行时循环导入**。

**⇒ 契约住在「它依赖的类型之后」，而不是「看起来该住的地方」。**

判据同 R21 第三面：**排顺序看「谁需要它的形状」**——这里多一条，**落位也看它**。

### ⚠️ 31 → 17：哪些塌了、为什么

| 上游 | 本包 | 原因 |
|---|---|---|
| `doc` **6 个重载**（session / conversation+id / task+id / 三个 family 版） | **1 个**（`token, *address`） | scope 在本包是**运行期属性**（`token.definition["scope"]`，由 `check_record_scope()` 校验） |
| `retireDoc` 6 个重载 | **1 个** | 同上 |
| `entry(id)` / `entry<D>(token, id)` 两个重载 | **1 个**（`id, token=None`） | 令牌做成第二个可选参数（偏离 #48 的同一取舍） |
| `SessionDocToken` / `ConversationDocToken` / `TaskDocToken`（+ `Rewindable` + 三个 family 版） | **不引入** | 上游靠**类型参数**把 scope 编进类型；本包 scope 在运行期。造六个只在类型上不同的别名是**噪音，不是契约** |
| `TypedEntry<D>` / `TypedEntryDraft<D>` | **别名 + 说明**（`TypedEntryRecord = EntryRecord`） | 类型参数在运行期**被擦除**；收窄由 `EntryToken.is_()` / `as_typed()` 承担 |

**「塌掉」不等于「少做」**：塌掉的那些在 Python 里**没有对应物可表达**，
强行造出来只会得到「看起来更贴合上游、实际不提供任何保证」的别名——
**那正是 R23 那类刻意的 no-op 的近亲**。

### ⚠️ 我在这条上犯过一个错，值得留档

我第一版**给 `Tx` 加了一个上游没有的名字 `entry_of`**（想把 `entry<D>(token, id)` 那半单独表达）。
测试立刻抓到它——`Transaction` 没有这个成员。

**错在哪**：本包的 `Transaction.entry(id, token=None)` **早就**用「可选第二参数」
覆盖了两个重载（偏离 #48 已登记）。我却为了「贴合上游的两个重载」而**新造了一个名字**，
那会让**同一个意图有两个入口**，而其中一个是多余的。

**判据**：**收口既有实现时，先看它已经怎么表达了这个意图**——
不要因为上游是重载就以为本包也需要两个名字。

## ⚠️ 偏离 #48 的子项：**位置参数顺序不同**（最危险的一类）

上游把「令牌」放在**第一个**位置参数，本包把可选的那个放到**最后**。
这不是重载问题，是**位置顺序**问题——而它**不会报签名错**：
两边都是 3 个参数，类型也未必拦得住，**错的是语义**。

| API | 上游 | **本包** |
|---|---|---|
| `appendEntry` | `(token, conversationId, value)` | **`(conversation_id, value, token=None)`** |
| `entry` | `(token, id)` 重载 | **`(id, token=None)`** |
| `append_entry`（`Tx`） | 见上 | 见上 |

**实际踩到的报错文本**（照写，方便后来的人**搜报错找到这里**）：

```
RuntimeError: Conversation EntryToken(kind='pi.tool-result') does not exist
```

**读法**：令牌被当成 ``conversation_id`` 传进去了，于是 ``_require_conversation()``
拿着一个 ``EntryToken`` 去查对话。**看到「对话 X 不存在」而 X 是一个 token，就是这个问题。**

**判据**：**移植「令牌 + 数据」这类多参数 API 时，先确认本包把令牌放在第几位**——
不要按上游的顺序想当然（偏离 #48 已经把令牌挪到末尾了）。

## ⚠️ TS 靠类型系统保证、Python 只能靠测试保证的三处

同一族的三种情形——**共同点：上游那行代码不写就编译不过，本端不写只是静默变松**。

| TS 的保证 | Python 的退化 | 对策 |
|---|---|---|
| `interface X extends A, B`（成员一定齐全） | 漏写父接口**不报错**，只是使用方拿不到方法 | 数 `dir()` 的成员数（见 R31 症状索引） |
| **穷尽的 `switch`**（漏分支编译不过） | 退化成 if 链，**漏的分支静默走默认** | **每个分支一条参数化测试** |
| 类型级「这个字段必填」 | `total=False` / 可选参数，**漏了不报错** | 契约测试断言字段集合（R22/R28） |

**判据**：**上游那行东西由编译器保证的，本端就要由测试保证**——
否则它会从「编译期错误」变成「运行期静默」。

## ⚠️ TS 的穷尽 `switch` 到 Python 会**退化成 if 链**——漏一个分支不报错

上游大量使用**穷尽**的 `switch`：类型系统保证每个分支都写了，
**漏一个连编译都过不了**。Python 没有这个保证：

```python
if status in ("failed", "faulted"): return ...
if status == "orphaned":            return ...
return "..."                        # ← 剩下的全都悄悄落到这里
```

**漏掉一个分支不会报错**——它只会让那个状态**静默地**走默认分支，拿到一句**错的话**。

**实例（批次 D）**：`compaction_failure()` 在 `Exclude<TaskOutcome, {status:"completed"}>` 上穷尽，
四种状态（`failed`/`faulted`/`orphaned`/`aborted`）各有文案。
**把 `aborted` 漏成默认分支不会报错**，只会让「被中止的压缩」显示成别的原因。

**做法**：对每一处上游穷尽 `switch`，**写一条参数化测试把每个分支都钉住**——
用测试补回类型系统让掉的那部分保证。

## 做法：**临时性事实应当写成会过期的断言，而不是注释**

「还没做」是一个**临时性事实**。它可以写在注释里，也可以写成一条断言——
**两者都会过期，但只有后者会叫你。**

```python
def test_unimplemented_methods_do_not_exist_yet() -> None:
    # ⚠️ 等它们落地时**这条测试会失败**，提醒改成「存在且可用」。
    for name in ("open", "resume", "abort", ...):
        assert not hasattr(TaskScheduler, name), f"{name} 尚未落地，不该存在"
```

> **注释会静静过期，断言会主动叫你。** 到期时它的失败**就是提醒**。

**这把「不留桩」从一句纪律变成了一个会自己报警的检查**——
纪律依赖人记得，**机制不依赖**。

（它与〈工具主动提醒的「该撤了」信号〉是同一族：**都是「让系统提醒你」，而不是「靠你记得」**。
区别只是**前者是工具给的、这条是你自己埋的**。）

## ⚠️ 关闭 / 中止路径上的代码**必须比正常路径更耐操**

**判据**：**它跑的时候，系统已经不正常了**——正常路径可以假设前置条件成立，**这里不能**。

**三个实例**（同一族，出现在三个不同层）：

| # | 位置 | 正常路径可以假设 | 关闭路径**不能** | 不这么写会怎样 |
|---|---|---|---|---|
| 1 | `join()` | 每个调用都有 `done` Future | 可能**没有** | 第一版 `gather(*[inv.done …])` 遇 `None` 抛 `TypeError` |
| 2 | `_seal()` | 退订回调不抛 | **宿主回调可能抛** | 关闭**中断在中途**，等待者再也不会被拒 |
| 3 | `schedule_watch_stop()` | 有事件循环可以 `await` | **同步上下文**（`#end` 在关闭路径上） | 观察**停不掉**；**关闭不能因此中断** |

**推论**：**在关闭路径上，"顺手抛出去"不是可选项**——那里没有上层能处理它。
所以关闭路径上的每一步都要**自己兜住**并**继续走完**剩下的步骤（`_seal` 的顺序正因此才重要）。

**两个限制合起来**：关闭路径上不仅**不能抛**，也**不能等**（`await` 一个可能永远不来的东西）。
> **关闭路径上的每一步都必须是「尽力而为且立即返回」的。**

上表三个实例正好各占一半：**1、2 是「不能抛」（1 抛会中断关闭、2 抛会漏掉等待者），3 是「不能等」（同步上下文里没法 await）**。

#### ⚠️ 最危险的一类：**依赖对方行为才能终止的等待**

第四个实例（`abort()` 的 join）：它等那次运行的 `done`，**但同时等 `session.failed`**——
因为一个**忽略自己信号**的运行可能活过一次**失败的 Session**，只等 `done` 会**永远等下去**。

**它比前三个更隐蔽**：

| | 前三个 | **这一个** |
|---|---|---|
| 什么时候挂 | 条件成立就挂 | **只有当对方忽略信号时才挂** |
| 正常测试能否触发 | 能 | **很难**（要对方"不配合"） |
| 症状 | 稳定的挂起 | **条件性的挂起**——在你最不希望它出现的时候 |

**判据**：**一个等待如果它的终止依赖「对方会不会配合」，就必须有第二条终止路径。**
正常路径上的合作方**在异常路径上恰恰是不合作的**——而那正是这段代码要处理的场景。

## 时序 / 生命周期相关的隐式契约

有些要求在代码里**看不出来**，只能从「这段代码在什么时候跑」推出来。它们最容易在重构时丢掉：

| 契约 | 实例 | 丢了会怎样 |
|---|---|---|
| **某个通知必须同步** | `RegistryReader.subscribe` | 异步通知 → 「发布了但没人被叫醒」的**时序 bug** |
| **关闭路径上的代码必须比正常路径更耐操** | `TaskScheduler.join()` | 它跑的时候**系统已经不正常了**——正常路径可以假设前置条件成立，这里不能（第一版 `gather(*[inv.done …])` 遇 `None` 就抛 `TypeError`） |
| **查与登记必须同一段原子区** | `Submissions.wait()` | 一次结算发布落在两者之间 → 等待者**永远不被唤醒** |
| **检查与使用之间状态会变** | `TaskScheduler._watch_doc()`：取到观察后**再查一次 `ended`** | 不查就会把一个**属于已结束调用**的观察挂上去 → **永远不会被停**（资源泄漏） |
| **检查的前提数据尚未加载** | `TaskScheduler._commit_state()`：判「还有没有活的下层工作」**之前**要先把链加载好 | `_has_owned_live` 靠**向下遍历**，而它要求每个活任务的链已加载——**不先加载会把一个有子任务的父误判成终局**，于是父的结果**提前定死** |
| **清理要在监听器都跑完之后** | `TaskScheduler.join()` 的调用时机 | 会让「Harness 关闭」显示成「用户取消」 |

**判据**：**读一段代码时问「它什么时候跑、跑的时候什么是可信的」**——
正常路径的隐含前提，在**关闭 / 取消 / 出错**路径上往往**不成立**。

## 工具主动提醒的「该撤了」信号（低成本自查）

有些妥协是**临时**的，而工具会**主动告诉你它过期了**。这类信号成本极低，**看到就处理**：

| 信号 | 什么时候出现 | 含义 |
|---|---|---|
| **mypy `unused-ignore`** | 被 `type: ignore` 掩盖的东西**已经存在**了 | 当初那个妥协**该撤了** |
| mypy `unused-awaitable` | 一个 `await` 是多余的 | 上游那里可能不是异步的 |
| ruff `invalid-syntax` | 用了目标版本不支持的语法 | 本仓 `requires-python` 比你以为的低 |
| ruff `redundant-cast` | `cast` 的目标已是推断出的类型 | 类型已经收窄了，cast 可以删 |

**实例**：SCC-1 给 `live.py` 留了 `from ...generation import convert_partial  # type: ignore[import-untyped]`，
因为那时 `generation.py` 还不存在。`generation.py` 的 Slice A 一落地，
**mypy 立刻报 `unused-ignore`** —— 不是人想起来的，是工具提醒的。

**判据**：**看到 `unused-*` 就当成「TODO 到期」处理**，别把它当噪音加进忽略列表。

## ⚠️ 代码也会骗你，而且**注释骗得最狠**

工具会骗你（见下一节，但那些是**噪音型的**），**代码本身**也会——而注释是最危险的一种。
原因：**注释是「作者的意图声明」，读的人天然信任它**。

| 骗法 | 症状 | 实例 |
|---|---|---|
| **注释承诺了行为，代码没做** | 下一个人**以为已经处理了** | `delay()` 里写了「剩余为 0：仍然要检查一次中止」，**但没有那行代码** |
| 注释描述的语义已过期 | 改了代码没改注释 | — |
| 注释与实现**方向相反** | 照注释写会写错 | `inbox.py` 模块文档先说「steer 排在 follow-up 之前」（错，实为按收件箱位置） |

**判据**：**注释里承诺了行为的，必须有一条测试钉住它**——
否则那句注释就是**未来的谎言**。

**实例留档**：`delay()` 那条注释是**测试**抓到的（`DID NOT RAISE`），不是人读出来的。
**所以「注释 → 测试」这条对应关系，就是防线本身。**

## ⚠️ 代码存在，但存在于**错误的作用域**里

**这是「便宜的替代物」的对偶**：那节讲「用便宜信号代替事实」，这条讲
**「你以为的那个事实，根本不在那个位置」**。

**实例**：我把三个方法 `cat >>` 到文件末尾——**但那里已经在类外了**
（前面有个模块级函数），于是它们变成了**那个函数的局部函数**：
**语法合法、`ruff` / `mypy` 全过、导入也不报错**，只是**类上没有这些方法**。

**为什么它比"没写"更糟**：

| | 没写 | **存在但作用域错** |
|---|---|---|
| 你以为 | 知道还没写 | **以为写了** |
| 工具报什么 | — | `'X' object has no attribute 'y'`——**只说"没有"，不说"它在别处"** |
| 什么时候发现 | 你知道要写 | 直到运行时踩到 |

**判据（从使用侧验证）**：

> **往一个类里追加方法后，直接看 `dir(实例)` 里有没有它。**

**`dir()` 比 `grep` 好**——`grep` 是**从文本侧**验证（"我看着缩进对了"），
`dir()` 是**从使用侧**验证（"我确实拿到了它"）。
**从使用侧验证总是更接近真实。**

## ⚠️ 决定「错误 / 降级 / 丢弃」的形态时，**先问「谁会消费这个值」**

**实例（`final_result` 的 `structuredOutput` 违约）**：同一个违约，处置**按调用种类不同**：

| 谁读这个值 | 处置 | 为什么 |
|---|---|---|
| **程序**（嵌套调用） | **变成错误结果** + 一条说明 | 程序读的就是那个值——**给了错的值还不如明确报错** |
| **模型**（模型发起的调用） | **只丢掉值**，调用照旧成立 + **报告宿主** | **模型根本看不到 `structuredOutput`**，对它来说没有违约 |

**判据**：**消费者不同，正确的处置就不同。**
（与「呈现的消费者缺位 → 去掉呈现」是同一条。）

## ⚠️ 写入的**形状**由下游的 **diff 语义**决定

**实例（`publish_progress`）**：三道设计，少一道就会让**每次提交都写整段输出**：

| 写什么 | 怎么写 | 不这么写会怎样 |
|---|---|---|
| `output` | **整体赋成一个字符串字段** | chord 把它 diff 成「追加」或「裁剪+追加」；**替换整个槽位对象会把整段窗口每次记下来** |
| `details` | **逐叶 diff**（`assign_json`） | 每次提交重写整棵 details |
| `diagnostics` | **只追加新的**（按上次写的条数切片） | 每次提交重写整份列表 |

**这是很容易忽略的一层**：代码看起来只是「给个字段赋值」，而实际是在**决定存储增长量**。
**它不会报错——只会在存储里慢慢膨胀。**

**判据**：**写入的数据形状，要按下游的 diff 算法来设计**，而不是按"读起来自然"来设计。

## ⚠️「先读被调方」要读到**它读我参数的哪一面**

不只是「它收几个参数」，而是**它怎么用我传进去的东西**：

**实例**：我读了 `Progress` / `OutputBuffer` / `overlap` 的**签名**，
却**没读 `bound_content` 读的是 `limits` 的哪一面**——它读的是 `limits.retain`
（chord 的 `OutputLimits`，**snake_case 属性**），而我传了 dict。

**这与 R21 的「看谁需要它的形状」是同一条原则在参数级的应用。**

## ⚠️ 错误信息里要带**实际值**与**实际原因**

三处同一条判据，出现在三个层次：

| 位置 | 只说「不行」会怎样 | 带上实际值/原因 |
|---|---|---|
| `_gated`（第 4 条前置） | 「不是 running」——排查少一半信息 | **报出它实际是什么**（`Task 7 is waiting`） |
| `_inspect_task`（第 5 条） | 「被挡了」，**不知道为什么** | **现在就造出那句错误**（`has no migration from 1`） |
| `_fit` / `_resolve` | 「接不下」 | **报出四种原因之一**（`missing_task`/`task_too_old`/`migration_failed`） |

**判据**：**阻塞原因要带足够信息让人能行动**——
「不行」是**观察**，「为什么不行」才是**可行动的**。

⚠️ **「可行动」的标准是「读的人知道下一步做什么」。** 若读完还是只能猜，那就是信息不够——
**不是"再详细一点"的问题，是缺了那个能推出下一步的字段。**

## ⚠️ `scheduler.py` 2,883 行 vs 上游 1,954 行 —— **差额的逐项核对**

⚠️ **不要只看总行数**。逐项量过之后，差额**全部有解释，没有代码膨胀**：

| | 上游 `scheduler.ts` | 本端 `scheduler.py` |
|---|---:|---:|
| 总行 | 1,954 | 2,883 |
| **注释** | 332 | 157 |
| **文档串** | （计入注释风格） | **680** |
| 空行 | 111 | 412 |
| **代码** | **1,511**（上界，行尾注释未剥离） | **1,634** |

**⇒ 代码只差 +123 行（+8%）**，而总行差 929 = **文档串 680 + 空行 301 + 注释结构差异**。

那 +123 行来自**逐条登记过的 JS→Python 适配**，不是新逻辑：
`inspect.isawaitable`（`T | Promise<T>`）、`_plain()`/`to_json()`（R24）、
`isinstance(dict)` 双形状（R26）、`_MISSING` 三态（R29）、显式字段赋值等。

**判据**：**报告规模时给"代码行"而不是"总行"**——否则注释多的实现看起来像膨胀，
而实际上**它可能比上游还短**（本端代码比上游**总行数**少 319 行）。

## ⚠️ `[0]` / `[-1]` 这类下标：**只在"顺序是契约的一部分"时才安全**

**实例**：`scan_entries` 返回的是**「最新在前」**。我写 `page["items"][-1]["id"]` 想取"最后一条"，
**实际取到的是最早那条** —— 于是 `runtime.context(..., at=cutoff)` 读回的视图只有 1 条条目，
`select_cut` 返回 `None`，整条溢出路径落到失败分支。

**正确写法**：`max(e["id"] for e in page["items"])` ——
**表达你要的是什么（"最新的 id"），而不是"列表最后那个"。**

⚠️ **最危险的性质**：我为了诊断写的那个脚本**也踩了同一个坑**（它复刻了我的写法）。
**⇒ 那个写法很"自然"，所以它才危险** —— 你不会觉得它在假设什么。

**判据**：**下标访问要么有契约保证，要么换成 `max`/`min`/显式排序。**
（与「`max_tokens <= 0` 时**不设上限而不是取 0**」并列：**都是"位置/数值边界上的语义"**。）

## ⚠️ 清 4 条 `xfail` 时揪出的**一个实现缺陷 + 三条测试基建认知**

### ① 实现缺陷（**已修**）：`_run` 的 phase 处理器查询在 `try` **外面**

上游是 ``await erased(state.task).phases[checkpoint.phase]!(...)`` —— **整个表达式都在 try 内**。
我第一版把 ``handler = phases[...]`` 放在 **`try` 外面**，于是**一个未知 phase 名**会：

1. 抛 `KeyError` → **逃出 `_run`** → `_start` 的 catch **只 `report` 它**；
2. 而 `_start` 的 `finally` 会 `_end` + `_kick` → 任务**重新可运行**；
3. `_reserve` 再预留 → 再抛 → **无限自旋 + 报告风暴**
   （实测一次跑出**上百条** `KeyError: 'prepare'`，而任务**永远不到终局**）。

**⇒ 移进 `try` 之后**，它就是「普通的一次 phase 失败」→ `previous.failure` →
下一次裁决按规则 6 落成 `faulted` → 任务**正常终局**。

⚠️ **判据**：**「只 report 不 fault」的错误路径 + 「finally 里重新排队」的组合 = 无限自旋。**
凡是 `finally` 会**把任务放回可运行集合**的地方，`try` 必须**包住所有可能抛的语句**
（不只是"用户代码"那一段）。

### ② `tx.task()` 返回的是**被记忆化的那份 dict** —— 原地改它会污染 memo

`Transaction._committed_task` 是**记忆化**的（上游 `task.committedRead ??=` 同样），
而 `tx.task(id)` 把**那个被记住的 dict 本身**交给你。原地改它之后，
`_validate_task_replacements` 再读同一个 id 时拿到的是**你改过的那份** ——
于是它以为「存储里已经终局」，报 `Task 4 is already terminal`（**而存储里其实是 `pending`**）。

**⇒ 照上游 `withState(record, …)` 的习惯：建一份新的**（`{**record, "state": {...}}`）。
**不要在 `tx.task()` 的返回值上原地改。**

### ③ `_reserve` 的第一条过滤是 `id in self._invocations`

测试若为了拿到真 runtime 而 `_create_invocation(...)`，**那个调用必须在跑调度器之前 `_end()`** ——
否则 `_reserve` 会**直接跳过这个任务**（它以为已经有活调用），任务永远停在 `waiting`。

### ④ `open()` 是 `#live` / `#runnable` 的**唯一**填充路径

`make_harness` 故意不自己 `open`（好让测试决定时机）。**不 `open` 就 `resume`，
`#live` 是空的**，`_reserve` 什么也找不到 —— 任务**永远停在 `pending`**。

## ⚠️「会过期的断言」的**第 5 种形态：`xfail(strict=True)`**

| 形态 | 临时性事实 | 谁叫 |
|---|---|---|
| 测试层 | 「方法还不存在」 | 测试自己（`test_unimplemented_*`） |
| 工具层 | 「这里有 import 的 ignore」 | mypy 的 `unused-ignore` |
| 类型层 | 「名字还没定义」的 ignore | mypy 的 `unused-ignore` |
| **marker 层** | **「这条还修不好」的 `xfail`** | **`strict=True` 的 `XPASS`** |

**⚠️ 第 4 种让「欠账标注」也变成会主动失效的形式** ——
**修好之后它报 `XPASS(strict)` 失败，于是修的人必须删掉 marker，不能"修好了还挂着 xfail"。**

**⇒ 本仓所有 `xfail` 都必须带 `strict=True` + 清楚的 `reason`。**

## ⚠️ 编辑工具**静默失效** —— 与"注入被覆盖"方向相反

| | 谁失效了 | 表现 |
|---|---|---|
| **注入被覆盖** | **被测对象**忽略了你的注入 | 测试失败，但断言/逻辑都"对" |
| **编辑没落上** | **你的工具**没执行你的意图 | **你在一个没改过的文件上反复"修"同一个问题** |

**实例**：我用 `python3` 做字符串替换，而 `ruff format` 已经改动过那几行的**形状**，
于是 anchor 匹配不上 —— **而替换失败不报错**。我因此在**没改过的文件**上"修"了三轮。

**⇒ 第二种更浪费时间**：因为你**会以为改过了**，于是**下一次"修"是在错误的前提上做的**。

**判据**：
> **任何"批量/脚本化"的修改之后，立刻有一次"读回来"的验证** ——
> `read` 一行或 `grep -c` 一个标记。**成本是一次工具调用，收益是排除整类浪费。**

**工具选择**：`edit` 在 `old_string` 不唯一或匹配不上时**会报错**（不像字符串替换静默跳过）——
**这就是它比脚本可靠的原因。**

## ⚠️ 观察不是"给不确定的人用的"，是给所有人用的

**实例**：我在诊断时提出假设「6 轮 user+assistant 可能全在同一块贡献里」，
而 `scripts/diagnose_select_cut.py` 的**第一次观察就推翻了它** —— 实际是**一条一块**。

> **连提出假设的人也会猜错。**

**⇒ 这是「同一个夹具试错两次还没成，改为观察」那条规则的实例之一。**
**而它同时说明：那条规则的阈值（2 次）不该因为"我比较有把握"而放宽** ——
**"有把握"与"猜对"是两件事。**

## ⚠️ `Draft` 的**协议边界**（用之前先查这张表）

**`Draft` 是一个自定义协议的对象，不是任何一种内置容器。**
**"长得像某个协议" ≠ "遵循那个协议" —— 要确认一个协议，看它的完整方法集。**

| | 已确认 |
|---|---|
| ✅ **像 dict** | `get` / `keys` / `items` / `__getitem__` / `__delitem__` / `__contains__` |
| ❌ **不像 dict** | **`pop(index=-1)` 是列表语义** —— 传 `pop("k", None)` 报 `TypeError: takes from 1 to 2 positional arguments but 3 were given` |
| ❌ **不像 list** | `isinstance(draft, list)` 为 `False` |
| ❌ **`in` 是下标记检查**，不是"有没有这个键" |

⚠️ **第 2 行让 6 处代码同时是错的**（`answer` 的交接、`classify` 的 overflow、
`finish_tool_round` 的两条分支、`_place_summary`、`_compaction_retry_phase`）——
而它们**全都通过了 mypy 与 ruff**，因为**调用形状是合法的**。

**正确写法**（`Draft` 有 `__delitem__` 与 `__contains__`）：
```python
if key in target:
    del target[key]
```

**判据**：**一个对象"有 dict 的多数方法"不等于它遵循 dict 的协议 —— 用之前先读那个方法的签名。**

## ⚠️ R26 的**反方向**：框架函数按对象写、我们给 dict

R26 原本几处是「**本包实现**按对象写、调用方给 dict」。
**这一处反过来**：`pi_ai` 的判别函数（`is_context_overflow` / `is_retryable_assistant_error`）
**按 pydantic 对象写**，而我们从 provider 流里拿到的是 **JSON dict** ——
传进去报 `'dict' object has no attribute 'stop_reason'`。

**⇒ 溢出压缩与重试这两条最难的路径，在任何真实运行里都会直接抛。**

⚠️ **修法有一条细节**：转换函数要**幂等** ——
`if isinstance(message, AssistantMessage): return message`。
**不要在有损转换上做无谓的重建**（重复构造会丢掉 provider 的元数据）。

## ⚠️ 先订阅、后加载 —— 反过来会**丢事件**

**实例（`open`）**：它**先挂三个监听器**（提交 / 关闭 / 注册表），**再做那次加载提交**。

**反过来（先加载、后订阅）会丢掉订阅之前发生的事件**，而症状是
**偶发的状态不一致**——**不是报错**。它会表现为「某个任务的状态在很久以后莫名不对」，
而你在那时根本想不到是"启动顺序"。

**判据**：**任何「订阅 + 读取当前状态」的组合，都必须先订阅**——
因为读取与订阅之间**总有一个窗口**，而先订阅能让那个窗口**只产生重复事件、不产生丢失**。
**重复可以幂等处理，丢失不能。**

## ⚠️ 两阶段：**「打标记」与「生效」必须分开在两个提交里**

**实例（`_reserve`）**：第一次（开档后）**只给「被抛弃的任务」打中止标记，然后返回**。

**同一次提交里既打标记又预留，会让刚被标记的任务在同一轮里就被预留成 ``run``**——
而它其实**应该等 abort**。**标记的动作会绕过标记的意图。**

**判据**：**当一个标记的作用是「改变下一轮的行为」时，它必须与「读取该标记的那一轮」分开提交。**
**合并两者 = 让写入与读取在同一轮里发生，于是读取看到的还是旧值**——
而**这里更糟**：它看到的是一个**半应用**的状态。

## ⚠️ 幂等的反面：**不幂等 + 会被重试** = 重复副作用

这两条要**一起看**才看得出严重性：

| | 单独的「不幂等」 | 单独的「会重试」 | **两者同时** |
|---|---|---|---|
| 后果 | 调两次错两次 | 没事，重试是对的 | **第一次的副作用已经发生了**，而系统以为它没跑 |

**实例（`_step` 的 `except`）**：这一步写的是**调度器自己的决定**。
**如果那里抛出而没人接住，任务会留在 `running`**——于是它会被**再次预留、再次运行**，
**而它上一次的状态已经被写过一半了**。

**⇒ 所以 `_step` 必须在自己内部结束调用并弄失败 Session**——不是为了"错误处理得好看"，
而是**为了不让一个半写的状态被重跑**。

**判据**：**一个操作如果"可能失败"且"会被重试"，它就必须要么幂等、要么在失败时把状态收回。**
**两者都不做 = 重复副作用**，而它**只在重试真的发生时**才暴露。

## ⚠️〈替身的正确边界〉—— 什么时候**该**用替身

前面一整族都在讲「替身在骗你」。**这条给出口**：

> **替身只该出现在真正的边界上**（网络、时钟、文件系统）。
> **一旦它出现在内部接缝上，你就在测自己编的语义。**

**实例**：`FakeModels` 假的是 **provider**（外部边界，没有网络就不可能真），
而**不是框架内部**。**凡是本包自己的东西都用真的**——真 `Session`、真 `TaskScheduler`、
真 `_RuntimeView`、真 `pi_ai.EventStream`（连假 provider 推事件都推**真的流**）。

**它与「能用真的就别用替代物」不冲突**：那条说「**能用真的就用真的**」，
这条说「**用不了真的时，边界画在哪**」。

## ⚠️ 读一个"容器的接口"时，先看它**继承了什么**

**实例**：`EventStream` 有个 `.events` 成员，我就写了 `async for event in events.events` ——
而 `.events` 是**普通 list**，报 `'async for' requires an object with __aiter__ method`。
**正解是直接 `async for event in events`**：它**本身**就是 `AsyncIterator`。

**判据**：**不要只看它有哪些成员，要看它的基类列表** ——
成员告诉你能"取"什么，基类告诉你能"当"什么。

## ⚠️ 测「攒一批再处理」的机制，**必须先让"攒"真的发生**

**实例**：`stream_response` 的 partial 提交是**节流**的（`partialIntervalMs`）。
我第一版测试推完 partial **立刻 `finish()`**，于是**什么都没写进去**——而**实现是对的**：
流一结束就取消计时器，**pending 的 partial 有意不提交**（终局消息由 `classify` 提交）。

⇒ 测试要先 `await asyncio.sleep(> interval)`，让计时器**真的落地**。

**判据**：**任何"攒一批再处理"的机制，测它的前提是让"攒"真的发生。**
**否则你测的是快速路径，而你以为是慢路径** —— 而**快速路径上那段代码根本没跑**。

## ⚠️ 测幂等：**枚举它的所有副作用，每条都要有断言**

「幂等」的失败表现**不是「函数炸了」**，而是**某一个副作用越界了**——
所以「连调两次不抛」几乎什么都没测到。

**做法**：先**列出这个函数会做的每一件事**，然后**逐条断言第二次没有再做一遍**。

**实例（`TaskScheduler._end`）**——它做四件事，所以幂等测试要四条断言：

| 副作用 | 断言 |
|---|---|
| 从登记表删自己 | 只删「还是我」的那条；已有的新调用**不动** |
| 放回可运行候选 | 已结束的调用**不再**放回别人的任务 |
| 停掉每个观察 | 第二次**不再**停一遍 |
| 中止控制器 | 第二次**不再**中止；**新调用的信号保持未中止** |

⚠️ 只测第 1 条（我第一版就是这样）会漏掉后三条——而**它们才是最容易越界的**。

## ⚠️ TS 的 `T | Promise<T>` → Python 的「**可能不可 await 的 awaitable**」

上游大量函数收 `T | Promise<T>`（同步或异步都行）。Python 里直接 `await` 会炸：
``object str can't be used in 'await' expression``。

**做法**：``inspect.isawaitable(result)`` 判断之后再决定 await 不 await。

**这与「`T | undefined` 三态」并列**——都是**类型层面的表达差异**：
TS 把「同步或异步」编进类型，Python 只能运行期判断。

## ⚠️「造一个」还是「改一个」——接收并返回同类对象的函数

`with_abort_signal(signal, context)` **不是**「从零建一个上下文」，而是**「给现有上下文叠加一个信号」**。
我把它当成了前者、传了 `None`，于是它**拿 None 就炸**。

**这类「看起来像装饰器/工厂、实际是变换器」的 API 很容易误用。** 而它的失败方式还算好——
**若它对 `None` 宽容，就会变成一个「静默无信号」的上下文**：取消再也不会传播，**而且什么都不报**。

**判据**：**遇到「接收一个已有对象并返回同类对象」的函数，先确认它是「造一个」还是「改一个」。**
名字里的动词（`make`/`create` vs `with`/`wrap`/`apply`）**通常**能提示，但**不能只靠名字**。

## ⚠️〈便宜的替代物〉—— 本仓翻车的最大公约数

**每一次翻车都是同一个形态**：**用一个便宜的替代物代替了当前事实，而替代物总有它覆盖不到的维度。**

| # | 替代物 | 它冒充的事实 | 漏掉的维度 | 实例 |
|---|---|---|---|---|
| 1 | **名字** | 实现的行为 | 名字是**摘要**，不是契约 | `done` 像过滤器（实为剪枝）/ `unknown` 像必然结果 / `node` 像收窄形状 |
| 2 | **旧观察** | 现状 | 文件**变了** | 「25 passed」是**加测试之前**跑出来的 |
| 3 | **混合导入图** | 运行时依赖图 | `import type` 被**擦除** | `types.ts` 看起来绕在环里，运行时依赖是**零** |
| 4 | **出簇方法调用数** | 「抽走它要带走什么」 | **字段写入** | C3：2 次调用 vs **4 个字段** |

**第 5 个实例（新形态）：注入的东西被**静默覆盖**。**

我为一条测试**注入**了一个假的注册表快照（写进 `state["snapshot"]`），
**但被测方法会重新读 `_registry.snapshot()`**（那正是它的规则之一：有进展就刷新快照）。
于是注入**被覆盖**，测试失败。

> **我注入了一个「我以为是输入」的东西，而实现把它当成「要重新取的东西」。**

**为什么这个形态特别阴**：注入**"成功"**了（代码跑通、没有异常）、测试**失败**了（断言不成立），
于是你会去查**断言**或**被测逻辑**——而问题在**夹具与被测对象的耦合方式**。
**这是"夹具错误伪装成实现错误"的第 5 次，但根因不同**：
前四次是夹具**抓错了地方**，这次是**夹具抓对了地方、但被测对象根本不用别处**。

**判据**：
> **当你要注入一个依赖时，先确认实现是「用传进来的」还是「自己去取的」**——
> 后者会让你的注入**静默失效**（实现照常跑，只是没用到你的注入）。

#### 这一族的统一定义：**夹具错误 = 「我以为喂进去的」≠「它实际吃到的」**

已经踩了 **6 次**，但**形态只有三种**：

| 形态 | 实例 |
|---|---|
| **抓错了位置** | 注入 `state["snapshot"]`，而实现**自己重新取** `_registry.snapshot()` |
| **构造顺序错** | 第三个处理器**先 `raise`**，于是根本没走到解析那一步（`assert 2 == 3`） |
| **初始状态错** | 传了 `None` 当 tx／记录状态用了 `pending` 而实现只看 `running`／断言 `_dirty is True` 但它被消费了 |

**三种都归到同一件事**：

> **夹具错误的本质永远是「我以为喂进去的」≠「它实际吃到的」。**
> **你不会因此收到任何错误——只会收到一个不成立的断言。**

#### 预防半节：**诊断救已经失败的测试，预防用来不写错夹具**

排查顺序（①②③）是**事后诊断**。它与下列动作是两回事：

> **写夹具/桩之前，把被调方的签名与返回形状读一遍。**

**别"看名字推断"**（那是〈便宜的替代物〉第 1 条）。**具体读三样**：
**参数个数与次序**、**每个参数的类型**、**返回值的形状**（例如分页函数返回的是
``{"items": [...]}`` 还是 ``[...]``——那是两种完全不同的桩）。

**它与「一轮只做一个可验证单元」同构**：都是**把代价前移**——
**事前多花 1 分钟，省掉事后 N 次"跑→报错→改"的循环。**

#### 📊 前后对照（实证，比任何说理都有力）

| 轮次 | 做法 | 夹具迭代次数 |
|---|---|---|
| 实现 C1 那轮（`_observe` 等 5 个） | **写完就跑** | **5 次**（缺方法 → 缺 `scan_tasks` → 挂错对象 → 签名少参数 → 返回形状错） |
| 实现 `inspect` 那轮 | **先读被调方签名与返回形状** | **1 次通过** |

**同一族的错误、同一批夹具、同一套工具**——差别只在**写之前有没有读**。

**排查顺序（照这个顺序走，别跳）**：

1. **它实际看到什么**（打日志 / 注入探针）；
2. **我给了什么**；
3. **两者差在哪**。

⚠️ **而慌的时候人会跳过第 1 步直奔第 3 步**——因为**推理比观察便宜**。
第 6 次实例里我的第一反应就是「数一下应该有几次解析」——**那不是观察，那是推理**。

**第 6 个实例（第 3 类形态）：测试的**构造顺序**错了。**

我写一条「三个阶段各解析一次 agent」的测试，让第三个处理器**先 `raise`**——
于是它**根本没走到解析那一步**。断言 `2 == 3` 失败。

> **被测行为在被测点之前就该发生，而我把被测点放在了它前面。**
> **测试的构造顺序错了，不是断言错了。**

**判据**：
> **当一个测试断言「某事发生了 N 次」而实际是 N-1 时，先查「第 N 次的那个路径有没有走到那一步」**——
> 不要先怀疑计数逻辑。**这类错误伪装成"计数不对"，实际是"路径没走到"。**

⚠️ **而且我当时的第一反应是去数「应该有几次解析」——那正是「用推理代替观察」。**
**这是本通则的元层面**：**慌的时候人会退回到推理**（推理比观察便宜），
**而"去看第 3 次到底走到哪了"才是省时的那条路。**

**统一判据**：

> **任何「我从这里就能推断出来」的结论，都要问一句：
> 「这个是当前事实，还是它的替代物？」**
> **而替代物总有它覆盖不到的维度。**

**为什么它值钱**：四条替代物**都是省事的**——省掉的是**读实现 / 重跑 / 排除 `import type` / 数字段**。
**省事的代价不是"多花点时间"，而是"你根本不知道哪里错了"。**

## ⚠️ 名字是意图的摘要，**不是契约**

移植时**先读实现**，再回来看名字合不合适。**从名字反推行为**是最省事也最容易错的路。

| 名字暗示 | 实际语义 | 我错在哪 |
|---|---|---|
| `done` 像**过滤器** | 它是**剪枝**：控制「进不进入」，不控制「列不列出来」 | 期望 `done` 里的任务**不出现在结果里** |
| `unknown: true` 像**必然结果** | 只有**走到需要那条边**时才知道不知道 | 期望「边没加载」**一定**导致 `None` |
| `node` 像**收窄后的归属字段** | 它是**记录本身**（归属字段的超集） | 用 `node_of(record)` 去比 |

**共同形态**：**按名字猜意图，没按实现读。**
**判据**：**凡是你"觉得它应该是这样"的地方，去读一遍实现**——三个实例都是这么来的。

## 权衡「要不要加防御检查」：**看违背的后果，不只看概率**

「这不大会发生」**不足以**成为不加检查的理由。要问的是：**发生了会怎样？**

| 违背的后果 | 加不加 | 理由 |
|---|---|---|
| **明确的错误**（崩、抛、测试红） | **可以不加** | 它**迟早会自己暴露**，而且暴露时就带着位置信息 |
| **静默挂起** | **要加** | 你**连"哪里错了"都不知道**；症状还会**出现在别处** |
| **静默错值** | **要加** | 同上——它比崩溃更难发现，因为它**看起来正常** |

**判据**：**权衡防御检查时，不只看「这会发生吗」，还要看「发生了会怎样」。**
**这是「静默失效」那一族从"要不要加检查"这个角度看的同一件事。**

**实例**：`_above()` 的上游**信任「归属图是棵树」**、没有环检测。
真实数据违反不了它——**但一次构造错误的输入就能让整个 `pytest` 挂住**，
而且**症状在别处**（"`pi-durable` 单独跑挂住、每个文件单独跑全绿"）。
**⇒ 本端加环检测、抛明确错误。**

## ⚠️ 不变量违反在 Python 里会变成**无限循环**（而 TS 那边可能是同一回事）

**实例（`scheduler.ts` 的 `_above`）**：它依赖「归属图是棵树」这条不变量，**没有环检测**。
我的一个测试构造了**环**（一个任务活在**它自己拥有的**对话里——真实数据产生不了，
**测试能**），于是 `_above` **无限循环**，**整个 `pytest` 挂住**。

⚠️ **这条代价极大**：它不是「一个测试失败」，而是**卡住所有人的门禁**——
**而且症状看起来在别处**（`pi-durable` 单独跑挂住、每个文件单独跑全绿）。

**⇒ 本端偏离**：`_above` 加**环检测**，违反不变量时**抛 `RuntimeError`** 而不是挂住。
上游没有这个检查（它信任不变量）——但**把挂起换成一个明确的错误**，
是唯一能在不变量被破坏时**救回诊断信息**的做法。

**判据**：**移植「靠不变量终止的循环」时，加一个环/迭代上限检测**——
成本是几行，收益是**把静默挂起换成可诊断的错误**。

## 验证设计：「用对方做不到的输入」证明能力边界

要证明**本包的 A 路径**真的支持而**对方的 B 路径**不支持，**不要挑两边都能过的输入**——
挑一个**只有 A 能做、B 做不到**的：

> **实例**：`structured_output_error` 走本包的 ``jsonschema``（全关键字），
> 而工具 ``parameters`` 走 pi-ai 的校验器（24 关键字，6 个被忽略）。
> 测试用 **``pattern``**——**它不在 pi-ai 那 24 个里**。
> 所以那条测试**不可能是"碰巧过"**：它过的唯一解释就是这一路真的全支持。

**判据**：**验证一个「我有而你没有」的能力时，输入必须落在对方的盲区里。**
否则两边都过的测试什么也没证明。

## 能力边界登记（**边界不是缺陷**）

| 边界 | 范围 | 说明 |
|---|---|---|
| **`validate_tool_arguments` 忽略 6 个 JSON Schema 关键字** | **工具 `parameters`** | 见下 |
| `pi_ai.Models` 目前 **9/24** | provider 门面 | 15 个成员未声明（架构裁决挡着）；durable 只用 5 个，全在 |

### `parameters` 的校验偏宽松（**能力边界，不是缺陷**）

pi-ai 的 `validate_tool_arguments` 支持 **24 个关键字**，
但下列 **6 个被忽略且不报错**：
``format`` / ``not`` / ``if-then-else`` / ``patternProperties`` / ``dependencies`` / ``contains``。

⚠️ **两条校验路径分工不同，别混**：

| 路径 | 校验器 | 关键字支持 |
|---|---|---|
| **工具 ``parameters``** | ``pi_ai.validate_tool_arguments`` | 24 个，**上述 6 个被忽略** |
| **``structuredOutputSchema``** | **本包的 ``jsonschema``** | **全关键字** |

**当前是否踩到**：**没有**。核查过上游**全部** schema 来源
（`parameters` 与 `structuredOutputSchema` 两条路都查），
``grep -rnE 'Type\.(Not|If|PatternProperties|Dependencies|Contains|Format)\b'`` **为空**——
实际只用到 `Object`/`Array`/`String`/`Number`/`Boolean`/`Optional`/`Unknown`/`Null`。

**但这是能力边界，不是"已经没问题了"**：``parameters`` 是**工具作者提供**的开放 schema，
第三方仍可能写 ``format`` 而得到**静默偏宽松**（该拒的没拒）。
**登记它，是为了让踩到的人知道去哪查，而不是假装它不存在。**

## 新增依赖登记

| 依赖 | 加入处 | 理由 | 上游对应物 |
|---|---|---|---|
| `pydantic>=2.7` | 批次 C₂ | 工具参数用 pydantic 模型定义、取 `model_json_schema()`，消除两个来源（偏离 #40） | TypeBox（`ToolRegistration.parameters`） |
| **`jsonschema>=4.20`** | **批次 D（D-e）** | **`tool.ts` 的 `Compile` 校验 `structuredOutputSchema`** | **`typebox/compile` 的 `Compile`（`tool.ts:13,557,573`）** |

**为什么是 `jsonschema` 而不是 pydantic**：pydantic 是「从 Python 类型生成/校验」，
而这里手上只有一个**运行时拿到的 JSON Schema**（`structuredOutputSchema`）。
要用 pydantic 就得**从 JSON Schema 动态建模**——绕了一层，且丢掉了 schema 本身的语义。
`jsonschema` 直接吃 JSON Schema，与 TypeBox 的 `Compile` **是同一件事**。

**为什么不自写校验器**：那等于**自己维护一个 JSON Schema 子集**——
看起来支持、实际只支持一部分，正是 R23 那类「刻意的 no-op」的近亲。
**要么用完整的实现，要么明确说不支持。**

## 批次 D 完结声明

**`harness/` 已从 0 覆盖到 10,510 行**，其中**代码 5,905 行**（注释 620 · 文档串 2,243 · 空行 1,742 ——
**规模口径见〈给「代码行」，不给「总行」〉**）。
`pi-durable` 共 **1,382 条测试**（+2 xfailed，带 reason），全仓 **2,987 passed**。

⚠️ **批次 D 的代码已全部落地**（`GenerationTask` + `CompactionTask` 本体在最后两轮完成），
**两处 `type: ignore[name-defined]` 都已删除**（mypy 各自在依赖落地的那一刻报了 `unused-ignore`）。

### 一、已完成（批次 D 的全部 20 个上游文件）

| 上游 | 本端 | 状态 |
|---|---|---|
| `types.ts` 的**纯数据契约** | `harness/types.py`(804) | ✅ |
| `json.ts` / `util.ts` | `harness/json.py`(78) / `util.py`(108) | ✅ |
| `define.ts` / `provider.ts` / `usage.ts` / `inbox.ts` | 同名 4 个模块 | ✅ |
| `context.ts` / `prompt.ts` / `agent.ts` | `context.py`(517) / `prompt.py`(209) / `agent.py`(461) | ✅ |
| `task-graph.ts` / `tasks.ts` | `task_graph.py`(288) / `tasks.py` | ✅ |
| **`scheduler.ts`** | `scheduler.py`(**2,882**) —— **63 个方法全部落地** | ✅ |
| `live.ts` / `compaction.ts` / `generation.ts` / `submissions.ts`（SCC） | 同名 4 个模块 | ✅ |
| `tool.ts` | `tool.py`(523) | ✅（叶子层；`ToolTask` 本体见欠账） |
| `runtime.py`(220) | **新增**：`TaskRuntime` 契约（上游在 `types.ts`） | ✅ |

### 二、判定不做（**已评估、故意不引入** —— 不是遗漏）

| 名字 | 理由（R28 **情形三**：形状已知，但**语言里没有对应物**） |
|---|---|
| `SessionDocToken` / `ConversationDocToken` / `TaskDocToken` + 三个 family 版 | 上游靠**类型参数**编 scope；本包 scope 在**运行期**由 `check_record_scope()` 校验。造别名**不提供静态保证** |
| `TypedEntry<D>` / `TypedEntryDraft<D>` | 类型参数运行期**被擦除**；给**别名 + 说明**，收窄交给 `EntryToken.is_()` |
| `Cursor` / `AnyTaskRecord` 的 `Any` | 批次 B 的既有形态，非本批次新开的口子 |

### 三、欠账（**未落地的东西根本不存在**；⚠️ 与「已覆盖」必须可区分）

| 项 | 状态 |
|---|---|
| `tool.ts` 的 `ToolTask` 本体 | ✅ **已完成**（含端到端 10 条测试） |
| `generation.ts`（6 个 helper + `GenerationTask` + 5 phase + abort） | ✅ **已完成**；⚠️ **欠账：没有端到端测试**（轮 5 的双向测试待做） |
| `compaction.ts`（`create_compaction` + `CompactionTask` + `place`/`place_summary`/`complete`） | ✅ **已完成**；⚠️ **欠账：没有测试** |
| `types.ts` 的**接口形状**那一半 | 已被逐项消费掉；**残余为空** |

**端到端覆盖情况（诚实口径 —— 三者分开，不可混）**：

| | 内容 |
|---|---|
| ✅ **已覆盖（E2E）** | `ToolTask`（10 条，真 Session × 2 后端 + 真调度器 + 真工具）· `stream_response`（3 条，真 `EventStream`）· 生成助手 4 个（7 条）· **`GenerationTask` 的 overflow → compaction → 回 `prepare`** · **`GenerationTask` 的 retry** · **「overflow 永不重试」** · **环的双向闭合**（2 条） |
| 🟡 **部分覆盖** | **`GenerationTask` 的 `blocking` 分支**：`owner`/`background=False`/`blocking is True` 已验（在 overflow 那条里顺带），**但「等待解开后生成真的继续了」未验** —— 见 `xfail` |
| ❌ **欠账（无 E2E）** | **`CompactionTask` 的本体** —— 两条 xfail，**根因与补法均已定位** |

**4 条 `xfail`（全部 `strict=True` + 带 reason）**：

| 测试 | 原因摘要 |
|---|---|
| `test_abort_after_a_restart_says_it_may_have_partially_run`（×2 后端） | `restart` 标记的语义是「保留标记并继续等定义」，**它本来就该停在 `pending`** —— 我构造了一个语义上不该走到终局的状态 |
| `test_compaction_abort_removes_the_status_and_leaves_no_half_summary` | ⚠️ **测试驱动错了调用者**：`_gated` 写的是 `invocation.task_id` 那个**生成任务**，不是压缩子任务 → 补法是**让 abort 跑在子任务自己的 invocation 下** |
| `test_a_blocking_compaction_resumes_the_generation_that_waits_for_it` | ⚠️ `ReadAfterWrite` **在 `create_compaction` 内部**（它自己写 `tx.doc`）→ 补法是**拆事务边界** |

⚠️ **全部 4 条都是"测试没接上"，不是"代码没写完"** —— 且**都没有验证到对应行为**，别当覆盖。

### 🎯 端到端找到的 8 个集成 bug —— **「两半各自正确、合起来错」**

`ToolTask` 的端到端测试（`test_harness_tool_task_e2e.py`，真 Session × 2 后端 + 真调度器 + 真工具）
**一行都跑不起来**时开始，逐层剥出 8 个 bug。**全部 8 个都通过了各自的单元测试。**

| # | 症状 | 根因 | 类别 |
|---|---|---|---|
| 1 | `unhashable type: 'dict'` | 把 `create_root_conversation()` 的**记录**当 id | 测试错 |
| 2 | `'dict' has no attribute 'initial'` | **批次 B 的 `_create_task` 用属性访问；批次 D 的 `define_task` 产出 dict** | **契约的两种表示在集成点碰撞** |
| 3 | `entry() takes 3 positional arguments but 4` | **本端把一条读拆成两条**（`entry` / `entry_in_conversation`），集成点用了拆前的那条 | 本端刻意偏离 |
| 4 | `change() takes 1 positional argument but 2` | **JS 容忍回调少写参数，Python 不容忍** | 语言差异 |
| 5 | `'dict' has no attribute 'id'` | `append_tool_result` 按**属性**读 JSON dict 的 call | R26 |
| 6 | `'dict' has no attribute 'task_id'` | 同上 | R26 |
| 7 | `'dict' has no attribute 'task_id'`（scope） | **`_gated` 传了 dict 当 `TransactionScope`** | 契约的两种表示 |
| 8 | `'dict' has no attribute 'model_copy'` | `validate()` 按 **pydantic `ToolCall`** 写，而调用方给的是 dict | R26 |

#### 两条新通则

**〈JS 回调的形参是"可少不可多"，Python 是"必须匹配"〉**
上游 `(tx, current)` 的签名下**大量调用点只写 `(tx)`**。
**判据：移植任何"回调类型"时，先数一遍上游调用点实际写了几个参数。**
（与「`T | Promise<T>` → 可能不可 await 的 awaitable」同族：**TS 的类型宽容 vs Python 的严格**。）

**〈本端刻意拆分的 API，集成点必须用拆分后的那条〉**
上游一条 `storage.entry(conversationId, id, context)`；本端拆成
`entry(id, context)`（全局）与 `entry_in_conversation(conversationId, id, context)`（本对话可见）。
**按上游形态调用的代码会报错——而它在单元测试里不会暴露**（测试直接调它，绕过了集成点）。
⇒ **这类偏离要登记进偏离索引，并在集成点复查。**

#### 一个"不报错"的 bug

`_RuntimeView.now()` 我写了 `float(self._scheduler._now())`，而宿主给的是 **int 毫秒**。
于是 `timestamp` 变成 `1.0`，pydantic 的字段是 `int`——
**它不抛、只发一条 `PydanticSerializationUnexpectedValue`**，而那条记录**照常写进存储**。
**判据：序列化警告要当错误看**——它意味着**存下去的东西与声明的类型不符**，
而读它的人（可能是别的语言、别的版本）会拿到意外的东西。

### 📏 规模统计的口径：**给「代码行」，不给「总行」**

**行数比在注释密度不同的两种语言之间没有意义。** 本批次实测：

| | 上游 | 本端 |
|---|---:|---:|
| 总行 | 1,954 | 2,883 |
| **代码行** | **1,511** | **1,634** |

**代码 +123 行（+8%）**，而总行差 929 全是文档串与空行。
**本端代码行（1,634）甚至少于上游的「总行数」（1,954）**。

**⇒ 以后所有规模报告都用「代码行」**；量法：`总行 − 注释 − 文档串 − 空行`
（文档串用 `ast.get_docstring` 累计，别手数）。

### 四、R 系列最终索引（32 条）

**排序方法论**：R21（三面 + 零价的替代物）。**移植差异**：R1~R20、R22~R27、R30。
**Python 特有陷阱**：R24（Draft/JSON）、R26（dict vs 对象）、R31（静默出错症状索引）、R32（故意的分叉不一致）。
**契约定义判据**：R28（三情形）、R29（三态不能压成两态）。

**另有 93 处偏离**逐条登记在〈有意偏离上游〉。

## 批次 B 完结声明

**Session 运行时（`session/` 4 个文件，2,513 行）至此完结**，批次 B 的**存储内核 + Session 运行时**全部交付。

### 已交付

| 上游 | 行数 | 本包模块 | 测试 |
|---|---:|---|---:|
| `types.ts`（存储子集） | — | `pi_durable.types` | — |
| `ids.ts` / `errors.ts` | 68 | `pi_durable.ids` / `errors` | — |
| `documents.ts` / `entries.ts` | 242 | `pi_durable.documents` / `entries` | 23 |
| `storage/scan.ts` | 40 | `pi_durable.storage.scan` | — |
| `storage/memory.ts` | 864 | `pi_durable.storage.memory` | 37 |
| `storage/sqlite/*`（除 cloudflare） | 1,428 | `pi_durable.storage.sqlite.*` | 5 + 24×2 |
| **`testing/storage-conformance.ts`** | **1,663** | `pi_durable.testing.storage_conformance` | **24 × 2 后端** |
| `session/observation.ts` | 292 | `pi_durable.session.observation` | 26 |
| `session/forks.ts` | 97 | `pi_durable.session.forks` | （含在上面） |
| **`session/transaction.ts`** | **1,043** | `pi_durable.session.transaction` | 89 |
| **`session/session.ts`** | **1,081** | `pi_durable.session.session` | 76 |
| **合计** | **7,817** | | **≈ 300** |

**主验收：`storage-conformance` 24 条用例 × 内存 / SQLite 两个后端 = 48 条全过。**
**「失败即终局」：11 条 × 2 后端 = 22 条全过。**

### 未交付（明确列出）

| 项 | 规模 | 说明 |
|---|---|---|
| `types.ts` 的 `Session` / `Tx` / `TaskRuntime` **完整协议** | — | 只交付了运行期真正用到的窄提取（`TaskDefinition` 的 `name`/`version`/`initial`）。完整协议的形状要等批次 D 的 scheduler 定 |
| `Session.snapshotAsOf` / `snapshot_as_of` | — | Session 接口里多出来的那一个方法（as-of 读）。`snapshot` / `document_state` / `watch_doc` 已交付 |
| `Task` / `TaskRuntime` 的运行时（`phases` / `abort` / `migrate` / `hooks`） | — | 批次 D（scheduler） |
| **批次 D**：`harness/scheduler.ts` 等 | ≈ 8,100 | `scheduler.ts` 一个文件就 1,953 行 |
| **批次 E**：`codemode/` | — | 未开工 |
| `storage/sqlite/cloudflare.ts` | 192 | 🚫 明确排除（`ctx.storage`，Python 无对等物） |

**没有半成品**：所有已交付模块都能 import、过 ruff/mypy、有测试；
未交付的方法**不存在**而不是「存在但抛 `NotImplementedError`」。

## 批次 D-1（本包已交付）：harness define 层

上游 `durable/src/harness/` 共 **21 个文件 / 8,177 行**，是整个包里最大的一块
（`recon/06-durable-breakdown.md` §1.1）。批次 D 的完整范围见该报告 §6，本包**只做了
其中解锁 C₂ 所需的一小片**。

### 已交付

| 上游 | 行数 | 本包 | 内容 |
|---|---:|---|---|
| `harness/define.ts:6-17` | 12 | `pi_durable.harness.define` | `define_tool` / `define_extension`（**值依赖**，工具对象由它们构造） |
| `harness/types.ts` 的窄提取 | — | `pi_durable.harness.types` | `ToolRegistration`、`ToolExecutionApi`（7 成员）、`ToolExecutionResult`、`ToolDiagnostic`、`ToolControl`、`ToolOutputLimits`、`RetainedOutput`、`Agent`（4 字段）、`Extension`（2 字段）、`ModelRef`、`Models`（1 方法） |

### 类型闭包：24 / 46（供后续估工作量）

以 C₂ 需要的 5 个类型
（`ToolRegistration`、`ToolExecutionApi`、`ToolExecutionResult`、`ToolDiagnostic`、`Extension`）
为根，脚本算出的**类型传递闭包是 `harness/types.ts` 46 个导出类型中的 24 个**：

```
Agent AnyTask CompactionPolicy ConversationRetryPolicy ConversationStreamOptions EnvTarget
Extension HarnessOptions HarnessSettings HookRegistration ModelRef NestedToolExecutionResult
ProgressPolicy PromptInput PromptSection RegistryReader RegistrySnapshot SettledTask
ToolControl ToolDiagnostic ToolExecutionApi ToolExecutionMode ToolExecutionResult ToolRegistration
```

其中 `HarnessOptions` / `HarnessSettings` / `Agent` / `RegistryReader` / `RegistrySnapshot` /
`SettledTask` / `PromptInput` / `PromptSection` / `AnyTask` / `CompactionPolicy` 等**依赖批次 B
的 L0 内核或批次 D 主体**，因此本段**没有**移植它们，只保留了工具定义与执行真正用得上的
那一支（见 `harness/types.py` 的模块文档，那里逐条列了省略项与原因）。

**未在闭包里的 22 个**（`AgentChange`、`AgentState`、`CompactionHooks`、`CompactionReason`、
`ContextView`、`Conversation`、`ConversationAbortOptions`、`ConversationInit`、
`ConversationWatch`、`HarnessInspection`、`HookApi`、`HookResult`、`HooksOf`、
`InputSubmissionDraft`、`Registry`、`Settings`、`Submission`、`SubmissionDraft`、
`TaskInspection`、`ToolHookCall`、`ToolHooks`、`UserInput`）本段自然也没做。

### ⚠️ 批次 D 仍未做的部分（**别以为 D 做完了**）

`harness/` 的 21 个文件中，本包只触及 `define.ts`（两个函数）与 `types.ts`（窄提取）。
**下列 19 个文件、约 8,100 行一行未写**：

| 上游文件 | 行数 | 作用 |
|---|---:|---|
| `harness/scheduler.ts` | **1,953** | 任务调度器：所有权索引、撤销级联、预留、终局化 |
| `harness/tool.ts` | 911 | `ToolTask`：工具任务的完整生命周期（含嵌套调用） |
| `harness/types.ts` | 812 | 完整类型面（本包只取了窄提取） |
| `harness/generation.ts` | 715 | 生成任务：模型请求、重试、溢出、延迟句柄 |
| `harness/events.ts` | 506 | `watchEvents` 与 agent 事件流 |
| `harness/harness.ts` | 470 | `Harness` 本体 |
| `harness/compaction.ts` | 464 | 压缩任务 |
| `harness/context.ts` | 333 | 上下文视图与派生 |
| `harness/agent.ts` | 314 | `AgentDoc`、`configure`、默认策略 |
| `harness/live.ts` | 268 | `LiveDoc` 与进度槽 |
| `harness/view.ts` | 267 | `ConversationView` |
| `harness/submissions.ts` | 241 | 提交（submission） |
| `harness/task-graph.ts` | 236 | 任务图视图 |
| `harness/inbox.ts` | 168 | `InboxDoc` |
| `harness/prompt.ts` | 158 | 系统提示与工具声明计划 |
| `harness/registry.ts` | 114 | `createRegistry` |
| `harness/usage.ts` | 72 | `UsageDoc` |
| `harness/util.ts` | 61 | 小工具 |
| `harness/json.ts` | 31 | JSON 辅助 |
| `harness/provider.ts` | 39 | `ProviderDoc`（会话 UUID） |
| `harness/define.ts` 的另外 4 个函数 | 32 | `hook` / `section` / `wrapTool` / `wrapSection` |

外加 `harness/` 之外的 `session/`、`storage/`、`types.ts`、`documents.ts`、`entries.ts`、
`tasks.ts`、`errors.ts`、`ids.ts`、`codemode/`（批次 B / E）。


| 轮询模式实现 | `env/node-watch.ts:228-234`、`317-405` |
| 轮询内容哈希 | `env/node-watch.ts:54-58`、`334-345` |
| 原生 watcher | `env/node-watch.ts:182-186`、`407-478` |
| 目录预算 | `env/node-watch.ts:76`、`327-332` |
| 监听排除规则 | `env/node-watch.ts:129-131`、`361-362` |
## 证据索引（上游文件:行号）

| 主题 | 位置 |
|---|---|
| 快照差分是权威语义 | `env/node-watch.ts:133-138`、`284-299`、`301-315` |
| 结果类型 | `env/index.ts:4-31` |
| 错误码表 | `env/index.ts:35-75` |
| 二进制读取器契约 | `env/index.ts:95-108`；实现 `env/node.ts:455-541` |
| 行扫描契约 | `env/index.ts:100-106`、`141-157`；实现 `env/node.ts:508-534`、`line-scan.ts` |
| 目录读取器契约 | `env/index.ts:159-168`；实现 `env/node.ts:543-597` |
| 监听契约 | `env/index.ts:110-138`、`257-266` |
| shell 契约 | `env/index.ts:287-372`；实现 `env/node.ts:652-894` |
| 超时/中止/退出码 | `env/node.ts:59-70`、`305-384`、`871-889` |
| 临时文件/目录 | `env/node.ts:1195-1219` |
| env-conformance 用例 | `testing/env-conformance.ts:114-633` |
| 断言原语 | `testing/assertions.ts:17-39` |
| 运行器适配 | `testing/runner.ts:28-38` |
| 用例类型与超时 | `testing/types.ts:25-46` |
| chord 依赖面（只有 2 个值符号） | `testing/env-conformance.ts:2`；`env/index.ts:1`、`env/node-watch.ts:5`、`env/node.ts:23` 均为 `import type` |

### 批次 C

| 主题 | 位置 |
|---|---|
| 截断的两个独立上限与默认值 | `truncate.ts:4-13` |
| 永不返回半行 | `truncate.ts:8` |
| 行计数（末尾换行不算新行） | `truncate.ts:50-55` |
| 首行超字节上限的处理 | `truncate.ts:119-135` |
| `truncatedBy` 的判定顺序 | `truncate.ts:155-156` |
| `outputBytes` 对拼接结果重算 | `truncate.ts:158-159` |
| `truncateHeadOf` 的前缀契约 | `truncate.ts:86-89` |
| `formatSize` 的分档 | `truncate.ts:60-68` |
| `utf8ByteLength` 的两条路径 | `chord/src/output.ts:272-294` |
| `characterEnd` | `chord/src/output.ts:101-106` |
| BM25 参数与公式 | `search.ts:52-85` |
| 停用词表 | `search.ts:8-30` |
| 朴素单数化 | `search.ts:33-38` |
| camelCase 切分先于小写化 | `search.ts:43-45` |
| 同分保持文档顺序 | `search.ts:81-83` |
| 图像上限默认值 | `tools/image-processor.ts:11` |
| 可内联的 MIME 集合 | `tools/image-processor.ts:40` |
| `toBase64` 的分块原因（issue #10923） | `tools/image-processor.ts:42-53`；`CHANGELOG.md:16` |
| `base64Length` | `tools/image-processor.ts:55-57` |
| MIME 判定（含 JPEG 0xF7、APNG） | `tools/image.ts:46-53`、`61-73` |
| 按 ByteSource 的头部判定与块遍历 | `tools/image.ts:16-44` |
| 各格式的头部尺寸解析 | `tools/image.ts:79-119` |
| BMP 的 DIB 头校验 | `tools/image.ts:121-143` |
| EXIF 方向（JPEG APP1 / WebP EXIF） | `images/exif.ts:30-59` |
| 方向的取值范围 | `images/exif.ts:4`、`20-24` |
| 处理器行为全流程 | `images/index.ts:37-89` |
| 编码候选顺序与 `jpegFirst` | `images/index.ts:95-117` |
| 0.75 递减缩放 | `images/index.ts:67-82` |
| 5..8 的手写像素搬运 | `images/index.ts:119-139` |
| 大图耗时说明（约 1.5 s / 12 MP） | `images/index.ts:29` |
| 路径归一化与变体探测 | `tools/path-utils.ts:4-29` |
| 同文件改动串行化与规范化键 | `tools/file-mutation-queue.ts:5-55` |
| `canonical()` 的父目录回退 | `tools/file-mutation-queue.ts:12-26` |
| `joinPath` 必须做词法归一化 | `env/node.ts:648-650` |
| `defineTool` / `defineExtension`（批次 D 阻塞点） | `harness/define.ts:6-17` |
| `ToolRegistration` 字段 | `harness/types.ts:293-317` |
| Node 侧 worker 与空闲退出 | `images/node.ts:15-18`、`115-123` |
| Cloudflare WASM 导入（已排除） | `images/cloudflare.ts:1-2` |
| `diff`(npm) 的使用点 | `tools/edit-diff.ts`（客户端 500 行，逐字节等同 v0.85.1） |

### 批次 D-1

| 主题 | 位置 |
|---|---|
| `defineExtension` / `defineTool`（恒等函数） | `harness/define.ts:6-10`、`13-17` |
| `section` / `hook` / `wrapTool` / `wrapSection`（**未移植**） | `harness/define.ts:20-26`、`29-31`、`34-36`、`39-44` |
| `ToolRegistration` 完整定义 | `harness/types.ts:293-317` |
| `ToolRegistration` 继承 pi-ai `Tool` | `harness/types.ts:293-297` |
| `ToolExecutionApi` 完整定义（20+ 成员） | `harness/types.ts:205-292` |
| `ToolExecutionApi` 继承的两个 trait | `harness/types.ts:205` |
| `details(value: TDetails, …)`（唯一用泛型的成员，**未移植**） | `harness/types.ts:286` |
| `usage` / `output` 的 pi-ai 来源 | `durable/package.json:60-67`；`harness/types.ts:2-16`、`158`、`164` |
| `ToolDiagnostic` | `harness/types.ts:144-149` |
| `ToolControl` | `harness/types.ts:137-142` |
| `ToolExecutionResult` | `harness/types.ts:154-167` |
| `ToolExecutionMode` | `harness/types.ts:196` |
| `ToolOutputLimits` 内联定义 | `harness/types.ts:329-335` |
| `Agent` | `harness/types.ts:447-466` |
| `Extension` | `harness/types.ts:365-375` |
| `ModelRef` | `harness/types.ts:47-50` |
| `Models` 的 8 个方法（本端只留 `getModel`） | `harness/types.ts:215` |
| C₂ 的 `api.*` 实测用法 | `tools/env.ts:6-7`、`tools/bash.ts:104,107,117,126`、`tools/read.ts:201-202`、`tool-search.ts:67` |
| C₂ 的 `defineTool` 调用点 | `tools/bash.ts`、`tools/read.ts`、`tools/write.ts:16-38`、`tools/edit.ts`、`tool-search.ts:55-87` |
| C₂ 的 `defineExtension` 调用点 | `tools/index.ts:31-36`、`tool-search.ts:90-92` |
| `expandedTool` / 模型可见 schema 的转换 | pi-ai `Tool.to_json_schema()`（去掉顶层 `title`） |

### 批次 B

| 主题 | 位置 |
|---|---|
| 记录持久化为 JSON | `storage/sqlite/migrations.ts:28`（`CHECK (json_valid(record))`） |
| ID / 序列号的擦除品牌 | `types.ts:20-40`；信任边界 `ids.ts:3-11` |
| 四条错误类型的语义 | `errors.ts:3-57`；`StorageRequestError` 只让那次读失败见 `errors.ts:24-34` |
| 游标带着自己的方向 | `storage/scan.ts:10-35` |
| 分页与续扫 | `storage/scan.ts:37-40`；`storage/memory.ts:202-210` |
| 二分索引与有序 ID 数组 | `storage/memory.ts:130-169` |
| 提交前校验（全局 ID 命名空间） | `storage/memory.ts:733-753` |
| 文档动作归并与校验 | `storage/memory.ts:755-815` |
| 文档物化（选定基点 + 增量重放） | `storage/memory.ts:659-676` |
| fork 感知的可见条目（新→旧 / 旧→新） | `storage/memory.ts:678-731` |
| `document.copy` 的解析与校验 | `storage/memory.ts:291-328` |
| 解除关联（读出的记录与留存写入） | `storage/memory.ts:94-114` |
| 一致性套件（**已移植，24 条用例**） | `testing/storage-conformance.ts`；用例名见其 `createStorageConformance` |
| 一致性套件覆盖三个后端 | `docs/spec.md` §10 末句 |
| SQLite 的 9 表 / 16 索引 | `storage/sqlite/schema.ts:5-34` |
| `STRICT` 表 + `json_valid` CHECK | `storage/sqlite/migrations.ts:28`、`49`、`61`、`75`、`87` |
| `next_id` 必须是 TEXT | `storage/sqlite/migrations.ts:12` |
| PRAGMA 三连 + busy_timeout | `storage/sqlite/node.ts:20-21`、`194-196` |
| 事务的回滚约定（回滚失败要用不同错误） | `storage/sqlite/database.ts:18-34` |
| 加密/串行操作队列 | `storage/sqlite/node.ts:31-120` |
| 索引字符串用 JSON 编码 | `storage/sqlite/storage.ts:75` |
| `retired_at` 与 `record` 同步 | `storage/sqlite/storage.ts`；用例 *uses bases for version transitions…* |
| 地址身份 = `JSON.stringify([kind, scopeKind, owner, key ?? null])` | `documents.ts:137-145` |
| 重载参数表解析 | `documents.ts:109-127` |
| `history`/`fork` 只盖在会话文档上 | `documents.ts:148-165` |
| 语义与版本检查 | `documents.ts:168-190` |
| 物化与迁移（只返回值，不写回） | `documents.ts:198-208` |
| 条目种类与类型守卫 | `entries.ts:5-34` |
| Session 运行时的导入面（= 定义层交付的函数） | `session/session.ts:4-11` |
| 失败即终局的语义 | `session/session.ts:106-110`、`210-218` |
| 文档缓存逐出 | `session/session.ts:96-104`、`115-125` |
| 提交变更的 4 条规则 | `session/transaction.ts:67-79` |
| 读后写不变量 | `session/transaction.ts:922-930` |
| 任务读取记忆化 | `session/transaction.ts:980-984` |
| 结算/放置不是表读取 | `session/transaction.ts:442-457` |
| 回调结算要排空未完成操作 | `session/transaction.ts:665-690` |
| 提交变更在装配期解析 | `session/transaction.ts:822-827` |
| 会话暂存（含分叉副本与创建钩子） | `session/transaction.ts:301-345` |
| 条目追加：`head: "self"` → 自身 ID，`byTaskId` 来自作用域 | `session/transaction.ts:351-381` |
| 裸提交：不施加准入规则 | `session/transaction.ts:430-438` |
| 装配顺序（校验 → 解析变更 → 拼批） | `session/transaction.ts:745-850` |
| 拥有者校验：不能在自己结束的提交里创建工作 | `session/transaction.ts:855-880` |
| `omitUndefinedProperties`：未定义的键整个省略 | `session/transaction.ts:369-377` |
| 任务创建：子任务继承拥有者会话、不能后台 | `session/transaction.ts:383-429` |
| `setTask` 的三条约束 | `session/transaction.ts:459-475` |
| 生命周期盖章（`??` 语义） | `session/transaction.ts:950-964` |
| 装配期对「替换」的校验 | `session/transaction.ts:786-796` |
| `??` 只跳过 null/undefined | MDN Nullish coalescing；对照本仓 R12 |
| `doc()` 的草稿复用与 fork 副本分支 | `session/transaction.ts:512-541` |
| `retireDoc()` 的四条分支 | `session/transaction.ts:557-586` |
| `#acquire`：先试已加载，再创建 | `session/transaction.ts:588-623` |
| 分叉副本的校验与物化 | `session/transaction.ts:625-650` |
| `planDocument` 的四类目标 | `session/transaction.ts:990-1039` |
| `publishes()`：迁移-only base 也要发布 | `session/transaction.ts:1041-1043` |
| 终局任务退休它名下每个文档 | `session/transaction.ts:798-810` |
| 分叉事务不得改来源 | `session/transaction.ts:882-901` |
| `adopt()`：指针交换式套用 | `session/transaction.ts:700-742` |
| `#loadDocument`（阶段 3 的宿主实现） | `session/session.ts:823-852` |
| 失败即终局的契约 | `session/session.ts:96-110`、`210-218` |
| `GuardedStorage.#call` 的豁免判定 | `session/session.ts:1037-1058` |
| `#enqueue` 的 promise 链 | `session/session.ts:854-861` |
| `close()` 的封口顺序 | `session/session.ts:482-520` |
| `assertUsable` / `#assertHealthy` | `session/session.ts:864-871` |
| 逐出：touch / drop / schedule / sweep / pin | `session/session.ts:598-690` |
| 保留期的合法化与默认值 | `session/session.ts:78-94` |
| 「定时器无法 unref 就降级」 | `session/session.ts:614-658` |
| `snapshot` 命中缓存不走线 | `session/session.ts:293-311` |
| `documentState` / `watchDoc` | `session/session.ts:331-448` |
| `#attachDocument` 与 `observedOperations` | `session/session.ts:780-817`、`890-901` |
| `closeSession(failure.error)` | `session/session.ts:220-224` |
| `#runCommit` 的完整链路 | `session/session.ts:690-742` |
| `#publish` 的两类监听器 | `session/session.ts:744-776` |
| `contained`：宿主回调不得让调用方失败 | `session/session.ts:878-888` |
| `subscribeCommits` / `observeCommits` / `subscribeClose` | `session/session.ts:544-566` |
| 关闭监听器各自独立 | `session/session.ts:522-527` |

### 批次 C₂

| 主题 | 位置 |
|---|---|
| 行尾检测 / LF 归一化 / 还原 | `tools/edit-diff.ts:7-20` |
| 模糊匹配的归一化链（NFKC → 去尾空白 → 引号 → 连字符 → 空白） | `tools/edit-diff.ts:27-51` |
| 保留未改动行的替换应用 | `tools/edit-diff.ts:118-169` |
| 先精确后模糊的查找 | `tools/edit-diff.ts:197-241` |
| 出现次数在归一化空间里数 | `tools/edit-diff.ts:248-252` |
| 四条错误消息（上游测试按正则断言） | `tools/edit-diff.ts:254-290`；`test/tools.test.ts:400,407,410` |
| 重叠检测 | `tools/edit-diff.ts:342-351` |
| **仅有的 2 处 npm `diff` 依赖** | `tools/edit-diff.ts:367`（`createTwoFilesPatch`）、`:382`（`diffLines`） |
| 主验收：patch 能被应用 | `test/tools.test.ts:369` |
| `write` 的形状与描述 | `tools/write.ts:9-38` |
| `edit` 的参数修复 | `tools/edit.ts:45-71`；测试 `test/tools.test.ts:373-390` |
| `edit` 的 BOM / 行尾处理 | `tools/edit.ts:116-122` |
| `read` 的 offset/limit 与行选择 | `tools/read.ts:222-312` |
| `read` 的头部读取与截断判定 | `tools/read.ts:54-78` |
| `read` 的图像分支 | `tools/read.ts:138-191` |
| `read` 的模型图像能力解析 | `tools/read.ts:196-212` |
| `bash` 的输出窗口 / spill / 非零退出 | `tools/bash.ts:82-136` |
| `bash` 的超时校验 | `tools/bash.ts:55-63` |
| `powershell` 的 argv 直启 | `tools/bash.ts:160-183` |
| `tool_search` 的检索文本与加载 | `tool-search/tool-search.ts:32-87` |
| `coding-tools` 扩展 | `tools/index.ts:27-39` |


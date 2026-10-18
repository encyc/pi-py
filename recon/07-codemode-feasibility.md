# 07 — codemode 的 JS 沙箱在 Python 侧能否落地（调研 G）

- **任务**：task-7 / 调研 G。判定 `@earendil-works/pi-codemode` 在 pi-py（Python）侧能否落地。
- **上游基线**：`/tmp/pi-up/v1.3.0/`（只读），codemode 35 个文件 / 5851 行；消费方 `packages/coding-agent/` 与 `packages/durable/`。
- **联网查证日期**：2026-10-18（Asia/Shanghai）。所有外部结论均标注来源 URL 与版本/发布日期。
- **pi-py 现状**：Python >= 3.11（`pyproject.toml`），本地 venv 为 **Python 3.13.3**；无任何 JS 引擎依赖（`.venv` 内无 quickjs/v8/wasm/duck 相关包）。

---

## 0. 结论速览

| 问题 | 回答 |
| --- | --- |
| Python 侧能否实现与上游行为等价的 codemode？ | **能，但有条件**（不是开箱即用，需要自建一层「宿主」代码，见 §4） |
| 硬需求「能否中断正在执行的脚本」满足吗？ | **满足，且能做到比上游更强**（详见 §3.1 / §4.2）：所选路线同时具备「VM 内中断轮询」与「wasm 层强制 trap」两级中断 |
| 推荐引擎 | **wasmtime（Python 绑定）+ 上游同一份 `quickjs.wasm`（vercel-labs/quickjs-wasi 3.6.2，MIT，637,405 字节，sha256 `d4c9375f…bbd9`）**，每次 `execute()` 用一个子进程承载 VM |
| 最大风险 | 需要**从零写一个「Python ↔ quickjs.wasm C ABI 宿主」**（上游没有这段代码，估 600–1200 行），且该 ABI 是 vercel-labs 的**非稳定私有契约**（月更）；若这条路走不通，降级到 V8 路线会损失「忠实镜像」的语义一致性 |
| 一行结论 | **codemode 可以镜像；引擎选型不是「能不能跑 JS」，而是「宿主层谁写、绑定谁的中断契约」。** |

---

## 1. 职责与运行时要求

### 1.1 codemode 是什么

一句话（上游自述，`README.md:3`）：**在 QuickJS（编译成 WebAssembly）里跑模型写的 JavaScript，脚本唯一的能力是调用注入的工具；嵌套的工具调用不进入 LLM 上下文，只有脚本的输出与返回值进入。**

用途有两处（不是同一个场景，pi-py 两条都要镜像）：

1. `packages/coding-agent/src/extensions/codemode/` —— 把 agent 的全部工具暴露给脚本，作为内置 `codemode` 工具给模型用（`execute.ts:502` `new CodemodeSandbox({...})`），一次执行一个 worker。
2. `packages/durable/src/codemode/codemode.ts` —— 用**确定性重放**（`sandbox.start()`）让会话在重启后能把脚本"接着跑"（`codemode.ts:10` 注释、`codemode.ts:460` `this.run = this.options.sandbox.start(code, {...})`）。

三个 sandbox 实现共享同一套 host 逻辑、同一份 prelude、同一份 VM 驱动代码，只换"VM 跑在哪"（`README.md:7-14`）：

| Sandbox | VM 位置 | 入口 | 关键差异 |
| --- | --- | --- | --- |
| `CodemodeSandbox` | 每次执行一个 Node/Bun **worker thread** | `pi-codemode` | 有 `wasm`/`workerUrl`/`acquireWorker`/`onWorkerStarted` |
| `InlineCodemodeSandbox` | **调用线程**（Cloudflare / 无 worker 的运行时） | `pi-codemode`（也 re-export） | 多一个 `interruptBudget`；阻塞调用者 |
| `RemoteCodemodeSandbox` | 另一个 isolate（Durable Object） | `pi-codemode/portable` | 走 Workers RPC；`wasm` 等选项不适用 |

pi-py 只需要前两个的语义（无 Cloudflare/Durable Object 场景），但 `CodemodeSandboxBase.start()`（确定性重放）必须保留。

### 1.2 一次 `execute()` 的完整生命周期（源码级）

1. `Execution` 构造：装载 deadline 定时器与 abort 监听（`src/runtime/execution.ts:214-226`，默认 `DEFAULT_TIMEOUT_MS = 300_000`，见 `execution.ts:49`）。
2. 组装 `VmStartData`（纯数据：code / tools{name,jsName,description} / globals / `memoryLimitBytes` / store / 可选 `deterministic`），交给 launcher（`execution.ts:229-257`）。
3. launcher 起 VM：`CodemodeSandbox` 走 `workerLauncher()`（`src/runtime/host.ts:20-75`）——创建一个 worker 线程、一块 `SharedArrayBuffer(4)` 作为中断标志、把编译好的 wasm 模块通过 structured clone 传进去（`host.ts:28-37`）。
4. worker 里 `startVm()`（`src/runtime/vm.ts:160-341`）：`QuickJS.create({ wasm, memoryLimit, maxStackSize, interruptHandler, wasi, ... })`（`vm.ts:170-179`），其中 `interruptHandler: options.shouldInterrupt`（`vm.ts:176`），worker 的 `shouldInterrupt` = `Atomics.load(interrupt,0) !== 0`（`src/runtime/worker.ts:28`）。
5. 在 VM 里 eval `PRELUDE_SOURCE`（`vm.ts:302`，prelude 原文在 `src/runtime/prelude-source.ts:48-522`），拿到 `{settle, run, stalled}`；再 eval 脚本本体 `(async (tools, console) => {<code>\n})`（`vm.ts:327`）。
6. 脚本调用 `tools.x(args)` → prelude 里 `bridge("call", id, name, json)`（`prelude-source.ts:197-211`）→ C 侧 host 回调 → worker 把 `{type:"call",...}` postMessage 给 host。
7. host 自己执行工具（`execution.ts:378-408`，为每次调用建一个 `AbortController`），结果 postMessage 回 worker；worker 调 `settle(id, ok, payload, properties)` 并在 VM 空闲时重新驱动脚本（`vm.ts:189-201`）。
8. 终态只有第一个生效：`done` / `crash` / launcher 失败 / deadline / abort（`execution.ts:410-448`）。`finish()` 会 abort 所有在途工具调用、请求 channel 停止，等请求完成后 resolve 结果。
9. 停止的实际语义（**1.3.0 的 Breaking**，`CHANGELOG.md:5-7`）：结果与 `close()` 在**请求**停机后即结算，**不等 worker 真的退出**。需要真实退出的宿主用 `onWorkerStarted(exited)` 或 `acquireWorker()` 的 release 回调（`CHANGELOG.md:11-12`、`types.ts:149-162`、`host.ts:30-38,68`）。coding-agent 正是这么用的（`execute.ts:514` 的 `acquireWorker` 做并发准入）。

### 1.3 对 JS 引擎的硬性要求（逐条附源码证据）

| # | 要求 | 为什么（证据） |
| --- | --- | --- |
| **R1** | **能在执行中被打断（核心判据）** | host 拥有 deadline 与 abort；触发时先置中断标志、再 terminate（`host.ts:59-66`；`README.md:366`）。`InlineCodemodeSandbox` 完全依赖引擎的 interrupt 轮询来防死循环（`inline.ts:10-20,77-82`） |
| **R2** | 中断回调必须是**宿主同步回调**，且能"抛一个脚本抓不住的错" | `interruptHandler: shouldInterrupt`（`vm.ts:176`）；注释明确"Returning true throws an uncatchable error in the script"（`vm.ts:12-16`） |
| **R3** | 能**单步驱动 promise job 队列**（`await` 死循环也要能被中断） | `vm.ts:238-250` 用 `vm._getExports()` 拿到内部 `qjs_is_job_pending` / `qjs_execute_pending_job`，每 `JOBS_PER_POLL=50`（`vm.ts:154`）个 job 轮询一次中断；`CHANGELOG.md:35` 记录 1.2.0 修的就是 `while (true) await null` |
| **R4** | 可设**内存上限**，超限在脚本内可捕获 | `memoryLimitBytes`（`types.ts:127-131`）；`README.md:60`：超限表现为 `InternalError: out of memory` |
| **R5** | 可设**栈上限**（深递归变成可捕获的 RangeError，而不是整个进程/实例 trap） | `vm.ts:52-57`、`README.md:372` |
| **R6** | 每次执行一个**全新 VM 实例**，有独立堆 | `README.md:362`；`host.ts:12-19` 一个 worker 一个 VM；`quickjs-wasi` 的设计也是每次 `QuickJS.create()` 一个 wasm 实例 |
| **R7** | **无 timers / 无 I/O / 无 fetch / 无模块**（脚本只能通过注入的工具接触外界） | `README.md:56`；prelude 只注入 `tools/ALL_TOOLS/text/image/exit/store/load/console`（`prelude-source.ts:451-456`） |
| **R8** | **JSON 是唯一的跨界编码**（参数、结果、store 都是 JSON 文本） | `protocol.ts:3-8`、`vm.ts:262-265`、`execution.ts:104-129` |
| **R9** | 可控的**虚拟时钟 / 种子随机 / UTC / 无 WeakRef**（确定性重放） | `vm.ts:102-125`（WASI clock+random 覆写）、`vm.ts:178`（`timezoneOffset: 0, intrinsics: ALL & ~WEAK_REF`）、`README.md:318-326` |
| **R10** | **WASM 不是必须，但上游就是 wasm**：引擎要能跑 ES2020+（async/await、generator、async generator、Proxy、`Symbol.iterator`、`Reflect.ownKeys`、`Object.getOwnPropertyDescriptors`、BigInt） | prelude 全量使用这些特性（`prelude-source.ts:58-139` 的 lockdown 递归冻结、`:230-247` 的 Proxy、`:103-115` 各种 prototype 探测） |

补充两条"宿主侧"约束（与引擎无关，但直接决定 Python 实现形态）：

- **不得在宿主回调里重入 VM**：`vm.ts:6-11` 明确契约（`post` 里不能调 `deliver`/`dispose`）。上游用 worker 线程 + `queueMicrotask` 保证（`inline.ts:88`）。
- **输出与 store 都要限额**：`MAX_OUTPUT_CHARS = 16 Mi`、`MAX_OUTPUT_ITEMS = 100000`（`prelude-source.ts:42-43`）、`MAX_STORE_VALUE_CHARS = 256 Ki`、`MAX_STORE_TOTAL_CHARS = 1 Mi`（`prelude-source.ts:34-35`）。

### 1.4 超时/中断到底是怎么实现的（三种机制，必须分清）

1. **等待期时间（tool 在跑、脚本在 await）**：host 的 `setTimeout` 到点 → `finish({kind:"timeout"})`（`execution.ts:214-218`），或调用方的 `AbortSignal`（`execution.ts:220-226,305-308`）。这一段与引擎能力无关。
2. **同步计算期（`while(true){}`）**：只有引擎的 interrupt 轮询能救。host **先把共享标志置 1**，VM 在下一个 poll 点抛不可捕获错误；`worker.terminate()` 是第二道（`host.ts:59-66`）。上游自己在 `InlineCodemodeSandbox` 里退化为**中断预算**：数 poll 次数，超预算按 `timeout` 报错（`inline.ts:39-42,77-82`，默认 `DEFAULT_INTERRUPT_BUDGET = 100_000`，实测约 2s（Apple M）到 10s（Cloudflare），`README.md:216-218`）。
3. **不轮询的宿主/内建函数**：上游 1.3.0 明确承认 `terminate()` 在 Bun 上**停不住**正在跑内建函数（如长 `String.prototype.indexOf`）的线程，因此停机**不等退出**（`CHANGELOG.md:14-16`、`README.md:62`）。

> **选型含义**：只要引擎提供"宿主回调式的中断轮询"，第 2 类就能做到与上游等价；再加一层"wasm trap / 进程 SIGKILL"就能补上第 3 类。反过来，**没有中断轮询的引擎（或只有 CPU 时间限制、且与宿主回调互斥的绑定）直接出局**。

---

## 2. API 面

### 2.1 导出符号（1.3.0 现状，`src/index.ts:1-65` 全量）

- 声明层（`./declarations` 入口，`package.json:14`）：`renderDeclarations`、`renderToolOutputType`、`renderToolSample`、`renderToolSignature`、`schemaToType`、`DEFAULT_INPUT_SCHEMA_MAX_CHARS`。
- 源码格式（`./source` 入口）：`parseCodemodeSource`、`CODEMODE_SOURCE_GRAMMAR`、`CODEMODE_OPTIONS_PREFIX`、`CodemodeSourceError`、类型 `CodemodeSourceOptions` / `ParsedCodemodeSource`。
- 沙箱：`CodemodeSandboxBase`（`runtime/execution.ts`）、`CodemodeSandbox`（`runtime/host.ts`）、`InlineCodemodeSandbox` + `DEFAULT_INTERRUPT_BUDGET`（`runtime/inline.ts`）、`RemoteCodemodeSandbox` + `serveCodemodeRemote`（`runtime/remote.ts`，`/portable`）。
- 工具函数：`toCodemodeIdentifier`、`loadQuickJSWasm`、类型 `CodemodeWasmModule`。
- 常量：`MAX_OUTPUT_CHARS`、`MAX_OUTPUT_ITEMS`、`MAX_STORE_TOTAL_CHARS`、`MAX_STORE_VALUE_CHARS`。
- 类型：`CodemodeTool`、`CodemodeToolContext`、`CodemodeResult`、`CodemodeOutcome`、`CodemodeRun`、`CodemodeRunOptions`、`CodemodeHostCall`、`CodemodeJsonSchema`、`CodemodeExecuteOptions`、`CodemodeSandboxOptions`、`CodemodeSandboxBaseOptions` …

**注意**：`MCP_TYPESCRIPT_PREAMBLE` 与 `mcpStructuredContentSchema()` 在 **1.2.0 已移除**（`CHANGELOG.md:22`）；1.3.0 的 `index.ts` 里确实没有它们。镜像时不要照 1.1 或更早的记忆去补这两个符号。

另外三个次要入口：`pi-codemode/portable`（无 Node import）、`pi-codemode/cloudflare`（Durable Object 端）、`pi-codemode/worker`（worker 入口，coding-agent 单独打包它，见 `packages/coding-agent/src/extensions/codemode/worker.ts:6`）。

### 2.2 数据契约（引擎无关，必须 1:1 复刻）

- 结果（`types.ts:94-109`）：`ok:true` → `{value, output, calls, storeWrites, during?}`；`ok:false` → `{error, output, calls, during?}`。
- 错误种类（`types.ts:59-73`、`README.md:347-355`）：`script` / `timeout` / `aborted` / `sandbox`。**`execute()` 从不因脚本失败而 reject**（`execution.ts:497-498`）。
- 输出项（`types.ts:47-49`）：`{type:"text", text, console?}` 或 `{type:"image", data, mimeType}`，`image()` 会做 base64 合法性 + 签名校验（PNG/JPEG/GIF/WebP，`prelude-source.ts:396-429`）并**拒绝远程 URL**。
- 调用记录（`types.ts:51-57`）：`{name, status:"ok"|"error"|"cancelled", durationMs}`；脚本返回时仍在跑的工具调用会被 abort 并记为 `cancelled`（`README.md:356`）。
- store（`types.ts:82-87`）：`{set, delete}`；失败的执行不报写入（`README.md:82`）。
- 工具定义（`types.ts:14-41`）：`name/description/inputSchema/outputSchema/spread/signature/execute(args,{signal})`；**schema 只用于渲染声明，不校验取值**（`types.ts:11`、`README.md:264`）。

### 2.3 消费方怎么调（决定 pi-py 必须实现哪些函数）

- **coding-agent**（`packages/coding-agent/package.json:45` 依赖 `1.3.0`；`execute.ts`）：
  - `parseCodemodeSource(input.code)` 取 `// @options:` 行（`execute.ts:449`）；
  - `new CodemodeSandbox({ tools, globals, timeoutMs: sourceOptions.timeoutMs ?? Infinity, memoryLimitBytes: 256MiB, wasm: loadQuickJSWasm(...), workerUrl: ..., acquireWorker })`（`execute.ts:502-524`，内存常量见 `execute.ts:56`）；
  - 用 `renderToolOutputType` / `renderToolSample` / `toCodemodeIdentifier` / `CODEMODE_SOURCE_GRAMMAR` 生成模型可见的工具描述（`tool.ts:26-28`）。
- **durable**：`portable` 的 sandbox + `parseCodemodeSource` + `sandbox.start(code, { startedAt, seed, tools, onCall, onIdle, onStarted, signal })`（`codemode.ts:460`，`describe.ts` 里还有 `DEFAULT_CODEMODE_INLINE_BUDGET`）。

→ pi-py 要镜像的最小面：`parseCodemodeSource` + `renderDeclarations` 全家 + `CodemodeSandboxBase.execute/start/close` + 一个具体 sandbox + `toCodemodeIdentifier` + 常量。

---

## 3. Python 侧引擎选型（核心，全部联网核实）

### 3.0 判据

| 判据 | 权重 | 说明 |
| --- | --- | --- |
| **C1 能否中断正在执行的同步计算** | **决定性** | 见 §1.4 第 2/3 类。必须能打断 `while(true){}` 与 `while(true) await null` |
| C2 中断能否与「JS→Python 工具回调」共存 | 决定性 | codemode 的整个设计就是回调；若绑定不允许"有限时/可中断 + 回调"同时存在，则进程内方案直接出局 |
| C3 引擎语义是否与上游一致 | 高 | 错误文本、栈帧格式（`at f (codemode.js:2:31)`）、`Intrinsics`、内存/栈语义 |
| C4 多实例隔离 | 高 | 每次 execute 一个全新 VM，独立堆 |
| C5 内存/栈上限 | 中 | R4/R5 |
| C6 确定性重放（虚拟时钟/种子/UTC）可行度 | 中 | `start()` 是 durable 的刚需 |
| C7 维护活跃度 / 许可 / Python 3.11+（本机 3.13）兼容 | 中 | 纯调研要求逐项给出 |

### 3.1 路线 A：`wasmtime`（Python）+ 上游同一份 `quickjs.wasm` —— **推荐**

#### A-1 这个 `.wasm` 是什么（二进制级核实，2026-10-18）

- 来源：npm `quickjs-wasi@3.6.2`（`https://registry.npmjs.org/quickjs-wasi`），仓库 **vercel-labs/quickjs-wasi**，描述 "Snapshotable JavaScript runtime via WebAssembly. **QuickJS-NG** compiled to WASM with snapshot/restore support."，**MIT**，**零 npm 依赖**，`exports` 直接暴露 `./quickjs.wasm`。发布时间线：3.2.0 (2026-07-23) → 3.6.2 (**2026-09-19**)，约月更，共 24 个版本。
- 上游 `packages/codemode/package.json:70` 锁的就是 `"quickjs-wasi": "3.6.2"`（同版本）。
- 我下载并解析了该 tarball（`quickjs-wasi-3.6.2.tgz`，sha256 `f1f4349f19a2d849e33ea0ae9bec2e7062b8839f4eceb17c9051ddbaa2720982`）：
  - `quickjs.wasm` = **637,405 字节**，sha256 **`d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9`**，wasm 版本 1，导出 `memory` / `__stack_pointer` / `__indirect_function_table`。
  - **导入只有 12 个**：

    | 模块.名字 | 签名 | 用途 |
    | --- | --- | --- |
    | `env.host_call` | `(i32,i32,i32,i32,i32)->i32` | 宿主回调蹦床（`newFunction` 用它回到宿主） |
    | **`env.host_interrupt`** | **`()->i32`** | **中断轮询：返回 1 即中断** |
    | `env.host_get_timezone_offset` | `(i32,i32)->i32` | `Date` 时区 |
    | `env.host_promise_rejection` | `(i32,i32,i32)->void` | 未处理 rejection |
    | `env.host_module_normalize` / `env.host_module_load` | `(i32,i32)->i32` | ES 模块（codemode 不用） |
    | `wasi_snapshot_preview1.{clock_time_get, fd_close, fd_fdstat_get, fd_seek, fd_write, random_get}` | 标准 preview1 | rust 侧可全部由宿主提供 |

  - **447 个函数导出**，其中 102 个 `qjs_*`。codemode 真正需要的（签名已解析确认）：
    `qjs_eval(i32,i32,i32,i32)->i32`、`qjs_call(i32,i32,i32,i32)->i32`、`qjs_new_host_function(i32,i32,i32)->i32`、`qjs_get_global()->i32`、`qjs_new_string(i32,i32)->i32`、`qjs_get_string(i32)->i32`、`qjs_get_string_len(i32,i32)->i32`、`qjs_free_cstring(i32)->void`、`qjs_get_prop_string(i32,i32)->i32`、`qjs_set_prop_value(i32,i32,i32)->i32`、`qjs_is_exception(i32)->i32`、`qjs_get_exception()->i32`、`qjs_typeof(i32)->i32`、`qjs_is_undefined(i32)->i32`、`qjs_new_number(f64)->i32`、`qjs_get_undefined()->i32`、`qjs_dup_value(i32)->i32`、`qjs_free_value(i32)->void`、`qjs_throw(i32)->i32`、**`qjs_execute_pending_job()->i32`**、**`qjs_is_job_pending()->i32`**、**`qjs_set_interrupt_handler(i32)->void`**、`qjs_set_memory_limit(i32)->void`、`qjs_set_max_stack_size(i32)->void`、`wasm_malloc(i32)->i32`、`wasm_free(i32)->void`。
  - 宿主回调约定（读 npm 包内 `dist/index.js:674-725`）：`host_call(namePtr, nameLen, thisPtr, argc, argvPtr)->i32`，`argvPtr` 指向 **argc 个 u32 handle**；返回值需 `qjs_dup_value(resultPtr)`；抛异常用 `qjs_throw` 后返回 0；`thisPtr`/argv 由 C 蹦床释放（借用手柄）。
  - 中断开关：`qjs_set_interrupt_handler(1)` 启用（`dist/index.js:510`），实际回调就是 `env.host_interrupt`（`dist/index.js:574`）。
  - 字符串用 **WTF-8** 写入 guest（`dist/index.js:728-736`；Python 的 `utf-8` + `surrogatepass` 正好产生同样的 3 字节代理序列）。
  - 同包还有 5 个 `.so` 扩展（url/crypto/encoding/headers/structured-clone）——**codemode 不需要**，`QuickJS.create({wasm})` 不加载它们。
- **对照上游 `vm.ts`**：`QuickJS.create({wasm, memoryLimit, maxStackSize, interruptHandler, wasi, timezoneOffset, intrinsics})`（`vm.ts:170-179`）、内部 `_getExports()`（`vm.ts:240`）、`Intrinsics.ALL & ~Intrinsics.WEAK_REF`（`vm.ts:178`）都能在 wasm 层找到对应物；唯一没有直接导出的是 `Intrinsics` 开关（wasm 只导出 `JS_AddIntrinsic*` 族），确定性模式需要退化为"在 prelude 前删掉 `WeakRef`/`FinalizationRegistry`"（见 §6 未确定项）。

#### A-2 Python 宿主侧能力（wasmtime，已核实 API 存在）

- 包：`wasmtime` @ PyPI，**最新 49.0.0（2026-09-21）**，`requires_python >=3.9`，classifiers 覆盖 3.9–**3.14**（含 3.13/3.14），**Apache-2.0 WITH LLVM-exception**，wheels：macosx_x86_64 / macosx_11_0_arm64 / manylinux1_x86_64 / …（另见 android）。仓库 `bytecodealliance/wasmtime-py`，按月发大版本（47.0.1 2026-07-20 → 48.0.0 2026-08-20 → 49.0.0 2026-09-21）。
- 中断能力（源码核实，`wasmtime/_config.py`、`_engine.py`、`_store.py`）：
  - `Config.epoch_interruption(enabled)`（`_config.py:344`）+ `Engine.increment_epoch()`（`_engine.py:21`）+ **`Store.set_epoch_deadline(ticks_after_current)`（`_store.py:109`）** → **wasm 代码会被 trap**，即使它根本不轮询宿主回调。这正好补上 §1.4 第 3 类，等价于（且强于）上游的 `worker.terminate()`。
  - 备选/叠加：`Config.consume_fuel(...)`（`_config.py:357`）+ `Store.set_fuel(int)` / `get_fuel()`（`_store.py:61,77`）→ 可做**确定性**的指令预算，比上游"数 poll 次数"更稳定（代价是执行变慢，需实测）。
- 宿主函数与表：`Func(store, FuncType, callback, access_caller=False)`（`_func.py:20`）、`Table.get/set`（`_table.py:62,85`）、`Memory` 读写——`env.host_interrupt` 这种"零参返回 i32"的回调是最简单的一种宿主函数。
- 隔离：每个 VM = 一个独立 wasm 实例 + 独立线性内存（`quickjs-wasi` 的设计，README "two-level model" 对比段落）。

#### A-3 进程模型建议

上游用 worker thread；Python 侧推荐**每次 `execute()` 一个子进程**（`multiprocessing` spawn 或 `fork`+exec 均可）：

- 与上游 `CodemodeSandbox` 的"一次执行一个 worker、跑完就杀"同形（`host.ts:12-19`）；
- 宿主（父进程）拥有 deadline / abort / 工具表 / 输出与调用记录 —— 也正是上游 `execution.ts` 的分工（`execution.ts:1-23`）；
- 中止 = ① `host_interrupt` 返回 1（VM 内）→ ② epoch 递增/trap（wasm 层）→ ③ SIGKILL（进程层）；三级兜底，**比上游的保证更强**（上游明确承认 Bun 上可能停不住，`README.md:62`）；
- 工具调用通过管道 JSON 往返（与上游 `protocol.ts` 同构）；父进程 asyncio 不被阻塞。

代价：每次执行多 ~20–60ms 进程启动（macOS spawn），上游 worker thread 约 20ms（`README.md:362`）。

#### A-4 工作量估计（这是本路线唯一的大成本）

| 部件 | 上游规模 | Python 侧估计 |
| --- | --- | --- |
| **wasm 宿主（新增）** | 无 | **600–1200 行**（instantiate + 6 个 env 宿主 + 6 个 WASI + ~26 个 qjs_* 封装 + handle 生命周期 + WTF-8 + 异常映射 + epoch/中断） |
| `runtime/vm.ts` | 341 行 | ~350 行（drain 循环、seq 排序、指纹、虚拟时钟几乎直译） |
| `runtime/execution.ts` | 573 行 | ~600 行（纯逻辑，无引擎依赖，可直译） |
| `runtime/prelude-source.ts` | 522 行 | **0 行改动**（原文照搬，作为包内字符串资源） |
| `declarations.ts` + `source.ts` | 340 + 125 行 | ~450 行 |
| `types.ts` / `identifier.ts` / 常量 | 254 + 12 行 | dataclass + 常量 |
| 子进程 launcher + 父子协议 | 88 + 39 + 93 行 | ~300 行 |
| `RemoteCodemodeSandbox` / cloudflare | 277 + 75 行 | **不镜像**（pi-py 无此场景） |
| 等价性测试 | `test/sandbox.test.ts` 869 + `deterministic.test.ts` 410 行 | 直接移植关键断言作为验收 |

合计新增/移植约 **2500–3500 行 Python**，其中真正"新"的只有 wasm 宿主那 600–1200 行。

#### A-5 已知坑

1. ABI 是 vercel-labs 的**私有 C ABI**（不是标准 WASI 接口），3.x 内看起来稳定，但**没有兼容性承诺**。缓解：vendor 死这一份 wasm（637KB 进包数据）+ sha256 校验 + 8 条冒烟断言进 CI。
2. `dist/index.js` 有 2786 行 TypeScript 粘合层；**不要整体移植**，只按 §A-1 那 ~26 个导出写最小宿主。
3. vercel 的 README 与 pi 的 README 对 wasm 上栈限制是否生效**说法矛盾**（见 §6-1）。
4. `env.host_interrupt` 是"每次字节码轮询"，回调必须极快（上游也是这个约束，`vm.ts:12-16`）；Python 回调有开销，实测前不要假设能达到上游的 5 万次/秒轮询率（`inline.ts:16-19`）。
5. 再入约束：Python 宿主函数里不能直接回调 VM（`vm.ts:6-11`）。子进程 + 微任务/消息队列的写法天然满足。

### 3.2 路线 B：`mini-racer`（PyMiniRacer，V8）—— 最低成本的**备选**

- 现状：PyPI **`mini-racer` 0.14.1（2026-02-01）**，仓库 `bpcreech/PyMiniRacer`（2024 年从 sqreen 手上接管的官方新家，**ISC**），共 17 个 release；文档站 `bpcreech.com/PyMiniRacer`。注意：**旧包名 `py-mini-racer` 停在 0.6.0（2021-04-22）**，别装错。
- **中断（硬需求）——满足，且有官方示例直接跑死循环**：
  - `ctx.eval('while (true) {}', timeout_sec=2)` → `JSTimeoutException: JavaScript was terminated by timeout`（文档首页示例）；
  - 函数对象也支持：`func(timeout_sec=2)`；
  - asyncio 场景：`await asyncio.wait_for(ctx.eval_cancelable('while (true) {}'), timeout=2)`，或 `func.cancelable()` 后用 `asyncio.wait_for`（文档明确"在事件循环里不要用 `timeout_sec`，要用 `cancelable`"）；
  - 实现层有 `_context.py` 的 `_run_cancelable_mr_task` / `mr_cancel_task`（`_context.py:488-525`）。
- **工具回调**：`ctx.wrap_py_function(async_fn)`（**只支持 async 回调**），示例 `async with ctx.wrap_py_function(read_file) as jsfunc: ctx.eval('this')['read_file'] = jsfunc`。文档自己警告："adding Python callbacks may degrade the security properties of PyMiniRacer"。
- 内存：`set_hard_memory_limit`（"JavaScript execution will be terminated when this limit is reached"，`_mini_racer.py:282-291`）、`set_soft_memory_limit`、`was_hard_memory_limit_reached`。
- Python 兼容：官方声明 **3.10–3.14**；wheels `py3-none`：macOS 10.9 x86_64 / 11.0 arm64、manylinux_2_27 x86_64+aarch64、musllinux、win_amd64 / win_arm64 → pi-py 的 3.13 有轮子。
- 坑：
  1. **进程内 V8**：隔离是 isolate 级，不是进程/线性内存级；V8 崩溃或硬 OOM 会带走整个 pi-py 进程。要硬隔离仍需子进程。
  2. **语义漂移**：错误文本/栈格式、`Date`/`Intl`、`Intrinsics`、`memoryLimit` 语义都与 QuickJS-NG 不同 → 上游 869 行 `sandbox.test.ts` 里涉及错误文本/行号的断言必须改写。
  3. **确定性 `start()` 没有引擎级支撑**：虚拟时钟、种子随机、UTC、结果序都得在 JS 层打补丁（且 `Date` 的构造路径不好完全虚拟化）→ durable 的重放保真度是最大损失。
  4. 它自带 asyncio 事件循环管理（`MiniRacer()` 会自带线程/循环），与 pi-py 的循环模型要和 `mini_racer(loop)` 对齐。
  5. `py3-none` 轮子意味着它自带预编译 V8 —— 包体较大（~5MB 级）。

### 3.3 路线 C：`quickjs-ng`（PyPI，genotrance）—— **进程内不可行（源码级证据）**

- 现状：PyPI **`quickjs-ng` 0.17.0.1（2026-10-01）**，仓库 `genotrance/quickjs-ng`，自述"**Drop-in replacement for the archived quickjs package** — `import quickjs` works unchanged"；abi3 wheels（cp310+，可跑 3.13/3.14）；平台：**macOS 只有 arm64**、manylinux/musllinux（i686/x86_64/aarch64）、win32/win_amd64、PyPy 3.10/3.11。它还有个旧名 `python-quickjs-ng`（0.15.1.1）。底座是 **quickjs-ng**（与上游 wasm 同引擎家族）。
- 中断能力（读 `module.c`）：
  - 唯一入口是 `set_time_limit(seconds)`，内部 `JS_SetInterruptHandler(runtime, js_interrupt_handler, &interrupt_data)`（`module.c:61-78`），handler 比较 `clock()` CPU 时间；
  - **`add_callable`（JS→Python 回调）与 time limit 互斥**：`module.c:441-443` 直接 `JS_ThrowInternalError(ctx, "Can not call into Python with a time limit set.")`；
  - 中断处理器用的 `limit`/`start` 在**进入 JS 时被拷贝**（`module.c:71-78`），运行中从别的线程改 `set_time_limit` 不会影响正在跑的 handler → **外部 abort 不成立**；GIL 在执行期间是释放的（`module.c:89-95`），但没有任何线程安全的取消入口；
  - `execute_pending_job()`（`module.c:557-572`）可以单步驱动 job 队列（R3 有解），但依然没有 R1 的中断源。
- 第三方实测佐证（同源实现，PetterS 版）：`JSException: InternalError: Can not call into Python with a time limit set.`；"Calling `ctx.set_time_limit(0)` from the main thread while the QJS worker thread is in an infinite `eval` does **not** stop the eval"；"there is no reliable in-process way to terminate a thread running pure-JS QuickJS. For hard isolation you need a separate process."（Simon Willison 调研笔记，见 §7 证据索引）。
- **结论**：`quickjs-ng` 只能在"**子进程 + SIGKILL**"形态下用（此时工具回调要跨进程桥接，等于把路线 A 的子进程模型照搬，但少了 wasm 层的引擎一致性与 epoch 强制中断）。作为**独立候选**它的净收益低于路线 A；作为参考实现很有价值（它的 `docs/threading.md`、`docs/c-extension.md` 对写宿主有帮助）。

### 3.4 路线 D：`dukpy` 0.6.0 —— 引擎对了，中断源不对

- 现状：PyPI **`dukpy` 0.6.0（2026-07-23）**，MIT，wheels 覆盖 cp39–cp314（含 free-threaded **cp314t**）、macOS x86_64+arm64、manylinux/musllinux、win32/win_amd64 —— 维护活跃度是这几个里最好的之一。
- **重要更正**：dukpy 已**不再是 Duktape**。`CHANGES.md`："JavaScript engine **migrated from Duktape to QuickJS-NG v0.11.0**, with modern JavaScript syntax and native Promise/job-queue support."；`src/dukpyjs.c` 里 `#include "quickjs.h"`，README 也写明"名字只是包兼容性"。（如果按记忆断言"dukpy 是 Duktape、没有 async/await"，就错了。）
- 中断实现（源码）：
  - `JS_SetInterruptHandler(ctx->runtime, dukpy_interrupt_handler, NULL)`（`dukpyjs.c:67`），handler 返回 `dukpy_should_interrupt()`（`dukpyjs.c:313-317`）；
  - **`dukpy_should_interrupt()` 的实现就是 `return PyErr_CheckSignals() != 0;`（`dukpyjs.c:21-22`）**，promise job drain 处同样检查（`dukpyjs.c:426-431`，中断后把 context 标记 `unusable`）；
  - 也就是说：**只有 Python 信号能触发中断**，没有 `timeout`/`abort` API，也没有预算计数。
- 可行性：若接受"主线程执行 + `signal.setitimer(ITIMER_REAL)` / SIGALRM"的写法，可以做出 wall-clock deadline；但（a）Windows 上不可用（只有 SIGINT/SIGBREAK），（b）并发多个 sandbox 时进程级信号会互相干扰，（c）JS 若不在主线程则信号语义不成立。对 pi-py 这种"agent 内可能并发多个 codemode 执行"的场景**不成立**。
- 另外它的设计目标（转译器/CLI/文件运行器，`dukpy.run`）与 codemode 的"工具注入 + 每执行一个 VM"差异较大。

### 3.5 路线 E：`quickjs`（PetterS）1.19.4 —— 排除

- PyPI `quickjs` **最新 1.19.4（2023-11-19）**，描述里自称"QuickJS is currently thread-hostile, so this wrapper makes sure that all calls to the same JS runtime comes from the same thread"，仓库已 archived（quickjs-ng 自称其 drop-in replacement）。
- wheel 只到 **cp312**（`cp38/39/310/311/312`），**没有 cp313/cp314** → 在 pi-py 的 3.13.3 上要从 sdist 编译（`requires_python` 为空，未声明支持）。
- 中断：只有 `set_time_limit`，与回调互斥（同 §3.3 的 `Can not call into Python with a time limit set.`）；外部线程改 limit 无效（实测）。
- **排除**。

### 3.6 其他候选（简要）

- `pythonmonkey`：PyPI 最新 **1.4.1（2026-10-16）**，`requires_python >=3.8`，cp310 wheels（macOS arm64/x86_64、manylinux aarch64…）。SpiderMonkey 系；**本次未核实其中断/超时能力**（列在 §6 未确定项），不作为候选。
- `py-mini-racer`（旧包）：0.6.0，2021-04-22，**末次发布 5 年前**，只到 py3 通用轮子（cp 无关），无 cancelable/timeout 的新语义 → 用新包 `mini-racer`。
- `js2py` / `PyV8` / `v8py`：纯 Python 解释器或早已停更，不支持 async/await 或无法中断 → 不评估。

### 3.7 路线 G：子进程直接跑 Node/Bun + 上游 dist —— 保真兜底

- 做法：把上游 `packages/codemode` 的构建产物（或 `quickjs-wasi` npm 包 + 一个 worker 入口文件）放进 pi-py，通过 `subprocess` 起 `node >= 22.19.0`（`package.json:67` 的 engines 要求）跑 `CodemodeSandbox`，Python 侧只做工具桥与结果解析。
- 保真度：**100%**（就是上游代码）。
- 代价：把 Node.js 变成 pi-py 的运行时依赖（与"纯 Python 包"冲突）；跨进程工具桥还得自己写（不比路线 A 少）；Node 缺失时要有降级策略。

### 3.8 汇总对比

| 候选 | 维护活跃度（截至 2026-10-18） | **能否中断正在执行的脚本** | 中断与工具回调能否共存 | 多隔离实例 | Python 3.11+/3.13 | 许可证 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **A. wasmtime + `quickjs.wasm`(3.6.2)** | wasmtime 49.0.0 (2026-09-21，月更)；wasm 3.6.2 (2026-09-19，月更) | **能，两级**：VM 内 `host_interrupt` 轮询 + wasmtime epoch trap | **能**（宿主自己实现，无互斥限制） | wasm 实例 + 子进程，最强 | ✅ 3.9–3.14 | Apache-2.0 WITH LLVM-exception + MIT | **推荐** |
| B. `mini-racer` 0.14.1 | 2026-02-01 发布，2024 起复兴 | **能**：`timeout_sec` / `eval_cancelable`（官方示例即 `while(true){}`） | 能（回调仅 async） | V8 isolate（进程内共享地址空间） | ✅ 3.10–3.14 | ISC | **备选** |
| C. `quickjs-ng` 0.17.0.1 | 2026-10-01，活跃 | **进程内不能**（time limit 与回调互斥；外部改 limit 无效）；子进程 SIGKILL 才行 | ❌（进程内） | Context 隔离（进程内） | ✅ abi3（macOS 仅 arm64） | MIT（PyPI 字段为空） | 仅作参考 |
| D. `dukpy` 0.6.0 | 2026-07-23，最活跃 | 仅靠 **Python 信号**（`PyErr_CheckSignals`） | 能（不互斥） | Context/进程 | ✅ cp39–cp314 | MIT | 不作为主选 |
| E. `quickjs` 1.19.4 | **2023-11-19，仓库 archived** | 同上互斥；外部线程改 limit 无效 | ❌ | Context | ❌ 无 cp313 轮子 | MIT | 排除 |
| G. 子进程 Node + 上游 dist | 跟上游 | 能（同上游，含其 Bun 局限） | 能 | 进程 | 取决于 Node | MIT | 保真兜底 |

---

## 4. 可行性结论（可执行）

### 4.1 结论

**能。** Python 侧可以实现与上游 v1.3.0 行为等价的 codemode，**包括硬需求"中断正在执行的脚本"**。

但"能"是**有条件的**：

- **条件 1（技术）**：必须自建一层宿主（Python ↔ `quickjs.wasm` 的 C ABI），上游没有这段代码。
- **条件 2（选型）**：必须选 §3.1 的路线 A（或接受 §3.2 路线 B 的语义漂移）。**不能用** `quickjs`(PyPI) / `quickjs-ng`(PyPI) 在进程内"边跑工具回调边中断"，这条路已被源码级封死（§3.3）。
- **条件 3（验证）**：先做 §4.3 的 Phase-0 冒烟；8 条断言不全过就不进入正式移植。

### 4.2 推荐路线与理由

**推荐：路线 A —— wasmtime(Python) + vendor 上游同一份 `quickjs.wasm`（quickjs-wasi 3.6.2），每次 `execute()` 一个子进程。**

理由（按重要性）：

1. **引擎同一性**：跑的就是上游那份 wasm 二进制（sha256 已固定），引擎=QuickJS-NG，prelude 可以**零改动**复用（`prelude-source.ts` 522 行原文），错误文本/栈帧格式、`InternalError: out of memory`、深递归 RangeError 等 869 行行为测试的断言不必改写 —— 这正是"忠实镜像"的核心诉求。
2. **中断能力不但够，而且更强**：`env.host_interrupt` 复刻上游 `shouldInterrupt`（含 `JOBS_PER_POLL=50` 的 job 分批，R2/R3 完整），wasmtime epoch trap 补上"不轮询的内建函数"，SIGKILL 补上"进程级失控"。上游在 Bun 上只能"请求终止、不等退出"（`README.md:62`），我们三级兜底。
3. **确定性重放可做**：WASI 的 `clock_time_get` / `random_get` 由我们自己提供 → 虚拟时钟与种子随机是**原生**能力（不是 JS 打补丁）；`host_get_timezone_offset` 返回 0 即 UTC。这比 V8 路线好得多。
4. **隔离与限额齐备**：独立 wasm 实例 + 独立线性内存；`qjs_set_memory_limit` / `qjs_set_max_stack_size` 直接对应 R4/R5。
5. **无运行时依赖**：纯 Python + 一个 637KB 的 MIT wasm 文件，不需要 Node/npm。

### 4.3 Phase-0 冒烟验证（1–2 天，可执行，带决策门限）

在 `/tmp` 下写一个不依赖仓库的 spike（允许临时 `pip install wasmtime` 到临时 venv；**本任务未安装任何依赖**），按顺序验证：

| # | 断言 | 通过标准 |
| --- | --- | --- |
| S1 | wasmtime 能实例化 `quickjs.wasm` | 提供 6 个 `env.host_*` + 自定义 `wasi_snapshot_preview1` 后 `Instance` 成功 |
| S2 | `qjs_eval("1+2", "<t>", 0)` | 返回 handle，`qjs_get_float64`=3 |
| S3 | `qjs_new_host_function` + `host_call` 蹦床 | JS 调 `py("abc", 1)` 能拿到 `nameLen/argc` 与 handle 数组，返回 `qjs_dup_value` 后 JS 读到值 |
| S4 | **上游 `PRELUDE_SOURCE` 原文直接 eval** | 零改动装载成功；`tools.echo({a:1})` 走通（证明 prelude 可复用） |
| S5 | **中断 `while(true){}`** | `env.host_interrupt` 在第 N 次轮询返回 1 后 ≤100ms 内，脚本收到不可捕获错误（对应 `shouldInterrupt` 语义） |
| S6 | **中断 `while(true) await null`** | 用 `qjs_is_job_pending`/`qjs_execute_pending_job` 单步 drain，每 50 个 job 轮询一次，能被中断（复刻上游 `vm.ts:240-250`） |
| S7 | `qjs_set_memory_limit` | 超限分配在脚本内抛 `InternalError: out of memory`，VM 可继续用 |
| S8 | wasmtime epoch trap | `Config.epoch_interruption(True)` + `increment_epoch()` + `set_epoch_deadline(1)` 能让一个不轮询的 wasm 循环 trap |

**决策门限**：S1–S6 全过 → 按路线 A 正式立项；**S4 或 S6 失败 → 直接切路线 B（mini-racer）**，并按 §4.5 接受漂移；S3 失败但 S5/S7 过 → 说明宿主回调约定理解有误，先修再判（不要在这一步切路线）。

### 4.4 工作量与里程碑（基于 §A-4）

- **M1（引擎宿主，2–3 周）**：wasm 宿主 + `vm.py`（移植 `vm.ts`），通过 S1–S8 + `test/sandbox.test.ts` 里与错误/输出/store 相关的断言。
- **M2（Host 语义，2 周）**：`execution.py`（deadline/abort/工具表/记录/输出/storeWrites）+ 子进程 launcher 与父子协议；通过 `test/worker-lifetime.test.ts`（229 行）对应的"请求停机 vs 真实退出"断言 —— **注意 1.3.0 的 Breaking**：结果在"请求停机"后即结算，`acquireWorker()` 风格的并发准入要保留（coding-agent 依赖它，`execute.ts:514`）。
- **M3（声明层与源码格式，1 周）**：`declarations.py` + `source.py`（含 `CODEMODE_SOURCE_GRAMMAR`）。这部分零引擎风险，**可以并行甚至先行**以降低整体风险。
- **M4（确定性重放，1–2 周）**：`start()` + `deliver(seq, now, id, outcome)` + 指纹（`vm.ts:71-81` 的 FNV-1a prelude hash + 引擎版本串）+ 虚拟时钟/种子；通过 `test/deterministic.test.ts`（410 行）的关键断言。

### 4.5 若路线 A 失败：降级到 B 的具体代价（"最大妥协"）

切到 V8（mini-racer）后必须**显式接受并文档化**四处不等价：

1. **错误与栈文本**：`script` 类错误的 `stack` 从 `at f (codemode.js:2:31)` 变成 V8 格式；上游测试里所有涉及错误文本/行号的断言必须改写。
2. **确定性重放（durable 的 `start()`）**：引擎不提供虚拟时钟/种子随机/UTC；只能 JS 层近似（`Date` 的构造路径难以完全虚拟化），**重放严格性下降**——这是最实质的损失。
3. **中断语义**：由 V8 `terminate_execution` 提供，一次性、会破坏 isolate 状态；上游"中断后 VM 仍可用"的语义（quickjs-wasi README 明确"VM is still usable after an interrupt"）不成立，必须每次执行新建 isolate。
4. **隔离与崩溃模式**：进程内 V8，硬 OOM/崩溃会带走 pi-py；要同等安全仍需子进程（于是省下的工时又被吃掉一部分）。

### 4.6 与上游版本变更点的对齐检查（防"照抄旧记忆"）

- [x] v1.2.0 移除 `MCP_TYPESCRIPT_PREAMBLE` / `mcpStructuredContentSchema()`：1.3.0 的 `index.ts` 里确实没有（`CHANGELOG.md:22`）→ 不要补。
- [x] v1.2.0 added：`RemoteCodemodeSandbox`（不镜像）、`InlineCodemodeSandbox` + `interruptBudget`、`CodemodeSandboxBase`、`start()` 确定性运行。
- [x] v1.2.0 fixed：`parseCodemodeSource()` 对非首行 `// @options:` 抛 `CodemodeSourceError`（`CHANGELOG.md:39`）。
- [x] **v1.3.0 Breaking（worker 生命周期）**：结果/`close()` 在"请求停机"后结算；新增 `onWorkerStarted(exited)` 与 `acquireWorker()`（`CHANGELOG.md:5-16`）→ Python 侧要保留这两个钩子（并发准入是 coding-agent 的真实依赖）。
- [x] v1.2.0 起 VM 在 drain promise job 时也轮询中断（`CHANGELOG.md:35`）→ S6 必须覆盖。
- [x] 输出上限 `MAX_OUTPUT_CHARS`/`MAX_OUTPUT_ITEMS`（1.0.1）与内置冻结（1.0.4）都已在 1.3.0 源码里。

---

## 5. 若不可行的替代路径（按保真度降序）

1. **子进程 Node/Bun 跑上游 dist（§3.7）**：保真 100%，代价是引入 Node ≥ 22.19 运行时依赖与 npm 包管理 —— 若 pi-py 允许"可选外部依赖"，这是**最省力**的高保真方案，甚至可以先落地它、把路线 A 作为长期替代（两者共用同一套 host 侧语义与测试）。
2. **只做声明层（不执行 JS）**：把 `renderDeclarations` / `renderToolOutputType` / `parseCodemodeSource` / `CODEMODE_SOURCE_GRAMMAR` 直译（约 450 行，零引擎风险），模型仍能看到 `codemode` 工具与工具的签名，但**不能执行**。明确损失：coding-agent 的 `codemode` 工具退化为"只是描述"；durable 的 `start()` 重放完全无法实现。
3. **只做工具直调（不做脚本聚合）**：模型改为逐个工具调用（等于放弃 codemode 的核心价值：嵌套调用不进上下文）。属于产品降级，不推荐。
4. **标 unsupported + 文档化差异**：在 pi-py 的 SYNC 文档里记一条"codemode 暂不镜像，原因见本报告"，把风险从实现转移到产品决策。

---

## 6. 无法确定项（显式清单，需在 Phase-0 或后续验证）

1. **wasm 上 `qjs_set_max_stack_size` 是否真的生效**：vercel 的 README 写"QuickJS-ng **disables** `JS_SetMaxStackSize` on WASI, so deep recursion causes a WASM trap (not a catchable exception)"，而上游 codemode README:372 写 Node/Bun 上"deep recursion 有可捕获的 RangeError"。两者矛盾，本次**未实测**（该 README 明显滞后：它写"7 个 imports"，实际 12 个）。影响 R5 的等价性。
2. **`Intrinsics` 开关**：`vm.ts:178` 用 `Intrinsics.ALL & ~Intrinsics.WEAK_REF`，但 wasm 只导出 `JS_AddIntrinsic*` 一族函数，没有直接等价物。确定性模式下"删除 `WeakRef`/`FinalizationRegistry`"是近似而非等价（引擎内部仍可能有引用路径）→ 需实测 `test/deterministic.test.ts` 的对应断言。
3. **wasmtime-py 自定义 `wasi_snapshot_preview1` 的可行性**：`Linker.define(store, "wasi_snapshot_preview1", "clock_time_get", Func(...))` 这类写法 API 上成立（`Func` 存在），但**是否会与 `define_wasi` / `WasiConfig` 冲突、6 个函数是否够用（reactor 模式）未实测**。
4. **`qjs_new_host_function(name, len, 0)` 第三个参数语义**：npm 包内三处调用都传 `0`（`dist/index.js:1048,1192,1218`），文档未说明；不影响使用但要确认。
5. **handle 生命周期细节**：`qjs_dup_value` / `qjs_free_value` 的引用规则、`thisPtr`/argv 借用窗口（`dist/index.js:700-706` 说是"C trampoline 拥有并在返回后释放"）在 Python 宿主里要严格照做；初值/空值（0 是否代表 NULL）需实测。
6. **quickjs-wasi 3.6.2 内嵌的 quickjs-ng 具体版本号**：只有运行时调 `qjs_get_quickjs_version()` 才能得到（npm README 未写）。
7. **性能数字**：子进程启动开销、Python 宿主 `host_call` 每次往返成本、`host_interrupt` 轮询速率（决定中断预算的粒度）——全部未实测；上游给出的参考是 worker ~20ms、轮询 ~5 万次/秒（M 系列）（`README.md:362`、`inline.ts:16-19`）。
8. **`pythonmonkey` 的中断/超时能力**：本次只核实了版本（1.4.1，2026-10-16）与轮子，未核实能力。
9. **`mini-racer` 的 hard memory limit 到底"终止执行"还是"V8 致命报错"**：文档写"JavaScript execution will be terminated"（`_mini_racer.py:282-291`），但 V8 硬堆限在部分配置下会 `abort()`；未实测（若走路线 B，这是必须验证的一条）。

---

## 7. 证据索引

### 7.1 上游源码（`/tmp/pi-up/v1.3.0/`，只读）

| 主题 | 位置 |
| --- | --- |
| 引擎依赖与 Node 要求 | `packages/codemode/package.json:67,70` |
| 三个 sandbox 与总览 | `packages/codemode/README.md:3,7-14,58,60,62,216-218,347-374` |
| 1.3.0 Breaking / 1.2.0 变更 | `packages/codemode/CHANGELOG.md:5-16,22,27-29,35,39` |
| 导出面 | `packages/codemode/src/index.ts:1-65` |
| worker 启动、共享中断标志、停机语义 | `src/runtime/host.ts:20-75`（尤其 `:28-29,:59-66,:68`） |
| worker 内 shouldInterrupt | `src/runtime/worker.ts:28` |
| VM 驱动：create/interruptHandler/drain/bridge/prelude/脚本 | `src/runtime/vm.ts:6-16,52-57,102-125,154,170-179,189-201,240-250,254-296,299-310,327,319` |
| host 侧执行、deadline、工具调用、finish | `src/runtime/execution.ts:1-23,49,214-226,229-257,260-298,378-408,410-448,497-517,530-566,569-572` |
| inline（中断预算） | `src/runtime/inline.ts:10-20,39-42,77-82` |
| 数据契约 | `src/types.ts:14-41,47-49,51-57,59-73,82-87,94-109,112-132,149-162` |
| prelude（可零改动复用） | `src/runtime/prelude-source.ts:34-43,48-139,197-211,230-247,304-319,342-355,396-429,451-456,459-521` |
| wasm 装载 | `src/wasm.ts:21-34` |
| 协议 | `src/runtime/protocol.ts:3-8,14-38,43-76` |
| 消费方（coding-agent） | `packages/coding-agent/src/extensions/codemode/execute.ts:56,449,502-524`；`worker.ts:6`；`tool.ts:26-28`；`packages/coding-agent/package.json:45` |
| 消费方（durable，`start()`） | `packages/durable/src/codemode/codemode.ts:10,211,365,460` |
| 等价性测试规模 | `test/sandbox.test.ts` 869 行、`test/deterministic.test.ts` 410 行、`test/worker-lifetime.test.ts` 229 行、`test/declarations.test.ts` 328 行、`test/source.test.ts` 78 行 |

### 7.2 外部来源（全部于 2026-10-18 查证）

| 内容 | 来源 |
| --- | --- |
| `quickjs-wasi@3.6.2`（vercel-labs，MIT，QuickJS-NG→WASM，导出 `quickjs.wasm`），发布时间 2026-09-19 | `https://registry.npmjs.org/quickjs-wasi`（元数据）；tarball `https://registry.npmjs.org/quickjs-wasi/-/quickjs-wasi-3.6.2.tgz`（sha256 `f1f4349f…0982`）；包内 `quickjs.wasm` sha256 `d4c9375f…bbd9`；`README.md`（"Built from quickjs-ng"、"WASI 上禁用 SetMaxStackSize"、"interruptHandler 返回 true 即中断"）；`dist/index.js:503-510,570-574,657-667,674-725,728-736,775-783,1048,1404` |
| wasmtime 49.0.0（2026-09-21，Apache-2.0 WITH LLVM-exception，py3.9–3.14） | `https://pypi.org/pypi/wasmtime/json` |
| wasmtime epoch/fuel 中断 API | `https://github.com/bytecodealliance/wasmtime-py` → `wasmtime/_config.py:344,357`、`_engine.py:21`、`_store.py:61,77,109`、`_func.py:20`、`_table.py:62,85`（raw 源码，2026-10-18） |
| `mini-racer` 0.14.1（2026-02-01，ISC，py3.10–3.14）与 `timeout_sec`/`eval_cancelable`/`wrap_py_function` | `https://pypi.org/pypi/mini-racer/json`；`https://bpcreech.com/PyMiniRacer/`（首页示例）；`https://bpcreech.com/PyMiniRacer/api/`；源码 `src/py_mini_racer/_mini_racer.py:110-116,172-183,282-303`、`_context.py:157-193,318,360-367,488-525` |
| `quickjs-ng` 0.17.0.1（2026-10-01，abi3 cp310+）与中断互斥 | `https://pypi.org/pypi/quickjs-ng/json`；`https://github.com/genotrance/quickjs-ng` → `module.c:61-78,89-95,441-443,557-572`、`README.md`、`quickjs/__init__.py` |
| `quickjs`（PetterS）1.19.4（2023-11-19，wheel 仅到 cp312） | `https://pypi.org/pypi/quickjs/json` |
| QuickJS(Python) 中断实测：time-limit ⊥ Python 回调、外部改 limit 不中断、进程内无法终止纯 JS 线程 | `https://raw.githubusercontent.com/simonw/research/main/quickjs-async-sandbox/notes.md`（2026-10-18 读取） |
| `dukpy` 0.6.0（2026-07-23，MIT，已迁移 QuickJS-NG v0.11.0；中断=PyErr_CheckSignals） | `https://pypi.org/pypi/dukpy/json`；`https://github.com/amol-/dukpy` → `CHANGES.md`、`src/dukpyjs.c:21-22,67,313-317,426-431`、`README.rst` |
| `pythonmonkey` 1.4.1（2026-10-16，仅记录版本/轮子） | `https://pypi.org/pypi/pythonmonkey/json` |

### 7.3 本次调研的方法与边界

- 上游源码：只读，未修改 `/tmp/pi-up/v1.3.0/` 任何文件。
- 外部库核实：联网读取 npm registry / PyPI JSON / 官方文档 / GitHub raw 源码；**未安装任何依赖**，未改 `pyproject.toml`，未触碰 `packages/`。
- wasm 二进制分析：下载 npm tarball 到 `/tmp/qjs-wasi*`，用自写的纯 Python WASM 段解析脚本（`/tmp/qjsprobe/`）读出 imports/exports/类型签名；命令输出见本报告 §3.1。
- 唯一写入的仓库文件：本报告 `recon/07-codemode-feasibility.md`。

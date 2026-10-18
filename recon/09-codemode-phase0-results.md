# 09 — codemode Phase-0 冒烟实测结果（S1–S8 + E1–E4）

- **任务**：task-14（决策点：codemode Phase-0 冒烟）。执行 `recon/07-codemode-feasibility.md` §4.3 设计的 S1–S8。
- **结论（一句话）**：**14/14 全过 → `pi-codemode` 按路线 A（wasmtime + 上游同一份 `quickjs.wasm`）立项**。S1–S6 门限全部满足，不需要转 `mini-racer`。
- **实测时间**：2026-10-18 15:21–15:45（Asia/Shanghai）。
- **环境**：macOS 27.2 / arm64；spike venv = `/tmp/codemode-spike/.venv`（**Python 3.14.5**）；`wasmtime==49.0.0`（pip 安装，仅装在 /tmp 的 venv）；`node v25.8.2`（仅用于提取 prelude 字符串）。
- **代码与原始日志**（均不入库）：`/tmp/codemode-spike/host.py`（326 行，最小宿主）、`/tmp/codemode-spike/spike.py`（619 行，14 项检查）、`/tmp/codemode-spike/run.log`（197 行，完整原始输出）、`/tmp/codemode-spike/abi-signatures.txt`（447 个导出签名）。

---

## 0. 决策门限判定

| 门限（来自 07 报告 §4.3） | 实测 | 判定 |
| --- | --- | --- |
| S1–S6 全过才立项 | S1,S2,S3,S4,S5,S5b,S6 **全 PASS** | ✅ 满足 |
| S4（prelude 零改动装载）失败 → 切 mini-racer | S4 **PASS**（prelude 原文装载 + 完整工具往返） | 不触发 |
| S6（`await` 死循环可中断）失败 → 切 mini-racer | S6 **PASS**（两种机制都能停） | 不触发 |
| 附加 S7/S8/E1–E4 | **全 PASS** | — |

**最终判定：立项（Go）。** 备选 `mini-racer` 不再需要激活（§2.5 保留降级说明以备将来）。

---

## 1. wasm 来源核验（先于一切）

```bash
curl -sSL -o quickjs-wasi-3.6.2.tgz https://registry.npmjs.org/quickjs-wasi/-/quickjs-wasi-3.6.2.tgz
tar xzf quickjs-wasi-3.6.2.tgz package/quickjs.wasm
shasum -a 256 quickjs.wasm
```
原始输出：
```
d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9  quickjs.wasm
ACTUAL  : d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9
EXPECTED: d4c9375f2b1ca4dc95f72c8aa2982a7a9951ac8011490d79c6582df732b4bbd9
MATCH   : True
```
- 文件大小 **637,405 字节**，与 07 报告记录一致。
- 上游锁版本核对：`grep -n "quickjs-wasi" /tmp/pi-up/v1.3.0/packages/codemode/package.json` → `70: "quickjs-wasi": "3.6.2"` ✅
- **额外收获**：S1 打印出 `qjs_get_quickjs_version()` = **`0.17.0`** —— 即该 wasm 内嵌 quickjs-ng **0.17.0**（07 报告 §6-6 的未确定项由此关闭）。巧合的是 PyPI `quickjs-ng` 0.17.0.1 绑定的是同一代引擎。

### prelude 的「零改动」如何被证明

不手写转义处理，而是用 **Node 真实求值** TS 模板字符串，只剥掉类型注解一行：

```bash
sed 's/^export const PRELUDE_SOURCE: string = /export const PRELUDE_SOURCE = /' \
  /tmp/pi-up/v1.3.0/packages/codemode/src/runtime/prelude-source.ts > prelude-src.mjs
node -e "import('/tmp/codemode-spike/prelude-src.mjs').then(m=>{require('fs').writeFileSync('prelude.js', m.PRELUDE_SOURCE)})"
diff <(sed 's/^export const PRELUDE_SOURCE: string = /export const PRELUDE_SOURCE = /' <原文件>) prelude-src.mjs
```
输出：`prelude bytes: 17233  lines: 475` / `diff clean (annotation strip only)`。
→ 进入 VM 的 **17,233 字符是上游模板字面量的求值结果，零人工改写**（sha256 前缀 `57c8dc8c…`，见 run.log:37）。

---

## 2. 逐条实测（原始输出引自 `/tmp/codemode-spike/run.log`）

复现命令：`cd /tmp/codemode-spike && .venv/bin/python spike.py`（编排器把每项放进独立子进程并带超时）。

### S1 实例化 + `qjs_init`（自带 WASI，不用 `define_wasi`）— **PASS**
断言：wasmtime 能用自定义 `wasi_snapshot_preview1`（6 个函数）+ `env.*`（6 个导入）实例化 `quickjs.wasm` 并初始化 runtime/context。
```
  wasm bytes           : 637405
  qjs_init() return    : 0
  quickjs version      : 0.17.0
  memory size (bytes)  : 1376256
  runtime ptr / ctx ptr: 1167440 / 1205392
  [PASS] qjs_init() == 0 (got 0)
  [PASS] linear memory exported and readable
```
→ 07 报告 §6-3「wasmtime-py 自定义 WASI 是否可行」**已证实可行**（全程未调用 `define_wasi`）。

### S2 `qjs_eval` 数值/字符串往返 — **PASS**
```
  eval('1+2') -> is_exception=False float64=3.0
  eval(string) -> 'codemode:42'
```

### S3 `qjs_new_host_function` + `host_call` 蹦床 — **PASS**
断言：JS 调宿主函数，宿主读到参数（字符串 + 数字），返回值回到 JS。
```
  host callback fired: name='hello' argc=2 second_arg=7.0
  JS (cb)=>cb('hello',7) returned via host: 42.0
  [PASS] host saw exactly [('hello', 7)]
```
→ 07 报告 §6-4「`qjs_new_host_function` 第三参语义」不再阻塞实现：`(namePtr, nameLen, 0)` 可用；argv 是 `argc` 个 u32 handle，`host_call` 返回值需 `qjs_dup_value`。

### S4 上游 `PRELUDE_SOURCE` 零改动 + 完整工具往返 — **PASS**（门限项）
断言：装载上游 prelude 原文；脚本按上游 `vm.ts:327` 的 `(async (tools, console) => {<body>\n})` 包装；`tools.echo()` 经 bridge 到宿主；`settle()` 回灌后 `text()` 与 `done` 正确。
```
  prelude: 17233 chars, 475 lines, sha256=57c8dc8c0e812117
  exec1 calls=[(1, 'echo', '{"a":1}')] output=[('text', 'got 42')] done=[(True, '"done1"', '[]')] jobs=2
  exec2 calls=[(1, 'echo', '{"b":2}')] output=[] done=[(True, '{"b":99}', '[]')] jobs=3
  exec3 calls=[] output=[('text', 'no tool call')] done=[(True, None, '[]')]
  [PASS] prelude api has settle/run/stalled (True, True, True)
  [PASS] tool call reached the host with JSON args -> [(1, 'echo', '{"a":1}')]
  [PASS] settle() -> text('got 42') -> [('text', 'got 42')]
  [PASS] done message with the return value -> [(True, '"done1"', '[]')]
  [PASS] exec2 pure return value round-trip -> [(True, '{"b":99}', '[]')]
```
覆盖到的上游语义：`tools.<jsName>` 代理、JSON 参数往返、`await`、`text()`、`return` 值、`done(ok, valueJson, writesJson)`、`store` 序列化占位（`'[]'`）。
**过程中发现的真实语义（对实现很重要）**：prelude 内的 `finished` 闩锁使**一个 VM 只能完成一个脚本**——第二次 `done()` 被静默忽略（`prelude-source.ts:152-156`）。这与上游「每次 `execute()` 新建 worker+VM」的设计完全吻合（`README.md:362`），移植时必须保持「一次执行一个 VM（或重载 prelude）」。

### S5 中断 `while(true){}`（共享内存标志 + `host_interrupt`）— **PASS**（门限项）
断言：另一个**进程**通过 `shared_memory` 置位，VM 内的 `env.host_interrupt` 读到后中断同步死循环。
```
  eval returned after 0.305s; is_exception=True; message='InternalError: interrupted | stack[0]=    at <eval> (<interrupt-test>:1:1)'
  host_interrupt polls  : 7,944
  VM reusable after interrupt: True (eval('1+2')=3.0)
  [PASS] stopped ~0.3s after the flag was set (took 0.305s)
  [PASS] eval raised an uncatchable error
  [PASS] VM still usable after the interrupt
```
- 中断是**不可捕获**的 `InternalError: interrupted`，栈格式 `at <eval> (<interrupt-test>:1:1)` —— 与上游 `at f (codemode.js:2:31)` 同形，**栈格式保真可达**。
- 中断后 VM **仍可继续用**（与 `quickjs-wasi` 文档一致）。
- 轮询率实测：`7,944 / 0.305s ≈ 26,000 polls/s`（Python 宿主）↔ 上游 JS 宿主约 50,000/s（`inline.ts:16-19`）。→ **`DEFAULT_INTERRUPT_BUDGET = 100_000` 在本机约等于 3.8 秒计算量**，不能照搬上游「约 2 秒」的说法，移植时要按平台重新标定。

### S5b 中断 `while(true){}`（**Python 线程**看门狗，上游 worker 模型）— **PASS**
```
  eval ran for 0.229s; flag->stop latency=0.2ms
  message='InternalError: interrupted | ...'; polls=5,832
  [PASS] thread watchdog stopped the loop within 200ms of the flag
```
→ **置位到中断的延迟 0.2ms**。这条与 E3 一起推翻了 07 报告里「必须靠子进程才能中断」的保守判断（见 §4.1）。

### S6 中断 `while(true) await null`（单步 job 队列）— **PASS**（门限项）
```
  async IIFE handle is_exception=False
  drain_jobs -> jobs=24,200 reason='host-side interrupt after 24200 jobs' elapsed=0.317s
  host_interrupt polls=10
  variant B (engine polling only): jobs=24,999 reason='job threw (InternalError: interrupted)' elapsed=0.323s polls=11
  [PASS] host-side check every 50 jobs stopped it
  [PASS] stopped within 3s (took 0.317s)
```
- 变体 A：复刻 `vm.ts:247`「每 50 个 job 检查一次」→ 0.317s 停止 ✅
- 变体 B：**完全不检查**，只靠引擎自身 opcode 轮询 → 也在 0.323s 抛出 `InternalError: interrupted` ✅
→ 07 报告引用的上游注释（`vm.ts:12-16`：job 之间 opcode 轮询「会让它跑很久」）在本配置下**不成立**；但保留 host 侧检查仍是正确的加固（成本可忽略）。

### S7 内存上限 — **PASS**
```
  catch-inside-JS result : 'caught:InternalError:out of memory'
  uncaught is_exception  : True message='InternalError: out of memory | stack[0]=    at <eval> (<eval>:1:4)'
  VM reusable after OOM  : True
```
→ 与上游 `README.md:60` 的描述逐字一致（可在脚本内捕获、之后 VM 仍然可用）。

### S8a wasm 级硬停：fuel — **PASS**
```
  fuel after instantiate+init: 18,058,803
  fuel before eval           : 200,000
  trapped='...Caused by:\n    wasm trap: all fuel consumed by WebAssembly\n' after 0.001s
```
- 结论：`Config.consume_fuel` + `Store.set_fuel` 能在 **1ms** 内硬停死循环（不依赖任何轮询）。
- **实现要点**：**实例化 + `qjs_init` 本身消耗约 1.94M fuel**（20,000,000 → 18,058,803）；预算必须把这段计入（或先给足、执行前再 `set_fuel`）。

### S8b wasm 级硬停：epoch — **PASS**
```
  (probe) default epoch deadline before instantiate:
    instantiate with default deadline: Trap: wasm trap: interrupt
  instantiated with deadline=1e9: ok=True
  trapped='...Caused by:\n    wasm trap: interrupt\n' after 0.000s
```
- **实现要点（踩坑记录）**：启用 `epoch_interruption` 后，wasmtime 的**默认 deadline = 0**，此时连 `instantiate` 都会立即 `wasm trap: interrupt`。必须先 `store.set_epoch_deadline(大值)` 完成实例化，再 `set_epoch_deadline(1)` + `engine.increment_epoch()` 来武装。
- 武装后 0.000s 内 trap → 这是比 `host_interrupt` 更硬的兜底（不轮询也停）。

---

## 3. 额外验证 E1–E4（07 报告 §6 的未实测项 + 两个关键测量）

### E1 `JS_SetMaxStackSize` 与 RangeError 的矛盾 — **已判明**（关闭 07 §6-1）
```
  max_stack_size=default  -> Trap: ... Caused by:\n    wasm trap: call stack exhausted
  max_stack_size=256KiB   -> returned 'caught:RangeError:Maximum call stack size exceeded'
```
**两份 README 都对，但描述的是不同配置**：
- 不调用 `qjs_set_max_stack_size` → 深递归触发 **wasm trap（不可捕获，执行直接死）**（vercel README 的说法）。
- 显式调用（如 256KiB）→ 得到**可捕获的 `RangeError: Maximum call stack size exceeded`**（上游 README:372 的说法）。
→ 移植时**必须显式设置栈上限**（上游 `vm.ts:162` 传 `MAX_STACK_SIZE`，默认 512KiB，正是为此）。

### E2 `Intrinsics.ALL & ~WEAK_REF` — **已判明，且不是近似**（关闭 07 §6-2）
```
  qjs_init2(0xfffffdff) return=0
  typeof probe: {"weakref":"undefined","fin":"undefined","proxy":"function","promise":"function",
                 "date":"function","perf":"object","map":"function","symbol":"function"}
  prelude loads under the mask: True
```
→ wasm 通过 `qjs_init2(mask)` 直接支持 intrinsic 掩码（`ALL = 0xFFFFFFFF`，`WEAK_REF = 1<<9`）。07 报告里「确定性模式只能靠删 `WeakRef` 近似」的担心**不成立**：可以用上游同一机制（`vm.ts:178`），而且**上游 prelude 在该掩码下照常装载**。
（补充：`performance` 仍是 object——上游并未屏蔽它，虚拟时间由 WASI 时钟提供。）

### E3 GIL 探针（纯计算 wasm 循环）— **关键架构结论**
```
  pure JS loop 20,000,000 iterations: 0.940s (value=199999990000000)
  background thread ticks during it : 155
  WASI callbacks during it         : {'clock': 0, 'random': 0}
  => GIL RELEASED during wasm execution
```
方法学说明：第一版探针用 `while(Date.now()-t0<500)`，每轮都回调 Python，无法区分「GIL 释放」与「回调让出 GIL」，判定无效；本版改为**纯计算循环**并断言期间 **0 次 WASI 回调** → 结论可靠：**wasmtime-py 在 wasm 执行期间释放 GIL**。

### E4 每次执行开销（上游：一次执行一个 VM）
```
  fresh engine+module          create=  68.4ms  prelude+tools=  5.3ms  full round-trip=  75.0ms
  shared compiled module       create=   5.0ms  prelude+tools=  5.2ms  full round-trip=  11.2ms
```
（各 10 次取中位数；"full round-trip" = 建 VM + 装 prelude + 2 次工具调用 + settle + drain + done）
- **共享已编译 Module 时，一次完整 codemode 执行的引擎开销 ≈ 11.2ms**，优于上游 worker+VM 的约 20ms（`README.md:362`）。
- 若每次重新 `Module(...)`（编译 637KB wasm）则 +63ms/次 → **必须缓存编译结果**（上游 `wasm.ts:14-34` 的 `loadQuickJSWasm` 就是按路径缓存，`build` 一次）。
- 若采用「每执行一个子进程」加固，还要叠加 ~30–60ms 进程启动（本 spike 未测子进程 VM，见 §5）。

---

## 4. 与 `recon/07-codemode-feasibility.md` 的差异（实测修订）

### 4.1 修订 1：进程模型从「必须子进程」降级为「线程够用，子进程可选」
07 报告 §A-3 建议「每次 `execute()` 一个子进程」，理由之一是「Python 线程无法中断正在跑的 wasm」。**E3 + S5b 证明这个理由不成立**：wasmtime 在 wasm 执行期间释放 GIL，Python 线程看门狗可以在 0.2ms 内让中断生效。因此：
- **默认架构**：与上游同构——同一进程内，每次执行一个 `Store`+`Instance`（独立线性内存），一个看门狗线程 + 共享标志（对应上游 `SharedArrayBuffer`），加上 wasm 级 epoch/fuel 兜底。
- **子进程**降级为**可选加固**：仅当需要「宿主崩溃/OOM 不拖垮主进程」「硬性 SIGKILL」时才启用，代价是每执行 +30–60ms。

### 4.2 修订 2：`Intrinsics` 与栈上限都可以 1:1 复刻（不是妥协项）
07 报告把这两条列为「已知妥协/未确定」；实测表明二者都有**与上游完全相同的机制**（`qjs_init2` 掩码、`qjs_set_max_stack_size`）。

### 4.3 修订 3：中断预算需重新标定，栈与 epoch 有「必须显式配置」的陷阱
见 §2 S5/S8b/E1 的三条实现要点，均已写入 §6 的实现清单。

---

## 5. 宿主可行性最小验证 + 与 600–1200 行估算的偏差

### 5.1 最小可运行片段（`/tmp/codemode-spike/host.py` 的真实代码）

```python
# 1) 实例化：自定义 WASI + env 宿主函数（不用 define_wasi）
linker.define(store, "env", "host_call",
              Func(store, FuncType([I32]*5, [I32]), self._host_call))
linker.define(store, "env", "host_interrupt",
              Func(store, FuncType([], [I32]), self._host_interrupt))
linker.define(store, "wasi_snapshot_preview1", "clock_time_get",
              Func(store, FuncType([I32, I64, I32], [I32]), self._clock_time_get))
# ... fd_write / fd_close / fd_fdstat_get / fd_seek / random_get （共 6 个）
self.instance = linker.instantiate(store, self.module)
self.exports = self.instance.exports(store)
self.memory = self.exports["memory"]

# 2) reactor 初始化 + QuickJS runtime/context（对应 dist/index.js:288-292）
self.exports["_initialize"](store)
init = self.exports["qjs_init2"] if intrinsics is not None else self.exports["qjs_init"]
init(store, intrinsics) if intrinsics is not None else init(store)
self.exports["qjs_set_interrupt_handler"](store, 1)     # 武装中断轮询
self.exports["qjs_set_memory_limit"](store, memory_limit)

# 3) 调一个 qjs_* 导出：eval("1+2") == 3
ptr, length = self.write_js_string("1+2")               # WTF-8 + wasm_malloc
handle = self.exports["qjs_eval"](store, ptr, length, self.write_cstring("<eval>"), 0)
assert self.exports["qjs_get_float64"](store, handle) == 3.0

# 4) 宿主函数：JS 调用回 Python（argv 是 argc 个 u32 handle）
def _host_call(self, name_ptr, name_len, this_ptr, argc, argv_ptr):
    name = self.read(name_ptr, name_len).decode()
    args = [self.u32(argv_ptr + 4 * i) for i in range(argc)]
    return self.exports["qjs_dup_value"](store, self.callbacks[name](args))
```

### 5.2 估算偏差

| 项 | 07 报告估算 | 实测 | 偏差 |
| --- | --- | --- | --- |
| Python↔wasm 引擎宿主 | 600–1200 行 | **326 行**（`host.py` 全文，含注释/文档字符串；覆盖 S1–S8 全部所需能力） | **估高了约 2–3 倍** |
| `runtime/vm.ts` 移植 | ~350 行 | 未做（S4/S6 已验证其关键路径：prelude 装载、drain 循环、settle、done） | 量级合理 |
| 生产版引擎宿主（含错误分类/句柄作用域/确定性时钟/类型标注/测试） | — | 估 **500–800 行** | 原估算的**下界**基本准确，上界偏保守 |

结论：**600–1200 行的估算「方向正确但偏保守」**，不构成立项阻碍；真正的成本在 host 侧语义移植（`execution.ts` 573 行）与等价性测试，而非引擎宿主。

---

## 6. 立项后的实现要点（本次实测换来的，直接可执行）

1. **缓存已编译的 `Module`**：每次执行只新建 `Store`+`Instance`（5ms vs 68ms）。上游同样按路径缓存（`wasm.ts:14-34`）。
2. **必须显式设置**：`qjs_set_max_stack_size`（否则深递归 → wasm trap 不可捕获）、`qjs_set_memory_limit`、`qjs_set_interrupt_handler(1)`。
3. **中断信号载体**：优先「共享标志 + 看门狗线程」（延迟 0.2ms，与上游 `SharedArrayBuffer` 同构）；epoch/fuel 作为不轮询场景的硬兜底。
4. **epoch 若启用，先给大 deadline 再武装**（默认 0 会连实例化都 trap）。
5. **fuel 预算要把 instantiate+init 的 ~1.94M 计入**。
6. **中断预算重新标定**：Python 宿主实测 ~26k polls/s；`100_000` 预算 ≈ 3.8s（不是上游说的 2s）。
7. **异常取值必须用 `qjs_get_exception()`**：`qjs_is_exception(handle)` 只回答「是不是异常」，该 handle 本身不是异常对象（直接取 `name`/`message` 会拿到空，本 spike 踩过）。JS 侧 glue 的 `throwIfException` 也是这个模式。
8. **一个 VM 只能跑一个脚本**（prelude 的 `finished` 闩锁，S4 实测）；每次执行新建 VM 或重新装载 prelude。
9. **脚本必须按上游包装**：`(async (tools, console) => {<body>\n})`（`vm.ts:327`），这样 `return`/顶层 `await` 才成立，且栈行号与输入一致。
10. **`Intrinsics` 掩码用 `qjs_init2(0xFFFFFFFF & ~(1<<9))`** 复刻确定性模式；`host_get_timezone_offset` 返回秒偏移（UTC 传 0）。

---

## 7. 若关键项失败时的 `mini-racer` 降级评估（本次不适用）

S1–S8 全部通过，**不触发降级**。为备将来（例如 wasm ABI 破坏性升级、性能不达标），07 报告 §4.5 列出的四项语义漂移仍然有效，本次实测又补充两条降级代价：
- `mini-racer` 的 `set_hard_memory_limit` 语义（"terminated" vs V8 致命错误）**仍未实测**，属于降级前必须先验证的一条；
- 上游 869 行 `sandbox.test.ts` 中涉及错误文本/栈格式的断言在 V8 上需重写（本 spike 已证明 QuickJS-wasi 侧栈格式 `at <eval> (<file>:line:col)` 与上游一致，这是路线 A 的隐性收益）。

---

## 8. 仓库零改动自证

本任务的写入范围**只有两处**：`/tmp/codemode-spike/**`（spike 代码、venv、日志）与 `recon/09-codemode-phase0-results.md`（本报告）。

```bash
$ ls /Users/mac/pi-py/.venv/lib/python3.13/site-packages/ | grep -i wasmtime
  仓库 .venv 中无 wasmtime（未安装任何依赖到仓库）
$ ls /tmp/codemode-spike
  __pycache__  abi-signatures.txt  host.py  prelude-src.mjs  prelude.js
  quickjs-wasi-3.6.2.tgz  quickjs.wasm  run.log  spike.py
```

`git status --porcelain` 当前**存在大量他人并发改动**（`packages/pi-chord/`、`packages/pi-durable/` 新增，`packages/pi-server/` 删除，`pyproject.toml`/`uv.lock` 等），它们与本任务无关，证据：

- `git diff -- pyproject.toml` 显示的是 `+ "pi-py-chord"` / `+ "pi-py-durable"` / `- "pi-py-server"` —— **是其他 teammate 的镜像工作**，不含任何 wasmtime/quickjs 依赖；
- 全仓库 tracked 文件 diff 里 `codemode` 关键词只有 8 处，全部出现在 `SYNC.md`(4)、`README.md`(1)、`.github/ISSUE_TEMPLATE/sync.md`(1)、`packages/pi-coding-agent/src/pi_coding_agent/tools.py`(2)，都是**包名引用/注释**（例如「`pi-ai → pi-chord → pi-durable → { pi-mcp, pi-codemode } → pi-coding-agent`」），**没有一处是 wasmtime/quickjs**；
- 本 spike 全程在 `/tmp/codemode-spike/.venv` 内运行，未执行过任何写入仓库的命令。

---

## 9. 仍无法确定项（更新后）

已关闭（07 报告的 3 项未实测）：~~wasm 上 `JS_SetMaxStackSize`~~（E1）、~~`Intrinsics.ALL & ~WEAK_REF`~~（E2）、~~wasmtime-py 自定义 WASI~~（S1）。另外关闭：~~内嵌 quickjs-ng 版本~~（S1 = 0.17.0）。

仍开放：

1. **确定性 `start()` 端到端未跑**：本次只验证了它的三块前提（`qjs_init2` 掩码、WASI 时钟/随机可自定义、`settle`/`drain` 可控）。`seq` 排序、虚拟时钟推进、指纹（FNV-1a prelude hash）、`onIdle`/`during` 语义仍是纸面设计，建议作为 M4 的首个验收项。
2. **子进程 + 完整父子 IPC 协议未实现**：S5 用的是「父进程内 VM + 子进程翻共享标志」，证明了共享内存路径可行，但「VM 在子进程、工具在父进程」的完整桥接（上游 `protocol.ts` 的 Python 等价物）未做。
3. **性能未压测**：`host_call` 在高频小工具调用下的吞吐；`MAX_OUTPUT_CHARS = 16Mi` / 100k 输出项上限下的内存与序列化行为；`wasm_malloc` 失败路径。
4. **平台矩阵未覆盖**：只在 macOS arm64 + Python **3.14.5** 上测过。pi-py 目标是 Python >= 3.11，需在 3.11/3.12/3.13 与 Linux（manylinux）复跑同一套 S1–S8（wasmtime 官方 classifiers 覆盖 3.9–3.14，风险低但需 CI 证据）。
5. **`qjs_set_interrupt_handler(1)` 的 C 侧实现细节**（是否每次进入 JS 重装 handler）未深究；行为已实测正确。
6. **快照/恢复**（quickjs-wasi 的 snapshot 能力）未测——codemode 不需要，但若将来想复用「确定性重放」，快照可能更省事。
7. **`mini-racer` 的 `set_hard_memory_limit` 语义**（降级路线的前提）未实测。

---

## 10. 原始证据清单

| 内容 | 位置 |
| --- | --- |
| 完整原始输出（197 行，本报告所有引用行的出处） | `/tmp/codemode-spike/run.log` |
| 最小宿主（326 行） | `/tmp/codemode-spike/host.py` |
| 14 项检查（619 行） | `/tmp/codemode-spike/spike.py` |
| 447 个导出签名（wasm ABI 参考） | `/tmp/codemode-spike/abi-signatures.txt` |
| wasm 本体（sha256 `d4c9375f…bbd9`） | `/tmp/codemode-spike/quickjs.wasm` |
| npm 原始 tarball（3.6.2） | `/tmp/codemode-spike/quickjs-wasi-3.6.2.tgz` |
| prelude 提取产物（17,233 字符） | `/tmp/codemode-spike/prelude.js` |
| 上游 prelude 原文 | `/tmp/pi-up/v1.3.0/packages/codemode/src/runtime/prelude-source.ts` |
| 07 可行性报告（本报告是其 Phase-0） | `recon/07-codemode-feasibility.md` |

**总判定：14/14 PASS（S1,S2,S3,S4,S5,S5b,S6,S7,S8a,S8b,E1,E2,E3,E4）→ `pi-codemode` 立项。**

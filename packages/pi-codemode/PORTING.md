# pi-codemode 移植注记

对应上游：[`@earendil-works/pi-codemode`](https://github.com/earendil-works/pi) **v1.3.0**

依据：[`recon/07-codemode-feasibility.md`](../../recon/07-codemode-feasibility.md)（选型）、
[`recon/09-codemode-phase0-results.md`](../../recon/09-codemode-phase0-results.md)（Phase-0 实测，14/14 PASS）。

上游入口点与本地模块的对应关系：

| 上游 | 本包 | 上游文件（v1.3.0） |
|---|---|---|
| `quickjs-wasi` 的 JS 绑定 + `runtime/vm.ts`（VM 驱动） | `pi_codemode.host` | `dist/index.js`（2786 行，只取所需子集）、`codemode/src/runtime/vm.ts`（341 行） |
| `runtime/execution.ts` 的 host 侧（工具表/结果装配） | `pi_codemode.sandbox` | `src/runtime/execution.ts`（573 行，本批次实现最小执行路径） |
| `src/types.ts` | `pi_codemode.types` | 254 行 |
| `identifier.ts` | `pi_codemode.identifier`（`sandbox` 与根面都 re-export） | 12 行 |
| `src/declarations.ts` | `pi_codemode.declarations` | 340 行 |
| `src/source.ts` | `pi_codemode.source` | 125 行 |
| `wasm.ts` 的 `loadQuickJSWasm` | `pi_codemode.host.load_quickjs_wasm` | 35 行 |
| `runtime/execution.ts` 的 `start()` / `deliver()`（确定性重放） | `pi_codemode.run`（`CodemodeSandbox.start`） | `execution.ts:530-566`、`vm.ts:202-336` |
| `vm.ts:102-125` 的虚拟时钟与种子随机、`:314-321` 的指纹 | `pi_codemode.deterministic` | |
| `QuickJS.snapshot()` / `restore()` / `registerHostCallback()` | `pi_codemode.host.QuickJSSnapshot`、`QuickJSVm.snapshot/restore/register_host_callback` | |
| _（上游无对应物）_ 子进程隔离 | `pi_codemode.subprocess.SubprocessCodemodeSandbox` | 本包的可选加固，见偏离 #11 |
| `runtime/prelude-source.ts` 的求值结果 | `_assets/prelude.js`（原文） | 522 行 TS → 17,233 字符 |
| `quickjs-wasi/quickjs.wasm` | `_assets/quickjs.wasm`（原文） | npm 包 3.6.2 |

---

## wasm 资产：入库 + 测试断言 sha256（本批次的选择）

**选择：把 `quickjs.wasm` 提交进包内**（`src/pi_codemode/_assets/quickjs.wasm`，637,405 字节），
并在 `tests/test_assets.py` 中断言 sha256 = `d4c9375f…bbd9`。

理由：

1. **可复现**：`uv sync` 后无需网络即可跑测试/使用，CI 不会因为 npm registry 波动而失败；
2. **防静默换版**：断言写死 digest，升级引擎必须显式改常量并重跑 Phase-0 断言；
3. **体积可接受**：637 KB（MIT，`THIRD_PARTY_NOTICES.md` 记录来源与许可），对比 wasmtime 自身 wheel
   约 8 MB 可忽略；
4. **与上游锁版本对齐**：上游 `packages/codemode/package.json:70` 锁 `quickjs-wasi@3.6.2`，本地就是同一份二进制。

升级步骤（写入 `scripts/fetch_quickjs_wasm.py`，需要显式跑）：

```bash
python scripts/fetch_quickjs_wasm.py 3.6.2     # 下载 → 校验 → 覆盖 _assets/
uv run pytest packages/pi-codemode             # sha256 断言会失败，提示更新常量
```

脚本会打印新 digest；确认后更新 `assets.WASM_SHA256` / `WASM_SIZE`，并重跑
`recon/09-codemode-phase0-results.md` 里的 S1–S8（`tests/test_engine.py` 已把它们固化成回归）。

`prelude.js` 同理：它是上游 `PRELUDE_SOURCE` 模板字面量在 v1.3.0 的**求值结果**，产生方式：

```bash
sed 's/^export const PRELUDE_SOURCE: string = /export const PRELUDE_SOURCE = /' \
  packages/codemode/src/runtime/prelude-source.ts > /tmp/prelude-src.mjs
node -e "import('/tmp/prelude-src.mjs').then(m => require('fs').writeFileSync(
  'packages/pi-codemode/src/pi_codemode/_assets/prelude.js', m.PRELUDE_SOURCE))"
```

只剥掉 TS 类型注解一行，其余零改动（`tests/test_assets.py` 断言字符数、行数、digest 与结构标记）。

---

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| 1 | Node/Bun **worker thread** + `SharedArrayBuffer` 中断标志 + `worker.terminate()` | **同进程**：每次执行一个 wasm 实例 + 看门狗线程 + VM 内 `host_interrupt` + wasm epoch 硬 trap | Python 侧无需 worker：Phase-0 E3 实测 wasmtime 在 wasm 执行期间释放 GIL，线程看门狗 0.2 ms 内可中断（S5b）。进程隔离降级为后续可选加固 |
| 2 | 栈上限 `MAX_STACK_SIZE` = 512 KiB（worker 线程） | **256 KiB** | 实测：wasmtime 默认 wasm 栈 512 KiB 下，512/384/1024 KiB 的 QuickJS 上限都先耗尽 wasm 栈 → **不可捕获的 `wasm trap: call stack exhausted`**；256/128 KiB 才给出可捕获 `RangeError`。上游对「跑在调用者线程上」的 inline 沙箱同样用 256 KiB（`inline.ts:42`），我们是同一处境。`tests/test_engine.py::test_upstream_worker_stack_limit_would_trap` 固化了这个事实 |
| 3 | `QuickJS.create({wasm})` + TS 绑定（2786 行） | 只实现所需子集（~26 个 `qjs_*` 导出） | 全量移植没有必要；Phase-0 S1–S8 已覆盖 codemode 需要的全部能力 |
| 4 | `memoryLimitBytes` 默认「无限制（wasm32 4 GiB）」 | 默认 256 MiB | 取 coding-agent 的实际取值（`coding-agent/.../execute.ts:56`），避免默认无上限 |
| 5 | 工具 `execute` 是 `async` | 本批次为**同步** callable | 骨架范围；异步/`asyncio` 集成见「后续批次」 |
| 6 | `signal: AbortSignal` | `abort_event: threading.Event` | Python 侧最小对等物；语义相同（`kind="aborted"`） |
| 7 | 结果类型是 TS 联合类型 `{ok:true,…} \| {ok:false,…}` | 单一 `CodemodeResult` dataclass（`ok` + 可选 `error`） | Python 侧更自然；字段名与语义逐项对应 |
| 8 | `CODEMODE_SOURCE_GRAMMAR` 由 **lark** 语法描述，但上游**不用它解析**：`parseCodemodeSource` 是手写字符串逻辑，语法只作为 provider constrained-sampling 的常量导出（`coding-agent/.../tool.ts:387` 的 `variants.openai_lark`） | 原样导出 184 字符的语法常量（逐字节相同），解析仍是手写实现 | **不引入 `lark` 依赖**：依赖只服务一个从不被执行的常量。语义差异仅在 JSON 报错文本（V8 vs Python），错误前缀与全部校验规则逐条对齐 |
| 9 | `loadQuickJSWasm(path?)` 返回 `Promise<WebAssembly.Module>` | `load_quickjs_wasm(path=None, *, epoch_interruption=True, consume_fuel=False) -> QuickJSEngine`（**同步**） | 上游的 Promise 来自 `WebAssembly.compile` 是异步的；Python 侧本地文件读取同步即可。返回类型是「已编译 + 可实例化」的 `QuickJSEngine`（wasmtime 的 `Module` 绑定 `Engine`，单独返回 `Module` 会逼调用方自己持有 Engine）。按 `(path, 配置)` 缓存，复现上游「按路径编译一次」 |
| 10 | `$ref` 片段用 `decodeURIComponent` 解码（畸形转义会抛 `URIError`） | `urllib.parse.unquote`（不抛） | 渲染器不应因为一个畸形 `$ref` 把整份声明炸掉；JSON Pointer 的 `~1`/`~0` 规则一致 |
| 11 | 上游只有 worker thread / Durable Object 两种 VM 位置 | 追加 **`SubprocessCodemodeSandbox`**（可选）：VM 在子进程、工具在父进程 | 同进程 + 看门狗已能中断（Phase-0 S5b），子进程只用于「wasm trap / 崩溃不拖垮宿主」这一条；用 `spawn` 而不是 `fork`（父进程持有 wasmtime engine 的内部线程） |
| 12 | `QuickJS.restore(snapshot)` 之后用 `registerHostCallback(name, fn)` 重新登记 | `QuickJSVm.restore(snapshot, callbacks=...)` | 宿主回调读的是**自己那个 VM** 的句柄，restore 后必须重新绑定（`test_snapshot.py` 里用工厂函数演示）；只给名字不够，所以 `callbacks` 收映射 |
| 13 | `run.result` 是 Promise、`run.finished` 是布尔 | `run.result` 是 `concurrent.futures.Future[CodemodeResult]`（`.result(timeout)` 阻塞取结果），`finished` 仍是布尔属性 | Python 侧没有 Promise；`Future` 是标准库对等物，且支持超时（否则一次永不 deliver 的 run 会挂住调用方） |

---

## 平台矩阵（实测范围）

| 平台 | Python | 结果 |
|---|---|---|
| macOS 27.2 / arm64 | 3.14.5（Phase-0 spike 的临时 venv） | S1–S8 + E1–E4 全过 |
| macOS 27.2 / arm64 | **3.13.3（仓库 venv / CI 实际运行）** | 109 条测试全过（含子进程用例，`spawn` 路径） |
| Linux / Windows | — | **未实测**。wasmtime 官方 wheel 覆盖 3.9–3.14 与三平台，风险低，但 CI 证据缺失；子进程隔离在 Linux 上会走 `spawn`（本包显式指定），Windows 未验证 |

## 已固化的 Phase-0 断言（`tests/`）

| 断言 | 测试 |
|---|---|
| 实例化 + `qjs_init` + 引擎版本 `0.17.0` | `test_engine.py::test_instantiate_and_init` |
| `qjs_eval` 数值/字符串往返 | `test_engine.py::test_eval_roundtrip` |
| `host_call` 蹦床（参数 + 返回值） | `test_engine.py::test_host_call_trampoline` |
| 内存上限可捕获 | `test_engine.py::test_memory_limit_is_catchable` |
| 栈上限 → 可捕获 `RangeError` | `test_engine.py::test_stack_guard_gives_catchable_range_error` |
| wasm epoch 硬 trap | `test_engine.py::test_epoch_tick_hard_stops_wasm` |
| `while(true){}` 可被线程看门狗中断 | `test_engine.py::test_thread_watchdog_interrupts_tight_loop` |
| `while(true) await null` 可中断 | `test_engine.py::test_job_drain_polls_between_jobs` |
| Module 复用 | `test_engine.py::test_engine_reuses_one_compiled_module` |
| prelude 原文装载 + 工具往返 | `test_prelude.py::test_tool_roundtrip_through_the_prelude` |
| 端到端超时/abort/预算 | `test_interrupt.py` |
| wasm/prelude sha256 | `test_assets.py` |
| `render_declarations` / `render_tool_signature` / `render_tool_sample` / `schema_to_type` | `test_declarations.py`（上游 `test/declarations.test.ts` 328 行逐条移植） |
| `parse_codemode_source` / `CODEMODE_SOURCE_GRAMMAR` | `test_source.py`（上游 `test/source.test.ts` 78 行逐条移植 + 语法常量逐字节断言） |
| `load_quickjs_wasm` 按路径缓存 | `test_engine.py::test_load_quickjs_wasm_compiles_once_per_path` |
| **确定性 `start()`：同种子两次运行逐字段相同** | `test_deterministic.py::test_same_seed_produces_an_identical_trace` |
| 换种子只改随机性、不改虚拟时钟 | `test_deterministic.py::test_a_different_seed_changes_only_the_randomness` |
| `seq` 顺序应用（先到 2 也要等 1）、`during`、`store_writes`、失败带 properties | `test_deterministic.py`（16 条） |
| 快照/恢复：句柄跨 VM 有效、回调按名重登记、内存增长 | `test_snapshot.py`（4 条） |
| 子进程：跨进程工具往返、超时杀子进程、SIGKILL 崩溃隔离 | `test_subprocess.py`（4 条） |

---

## `timeout_ms` 的「无期限」语义：`None` 与 `inf` 等价（跨包修复，2026-10-18）

**背景**：`pi-coding-agent` 移植 `extensions/codemode/execute.ts` 时，上游传的是
`timeoutMs: sourceOptions.timeoutMs ?? Number.POSITIVE_INFINITY` —— **不设 `@options` 期限的脚本
应当无限期运行**。本端若把 `inf` 直接交给 `threading.Event.wait(timeout=)`，会走到
`threading.py` 的 `waiter.acquire(True, timeout)` 并抛：

```
OverflowError: timestamp out of range for platform time_t
```

**这是一次典型的「静默失效」**：异常抛在**看门狗线程内部**，主路径照常返回
`ok=True`（脚本结果正确），**只有 stderr 留下 traceback** —— 于是「超时保护已经不起作用」
这件事没有任何调用方看得见。

**修复**：新增 `run._deadline_seconds(timeout_ms) -> float | None`，把 `None` 与**所有非有限值**
（`inf` / `-inf` / `nan`）一律视为「无 deadline」。两处 deadline 消费点改用它：

* `run.py: CodemodeRun._wait_for_delivery` —— 泵的等待
* `run.py: CodemodeRun._start_watchdog` —— **无 deadline 就不起看门狗线程**
* `sandbox.py: CodemodeSandbox._start_watchdog` —— 同上（`_start_watchdog` 现在返回 `Thread | None`，
  调用点判空后 join）

**语义**：`None` == `inf` == `-inf` == `nan` == 「不设期限」。有限值行为不变（有测试锁住
`timeout_ms=1.0` 仍在 `kind="timeout"` 上触发，防止「为了修 inf 把看门狗整个关掉」）。

**测试**：`tests/test_timeout_deadline.py`（5 条）—— `_deadline_seconds` 的四类输入、
`inf` 下**不起看门狗线程**、`{}`/`None` 仍可跑、有限期限仍触发、以及
**断言 stderr 里没有 `OverflowError`**（那正是本机唯一能看见的信号）。

**授权范围**：本修复由 lead 授权、**有界** —— 只动 `pi-codemode` 里与 timeout/deadline 相关的处理，
未触碰该包其它任何内容。

## globals 的 `spread`：多参全局必须收到全部实参（跨包修复 #2，2026-10-18）

**背景**：`pi-coding-agent` 移植 `extensions/codemode/execute.ts` 的
`createDiscoveryGlobals` / `createModelGlobals` 时发现，上游的**发现类全局与 `models.*` 全部**声明
`spread: true`（`execute.ts:601/623/635/771`），而本仓 `CodemodeTool` **没有 `spread` 字段**，
`_globals_json()` 把 `"spread": False` **硬编码**。

**后果（实测，不是推断）**：

```
单参全局:  one("a")        → 宿主收到 ['a']       ✅
多参调用:  two("a", "b")   → 宿主收到 ['a']       ⚠️ 第二个实参丢了，ok=True（**不报错**）
```

语义由 prelude 决定：`serialize(spread ? args : args[0])`（`prelude.js:155`）。
影响面：`searchTools(query, {limit, namespace})` **静默**退回默认 limit 8 条、**不按 namespace 过滤**；
`models.getModelOfType(type, provider, id)` 只收到 `type` → 走到误导性报错；
`models.classify(model, context)` 的 context 丢失 → 报「expects a context object」同样是误导。
只有 `describeTool(name)` / `describeNamespace(name)` 两个单参全局不受影响。

**修复**：`CodemodeTool` 加 `spread: bool = False`（`CodemodeDeclaredGlobal` 早有同名字段可参照），
`_globals_json()` 改为取 `tool.spread` 而不是硬编码。语义与上游一致，默认值不变。

**测试**（`tests/test_globals_spread.py`，5 条）：带**两个**实参调用两参全局 → 宿主收到两个；
**反面**：`spread=False` 仍只收到第一个（锁住既有语义）；默认值为 `False`；
`searchTools("docs", {limit, namespace})` 的真形状；不传实参时收到空列表。

> **为什么这个 bug 能藏这么久**：它藏在**序列化**里。任何只测「属性能设上」「单参能跑通」的测试
> 都看不见它 —— 所以测试要复现**上游那个真实失败场景**（多参 + 断言收到几个），
> 而不是断言 API 形状。

## 模式：**骨架阶段实现的机制，在第一个真实消费者到来时要重新核对「参数传递/边界」**

这是 `pi-codemode` **第二次**出现「只有真消费者到来才暴露」的缺口：

| # | 缺口 | 触发条件 | 自身 114 条测试为何测不出 |
|---|---|---|---|
| 1 | `timeout_ms=inf`（看门狗 `wait(inf)` 溢出） | 有消费者传 `inf`（上游用 `POSITIVE_INFINITY` 表示无期限） | 没有传 `inf` 的用例 |
| 2 | globals 的 `spread` 被硬编码为 `False` | 有多参全局的消费者（`searchTools` / `models.*`） | **当时没有任何多参全局的消费者** |

**判据**：`pi-codemode` 是按「最小执行路径」建的（骨架），其测试覆盖的是**当时的**调用形状。
**第一个真实消费者到来时，要重新核对机制在「参数传递 / 边界 / 可选值」上是否完整** ——
不只是「能不能跑通」。上面两次都是「跑得通」，但结果不对。

## 后续批次（本批次明确不做）

| 批次 | 内容 | 依据 |
|---|---|---|
| ~~D3~~ | ~~声明层：`declarations.ts` 340 行~~ | **已完成** |
| ~~D4~~ | ~~源码格式：`source.ts` 125 行~~ | **已完成** |
| ~~D5~~ | ~~确定性 `start()`：虚拟时钟、种子随机、`seq` 排序、指纹~~ | **已完成**（`run.py` + `deterministic.py`，16 条测试，含同种子逐字段一致） |
| ~~D6~~ | ~~子进程崩溃隔离 + 完整 IPC 桥~~ | **已完成**（`subprocess.py`，4 条测试：往返 / 超时杀 / SIGKILL 隔离 / 杀后可用） |
| ~~D7~~ | ~~快照/恢复~~ | **已完成**（`QuickJSSnapshot` + `snapshot()/restore()`，4 条测试） |
| D2 | 异步工具 + `execute_async`（`asyncio.to_thread` 跑 VM，工具结果经队列回灌） | pi-py 全栈是 asyncio；`CodemodeTool.execute` 目前是同步 callable |
| D8 | `RemoteCodemodeSandbox`（Cloudflare/Durable Object 端） | pi-py 无该运行环境，**不做**（记为不适用，而不是未完成） |

### 遗留实现注记（踩过的坑，别踩第二次）

1. `qjs_is_exception(handle)` 只回答「是不是异常」；异常值要用 `qjs_get_exception()` 取
   （JS 绑定的 `throwIfException` 也是这个模式）。
2. **epoch 默认 deadline = 0**：启用 `epoch_interruption` 后若不先 `set_epoch_deadline(EPOCH_IDLE)`
   连 `instantiate` 都会 trap。武装真实 deadline 必须放在 prelude 装载**之后**。
3. fuel 预算要把 `instantiate + qjs_init` 消耗的 **~1.94 M** 计入（`set_fuel` 设的是脚本预算）。
4. 不要暴露 `max_wasm_stack` 旋钮：它不得超过 `async_stack_size`，越界时 wasmtime 是 **Rust panic
   → 进程 abort**，不是 Python 异常。
5. prelude 的 `finished` 闩锁意味着**一个 VM 只能完成一个脚本**；每次 `execute()` 必须新建 VM
   （上游 worker 模型同理）。
6. 中断轮询率随宿主语言变化（Python 实测 ~26k 次/秒，上游 JS ~50k 次/秒），
   `interrupt_budget` 需按平台标定。
7. 多行成员要整体缩进：`objectType` 在拼接成员时把成员文本里的**每个换行**替换成
   「换行 + 两级空格」（上游 `members[index].replaceAll("\n", "\n" + INDENT)`），
   否则嵌套多行对象会少一层缩进（本批次踩过）。
8. `Number.isSafeInteger` 的 Python 等价物要排掉 `bool`（`True` 是 `int` 的子类）并把
   整数值的 `float`（JSON 的 `1.0`）算作整数，否则 `// @options: {"timeout_ms": 1500.0}`
   会被误拒。
9. mypy 无法从自定义谓词收窄 `object`，`_is_number` 用 `TypeGuard[int | float]`。
10. 确定性 run 的 pump 必须**先 drain promise job 再判断 stalled**：`return 1;` 这类脚本的
    `done` 是在 job 队列里产生的，先判 stalled 会把正常脚本误判为「永远不会有结果」（本批次踩过）。
11. `deliver()` 的失败载荷是 `outcome.message`（不是 `value`）；值部分只在 `ok=True` 时 JSON 序列化
    （上游 `execution.ts:276-280`）。
12. 子进程桥要对**写管道失败**免疫：工具执行期间子进程被杀时，父进程的 `send` 会抛
    `BrokenPipeError`，必须转成 `kind="sandbox"` 的结果而不是让异常冒出去。
13. 期限一到，**宿主的裁决优先**：子进程随后送来的 `done` 不改变 `kind`（对齐上游
    `execution.ts` 的「第一个终态事件生效」）。

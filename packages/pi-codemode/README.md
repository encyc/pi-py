# pi-codemode

`@earendil-works/pi-codemode` v1.3.0 的 Python 移植：**在 QuickJS（编译为 WebAssembly）里运行模型写的
JavaScript，脚本唯一的能力是调用注入的工具**。嵌套的工具调用不进入 LLM 上下文，只有脚本的输出与
返回值进入。

引擎与上游**同一份二进制**：`vercel-labs/quickjs-wasi` **3.6.2** 的 `quickjs.wasm`
（sha256 `d4c9375f…bbd9`），由 `wasmtime` 从 Python 侧驱动其 C ABI；上游的
`PRELUDE_SOURCE`（17,233 字符）**原文装载**，零改动。

```python
from pi_codemode import CodemodeSandbox, CodemodeTool

sandbox = CodemodeSandbox(
    timeout_ms=30_000,
    tools=[
        CodemodeTool(
            name="read",
            description="Read a file",
            execute=lambda args: open(args["path"]).read(),
        )
    ],
)

result = sandbox.execute("""
    const source = await tools.read({ path: "package.json" });
    text("bytes " + source.length);
    return JSON.parse(source).name;
""")

print(result.output)          # [CodemodeOutputItem(type='text', text='bytes 1234')]
if result.ok:
    print(result.value)       # "pi-py-codemode"
else:
    print(result.error.kind, result.error.message)
```

## 脚本里有什么

与上游完全一致（由 prelude 提供）：

- `await tools.<name>(args)` —— 参数与结果各经一次 JSON 往返；工具抛错在脚本里表现为 `Error`。
  `my-tool` 同时可用 `tools.my_tool` 与 `tools["my-tool"]`。
- `ALL_TOOLS`、`text(value)`、`image(dataUriOrBlock)`、`exit()`、`console.log/info/warn/error/debug`。
- `store(key, value)` / `load(key)`（同步，快照式；成功执行后在 `result.store_writes` 报告改动）。
- 除此之外什么都没有：没有 timers、`fetch`、`process`、模块，也没有 `WebAssembly`。

## 中断与限额

| 能力 | 实现 |
| --- | --- |
| 超时 / abort | 看门狗线程先 `host_interrupt` 置位（VM 内抛不可捕获错误），仍不停止则推进 wasm epoch **硬 trap** |
| 中断预算 | `interrupt_budget=N`（对齐上游 `InlineCodemodeSandbox`）：按宿主轮询计数，超预算报 `timeout` |
| 内存上限 | `memory_limit_bytes`（默认 256 MiB）：脚本内可捕获 `InternalError: out of memory` |
| 栈上限 | 默认 **256 KiB**：深递归是可捕获的 `RangeError`（见 `PORTING.md` 的定标说明） |
| 隔离 | 每次 `execute()` 一个全新的 wasm 实例（独立线性内存）；`Module` 只编译一次并复用 |

中断后 VM 保持可用（与上游一致）；执行结果里 `error.kind ∈ {script, timeout, aborted, sandbox}`，
`execute()` 不会因脚本失败而抛异常。

## 体积与性能

- 包内自带 `quickjs.wasm`（637,405 字节）+ `prelude.js`（17 KB），无网络依赖；两个资产的 sha256 在
  `tests/test_assets.py` 中断言。
- 编译一次 wasm 约 68 ms，之后每次执行（建 VM + 装 prelude + 工具往返）约 **11 ms**（Phase-0 E4）。

## 声明层（模型可见的工具描述）

```python
from pi_codemode import ToolDeclaration, render_declarations, render_tool_sample, parse_codemode_source

render_tool_sample(ToolDeclaration("read", "Read a file", {"type": "object",
                   "properties": {"path": {"type": "string"}}, "required": ["path"]}, {"type": "string"}))
# 'Read a file\n\ncodemode tool declaration:\n```ts\ndeclare const tools: { read(args: { path: string; }): Promise<string>; };\n```'

parse_codemode_source('// @options: {"timeout_ms": 30000}\nreturn 1;')
# ParsedCodemodeSource(code='\nreturn 1;', options=CodemodeSourceOptions(max_output_tokens=None, timeout_ms=30000))
```

`render_declarations` / `render_tool_signature` / `schema_to_type` 与上游 `declarations.ts` 逐字符一致
（测试直接对照上游期望字符串）；`CODEMODE_SOURCE_GRAMMAR`（184 字符 Lark 语法）逐字节照搬，供 provider
的 constrained sampling 使用。

## 确定性重放（`start()`）

```python
run = sandbox.start(code, CodemodeRunOptions(
    started_at=1_700_000_000_000, seed=7,
    tools=[CodemodeDeclaredTool("read")],
    on_call=lambda call: run.deliver(1, 1_700_000_001_000, call.id, CodemodeOutcome.success("...")),
))
result = run.result.result(timeout=10)     # Future，可带超时
```

同一个脚本 + 同一个 `started_at`/`seed`/工具/globals/store，并按 `seq` 顺序回灌同样的结果 ⇒
**调用序列、参数、输出、返回值、指纹全部相同**（`test_deterministic.py::test_same_seed_produces_an_identical_trace`）。
VM 运行在 UTC，`Date.now()` 读 `started_at` 与所处理结果的 `now`（只前进），`Math.random()` 由 `seed` 派生，
`WeakRef`/`FinalizationRegistry` 不存在。

## 快照与子进程隔离

```python
snapshot = vm.snapshot()                       # 整块线性内存 + runtime/context 指针
restored = engine.new_vm(arm_interrupt=False)
restored.restore(snapshot, callbacks={"bridge": make_bridge(restored)})   # 回调需绑定到新 VM

from pi_codemode import SubprocessCodemodeSandbox
sandbox = SubprocessCodemodeSandbox(tools=[...], timeout_ms=30_000)       # VM 在子进程、工具在父进程
```

子进程隔离是**可选加固**：同进程 + 看门狗线程已经能在 0.2ms 内中断（wasmtime 在执行 wasm 时释放 GIL），
子进程多给的是「wasm trap / 崩溃不拖垮宿主」与 `SIGKILL` 兜底。

## 本批次未实现

异步工具与 `execute_async`（`CodemodeTool.execute` 目前是同步 callable）、`RemoteCodemodeSandbox`
（Cloudflare/Durable Object 场景，pi-py 不适用）、Linux/Windows 的 CI 实测。
详见 [`PORTING.md`](./PORTING.md)。

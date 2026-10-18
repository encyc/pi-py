# pi-chord 移植注记

对应上游：[`@earendil-works/chord`](https://github.com/earendil-works/pi)（v1.3.0）

| 批次 | 上游子路径 | 本包模块 | 状态 |
|---|---|---|---|
| **A** | `@earendil-works/chord/context`（133 行） | `pi_chord.context` | ✅ 完成 |
| **B** | `@earendil-works/chord/json.ts`（135 行） | `pi_chord.json` | ✅ 完成 |
| **B** | `@earendil-works/chord/delta`（op 语义层） | `pi_chord.delta`（ops/apply/textdiff/errors） | ✅ 完成 |
| **B** | `@earendil-works/chord/delta/tracker.ts`（2,239 行） | `pi_chord.delta.tracker` | ✅ 完成（**见 T 系列偏离**） |
| **B/D** | `@earendil-works/chord/state.ts`（423 行） | `pi_chord.state` | ✅ 完成（**见 S 系列偏离**） |
| **B/D** | `@earendil-works/chord/output.ts`（383 行） | `pi_chord.output` | ✅ 完成（**见 O 系列偏离**） |
| — | 宿主半边（`host`/`contract`/`object`/`link`/`reload`/`registry`/…） | — | ❌ **不移植**（v1.3.0 全仓库零消费） |

> **chord 的 `delta` 岛已全部落地。** 上游 `chord/src/` 中真正被 `pi_durable` 消费的
> 部分（`delta/` + `context/` + `json.ts` + `state.ts` + `output.ts`）**已 100% 移植**，
> 仅剩三处**有意**的简化：数组 piece/permutation 子系统（T3）、
> `delta/apply-immutable-trusted.ts` 快速路径（T5）、`OutputBuffer` 的性能细节。
>
> 「tracker 完成度：全部」的界定同 task-19：上游 `tracker.ts` 的公开契约
> **4 类型 + 5 方法全部实现**；piece/permutation 数组差分子系统属于
> `delta/README.md:94` 明确允许的实现自由度（op 形态非契约），见下 T3。

---

# 批次 A —— `context`

（子路径 `@earendil-works/chord/context`，上游 `packages/chord/src/context/index.ts`，133 行。）

## 覆盖范围

| 上游 | 本包 | 上游文件 |
|---|---|---|
| `@earendil-works/chord/context` | `pi_chord.context` | `chord/src/context/index.ts`（133 行） |
| `@earendil-works/chord`（根，仅类型再导出 `Context`/`ContextKey`） | `pi_chord`（同样只再导出这两个名字） | `chord/src/index.ts:3` |

`chord/src/index.ts` 的根入口把 `Context`、`ContextKey` 作为**类型**再导出（第 3 行
`export type { Context, ContextKey } from "./context/index.ts";`），所有**值**导出都挂在
`./context` 子路径上；本包保持同样的分层：`pi_chord` 只再导出两个类型，
`BACKGROUND_CONTEXT` / `with_abort_signal` 等值只在 `pi_chord.context` 里。

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| 1 | `AbortSignal` / `AbortController` 由运行时（DOM/Node 全局）提供：`signal.aborted`、`signal.reason`、`addEventListener("abort")`、`AbortSignal.any([...])`、`controller.abort(reason)` | 在 `pi_chord.context` 内自带一份最小实现：`AbortSignal.aborted` / `.reason` / `.add_listener` / `.remove_listener` / `AbortSignal.any()`、`AbortController.abort(reason)`，外加 `AbortError` 代替 `DOMException("AbortError")` | Python 没有对等物。语义按上游实际用到的部分实现：`context/index.ts:15-17`（key）、`83-87`（`withAbortSignal` 用 `AbortSignal.any` 合并父/子信号）、`94-104`（`withCancel`）、`110-128`（`awaitWithContext` 监听 abort）、`130-133`（`abortError` 取 `signal.reason`） |
| 2 | `abortError()` 返回 `Error`（`context/index.ts:130-133`） | `abort_reason()` 返回 `BaseException`；`AbortError` 继承 `Exception` | Python 里「抛出的东西」必须是 `BaseException` |
| 3 | `awaitWithContext(promise, context)` 是普通函数，返回 `Promise` | `await_with_context(awaitable, context)` 是 `async def` | Python 没有「普通函数返回挂起值」的形式；`await` 语义与上游一致 |
| 4 | `Context.value<T>(key)` 用泛型方法 + 类型擦除的 `valueType` 标记 | `Context.value(self, key: ContextKey[Value]) -> Value \| None`，`ContextKey[T]` 用 `Generic[T]` | Python 的泛型方法就是上游 `value<T>` 的对应物；`valueType` 标记是纯类型层的，Python 用类型参数表达 |
| 5 | `interface Context` / `abstract class BaseContext` | `class Context(ABC)` / `class BaseContext(Context)` | Python 的无显式实现声明用 ABC 表达 |
| 6 | 派生上下文类不导出（`EmptyContext`、`ContextValue` 都是模块私有） | 同样私有（`_EmptyContext`、`_ContextValue`） | 与上游可见性一致 |
| 7 | `withCancel` 返回对象字面量 `{context, cancel}` | 返回冻结 dataclass `CancelScope(context, cancel)` | 结构化类型的对应物 |
| 8 | 公开函数/属性 camelCase（`withAbortSignal`、`withoutAbortSignal`、`createContextKey`、`Context.abortSignal`） | snake_case（`with_abort_signal`、`without_abort_signal`、`create_context_key`、`Context.abort_signal`） | PEP 8；类名保持上游拼写（`Context`、`ContextKey`、`AbortSignal`、`AbortController`） |
| 9 | `instanceof Error` 判断 abort reason | `isinstance(reason, BaseException)` | 语言等价物 |
| 10 | 版本随上游（`chord/package.json` = 1.3.0） | 本包 `1.3.0`，与仓库其余包（`0.85.1`）不同 | 本包直接镜像 v1.3.0 源码，没有 0.85.1 对应物 |

## 语义细节（与上游逐条对齐，均有测试）

| 语义 | 上游 | 本包测试 |
|---|---|---|
| 两个描述相同的 key **不是**同一个 key（按 token 身份比较） | `context/index.ts:57-59` | `test_keys_are_identified_by_token_not_description` |
| 派生值遮蔽父值，但父上下文不变 | `context/index.ts:74-77` | `test_with_context_value_shadows_and_falls_back_to_parent`、`test_deriving_a_value_does_not_mutate_the_parent` |
| `str(context)` 描述派生链，空描述回退 `anonymous` | `context/index.ts:62-64` | `test_string_form_describes_the_derivation` |
| `withAbortSignal` 合并父、子信号（任一取消都生效） | `context/index.ts:83-87` | `test_with_abort_signal_combines_parent_and_child` 等 |
| `withoutAbortSignal` 只去掉取消、保留其它值 | `context/index.ts:89-92` | `test_without_abort_signal_drops_cancellation_only` |
| `withCancel` 派生出可独立取消的子上下文 | `context/index.ts:94-104` | `test_with_cancel_derives_an_independent_child` |
| `awaitWithContext` 只取消等待者，底层 promise 照常跑完 | `context/index.ts:106-128` | `test_await_with_context_cancels_only_the_waiter`、`test_await_with_context_raises_immediately_when_already_aborted` |
| `awaitWithContext` 透传自定义 reason | `context/index.ts:130-133` | `test_await_with_context_propagates_custom_reasons` |

---

# 批次 B —— `delta`（op 语义层）

上游 `packages/chord/src/delta/`。本批**只做 op 语义层**：
`delta/index.ts` 的类型与校验、`apply` 三件套、以及 `tracker.ts` / `diff.ts` 里的
**字符串级发射**。

| 上游 | 本包 | 上游文件:行 |
|---|---|---|
| `Op` / `Path` / `Seg` / `NonEmptyPath` | `pi_chord.delta.ops` | `delta/index.ts:14-38` |
| `isReplace` / `isBase` | 同上 | `delta/index.ts:70-76` |
| `overlap` | `pi_chord.delta.textdiff` | `delta/index.ts:87-112` |
| `RESERVED_SEGMENTS` / `UnsafePathError` / `PathError` | `pi_chord.delta.errors` | `delta/index.ts:131-149,310-317` |
| `assertValidOp` / `assertPathArg` / `assertPermutation` | `pi_chord.delta.ops` | `delta/index.ts:152-208` |
| `assertSafePath` / `assertIndexInRange` | 同上 | `delta/index.ts:279-306` |
| `apply` / `applyOps` | `pi_chord.delta.apply` | `delta/index.ts:326-406` |
| `applyImmutable` / `applyImmutableBatches` | 同上 | `delta/index.ts:408-441` |
| `appendCopy` / `copyContainers` / `resolveValue` / `resolve` | 同上 / `ops` | `delta/index.ts:443-540` |
| 字符串级发射（`a` / `t` / `s`） | `pi_chord.delta.textdiff.emit_string_ops` | `delta/tracker.ts:2039-2063`、`delta/diff.ts:63-76` |

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| D1 | op 是数组（`readonly ["s", NonEmptyPath, JsonValue]`），`Path = readonly Seg[]` | op 是 `tuple`，`Path = tuple[Seg, ...]` | 上游注释自己说 op 是「内存/线上/磁盘同一形态」（`delta/index.ts:19-20`），元组保真且可哈希、可比较。JSON 往返时元组 ↔ 数组互转 |
| D2 | `NonEmptyPath` 用 `readonly [Seg, ...Seg[]]` 在**类型层**强制非空 | Python 无法表达「非空元组」，改为 `assert_valid_op` **运行时**校验（`path is empty`） | 与上游行为等价：上游 `assertValidOp` 也做运行时检查（`delta/index.ts:160,164,168,172` 的 `assertPathArg(op[1], true)`） |
| D3 | `apply<T>(target: T \| undefined, ops): T` 泛型 | `apply(target: T, ops) -> T`，内部 `cast` 到 `JsonValue` 再 `cast` 回 `T` | 静态形态不同，运行时语义一致 |
| D4 | `WeakSet` 记录已拷贝容器（`delta/index.ts:421`） | `dict[int, object]`（`id(容器) -> 容器`），**保留强引用** | 仅存 `id` 的话，一个不再被树引用的容器可能被回收、`id` 被复用，导致另一个对象被误判为「已拷贝」。保留强引用消除该风险 |
| D5 | `Object.defineProperty(parent, key, {...})` 写入（`delta/index.ts:377-379`），避免原型链 setter | 直接 `container[key] = value` | Python 没有原型链 setter。但**保留段校验仍然保留**（见 D6 与 `RESERVED_SEGMENTS`） |
| D6 | `delete parent[key]` 对不存在的键是 no-op（`delta/index.ts:389`） | `parent.pop(key, None)` | Python `del d[k]` 会抛 `KeyError`，必须显式对齐 JS 的 no-op |
| D7 | `"p"` 按 `chunkSize = 10_000` 分块插入（`delta/index.ts:354-357`） | 直接 `target[index:index+remove] = items` | 分块是 JS 展开运算符的**栈限制 workaround**，无语义内容 |
| D8 | `Number.isInteger(v)` | `is_int(v)`，**显式排除 `bool`**，并声明为 `TypeGuard[int]` | Python 的 `bool` 是 `int` 子类，而 JS `Number.isInteger(true)` 是 `false`。漏掉会让 `("s", ("xs", True), 1)` 通过校验（上游抛 `UnsafePathError`，`delta/index.ts:283-284`）。`TypeGuard` 让校验与类型收窄一步到位 |
| D9 | `String.prototype.length` / `.slice` 按 **UTF-16 code unit** | 显式 UTF-16 层（`to_units` / `from_units` / `u16_len` / `u16_slice`），内部用 `utf-16-be` 打包成 `bytes` 做比较与查找 | 见下节「UTF-16 决定」 |
| D10 | `overlap` 用 `indexOf` + `slice` 在 UTF-16 码元空间探测 | 在 `utf-16-be` 字节空间用原生 `bytes.find`，并**校验匹配偏移是偶数**（码元对齐） | 字节空间搜索是原生实现；对齐校验防止错开半个码元的假匹配 |
| D11 | `WireOp` / `PathRef` / `encoder` / `decoder` / `assertValidWireOp`（`delta/index.ts:42-67,211-277,558-738`） | **不移植** | path interning 只存在于「tracker 与边界之间」；`pi_durable` 对 `encoder`/`decoder` **零引用**，单进程也不需要 |
| D12 | `diffRevisions`（`delta/diff.ts`，523 行） | **不移植** | `pi_durable` 零引用（`grep -rn diffRevisions packages/durable/` 无命中） |
| D13 | `tracker`（`track` / `Tracker` / `Change` / `Prepared` / `Draft`，`delta/tracker.ts` 2,239 行） | **本批不做**，下一个工作项 | 逐级移植；op 语义层是它的前置 |
| D14 | `Draft` 类型经根入口再导出（`chord/src/index.ts:28`） | 未再导出 | 它属于 tracker，尚未移植 |
| D15 | `JsonValue` 定义在 `chord/src/json.ts` | 暂定义在 `pi_chord.delta.ops` | `json.ts` 的 `copyJson` / `isJsonValue` 是 tracker 的前置，属于下一个工作项；届时可移到 `pi_chord.json` 再从此处再导出 |
| D16 | 公开函数/类型 camelCase（`assertValidOp`、`applyImmutable`、`isBase`） | snake_case（`assert_valid_op`、`apply_immutable`、`is_base`） | PEP 8；类名（`PathError`、`UnsafePathError`）与常量（`RESERVED_SEGMENTS`）保持上游拼写 |
| D17 | 错误消息为英文短串（`"r arity"`、`"path is empty"`、`"m permutation is not a bijection"`） | 保持**逐字相同** | 上游测试用正则匹配消息（如 `delta.test.ts:215` 的 `/bijection/`），保持可对拍 |
| D18 | `apply` 返回值可能是 `undefined`（`target: T \| undefined`） | 允许传 `None`，类型为 `T` | `apply(None, [("r", ...)])` 是上游测试用法（`delta.test.ts:204`），运行时一致 |

## UTF-16 决定（Lead 已定，理由存档）

**结论：采用 UTF-16 code unit 约定。**

`("t", path, count)` 的 `count` 在上游是 **UTF-16 码元数**，依据是
`delta/index.ts:400` 的 `write(current.slice(op[2]))` —— JS 的 `String.prototype.slice`
按码元切。计数来源是同为码元空间的字符串 diff（`tracker.ts:2050-2057`：
`before.length` / `after.slice(...)` / `overlap(...)`），其中
`before.length - shared` 就是 `t` 的计数（`tracker.ts:2056`）。

选 UTF-16 而非 code point 的三条理由：

1. **成本极低且局部化**：只有字符串 diff 的计数与 `t` 的应用需要转换，
   局限在 `pi_chord.delta.textdiff` 的四个函数里。
2. **op 是磁盘格式**：`pi_durable` 把 `prepared.ops` 作为 `document.change` 的
   `kind="delta"` 持久化（上游 `durable/src/session/transaction.ts:1019-1024`），
   约定一旦落盘再改就要迁移所有含非 BMP 的历史数据。
3. **保住差分对拍能力**：UTF-16 约定下，上游测试向量可直接作为 oracle；
   code point 约定会让任何含非 BMP 的用例静默分叉。

**用户已确定「忠实镜像上游」，保留与上游数据互通的能力**，因此取 UTF-16。

### 精确规则

对 `before, after ∈ str`：

- `("a")` 的载荷在两种约定下**永远相同** —— `before` 末尾必在字符边界，
  切片点在两种约定下落在同一位置。（30,000 例 fuzz：`a`-op 差异 = 0。）
- `("t", n)` 是**唯一**会不同的 op，且
  `n_utf16 - n_codepoint` **恰好等于被删前缀中非 BMP 码点的个数**。
- 两种约定**各自都自洽**：用哪个约定发射就用哪个约定应用，往返都正确。
- **跨约定混用是静默错值**：JS 产出的 `("t", 2)` 用码点切片应用，
  `"😀abc"` 会变成 `"bc"`。

### 20 条测试向量的约定分歧

`tests/test_delta_string_ops.py` 的 20 条向量中，**3 条**在两种约定下不同：

| 向量 | before → after | ops_utf16 | ops_codepoint |
|---|---|---|---|
| V5 | `😀abc` → `abc` | `[("t", 2)]` | `[("t", 1)]` |
| V12 | `😀😀` → `😀` | `[("t", 2)]` | `[("t", 1)]` |
| V20 | `👨‍👩‍👧x` → `x` | `[("t", 8)]` | `[("t", 5)]` |

其余 **17 条一致**。三条的共同点是**截断边界落在非 BMP 字符之后**。
`test_convention_divergence_is_pinned` 把「哪 3 条不同」也固化成断言，
这样约定被无意改动时会**精确指出**受影响的向量。

### 该节的两个 Python 陷阱

1. **反解不能用 `surrogatepass`**：
   `data.decode("utf-16-be", errors="surrogatepass")` 会把一个合法代理对拆成
   **两个**孤立代理，`len()` 从 1 变 2。必须手写合并循环（`from_units`）。
2. **未合并的代理对会被规范化**：Python 里 `"\ud83d\ude00"` 是**两个**字符。
   经 `to_units` → `from_units` 后合并成一个 😀。这不是往返恒等，而是刻意的
   规范化（与 JS 语义对齐：JS 里那本来就是一个 emoji）。这是 `from_units`
   唯一不满足恒等的输入类别，已在测试中显式登记。

### 一个需要留意的上游行为（不是缺陷）

`overlap` 是**有界探测**而非「最长重叠」精确算法。上游 `index.ts:98-101` 明确接受：
候选数有上限，放弃时返回 0，那会发射一个 `set` —— **更大，但永远不会错**。
所以返回 0 **不代表**「没有重叠」。`test_overlap_is_a_bounded_probe_not_the_true_longest`
固化了这一点（`overlap("abc" + "x"*10, "abcdef", 65_536) == 0`，尽管 `"abc"` 确实是
后缀/前缀重叠）。

---

# 批次 B —— `json.ts`

上游 `packages/chord/src/json.ts`（135 行）。这是 tracker 放置（placement）校验的前置。

| 上游 | 本包 | 上游文件:行 |
|---|---|---|
| `JsonValue` | `pi_chord.json.JsonValue` | `json.ts:1` |
| `CopyJsonOptions` | `pi_chord.json.CopyJsonOptions` | `json.ts:25-28` |
| `copyJson` | `pi_chord.json.copy_json` | `json.ts:30-80` |
| `isJsonValue` | `pi_chord.json.is_json_value` | `json.ts:88-135` |
| `JsonRepresentation<T>`（条件类型 + 同态映射类型） | `pi_chord.json.JsonRepresentation`（**近似**，见 J7） | `json.ts:5-16` |

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| J1 | `JsonRepresentation<T>` 是条件类型 + 同态映射类型（`IsAny`/`unknown extends`/`infer`/`keyof` 映射） | `JsonRepresentation: TypeAlias = JsonValue` —— **sound 上界，非全等近似** | 见下 J7 的忠实度分析 |
| J2 | `copyJson(value, { omitUndefinedProperties })` | `copy_json(value, options)`；`CopyJsonOptions.omit_undefined_properties` **接受但无效果** | JS 的 `{a: undefined}` 在 JSON 序列化时丢键；Python 的 `None` 是 JSON ``null``，是**必须保留**的合法值，不存在「值为 undefined 的属性」。保留参数只为让调用点可平移 |
| J3 | 用 `Object.getPrototypeOf` / `Reflect.ownKeys` / 属性描述符识别「普通对象/稠密数组/数据属性」 | `type(value) is dict` / `type(value) is list` | Python 的等价物：子类（`OrderedDict`、`defaultdict`、`list` 子类）一律视为非普通容器，与 JS「非 `Object.prototype` 原型」一致。Python 没有访问器属性，所以 `enumerable`/`"value" in descriptor` 那几项检查无对应物 |
| J4 | symbol 键被拒绝 | **非 `str` 键一律拒绝** | JS 对象键是字符串；Python 的 `int`/`tuple` 键都过不了 JSON 对象往返 |
| J5 | 循环检测用 `Set<object>` + `try/finally` 出栈 | `set[int]` 存 `id()`，配 `try/finally` | 同一容器出现在**两个兄弟位置**是合法别名（上游测试也允许）；只有真正的祖先环才拒绝 |
| J6 | `isJsonValue` 与 `copyJson` 各写一遍遍历（`json.ts:35-80` / `:93-135`） | 同样各写一遍 | 保持与上游同构；合并会牺牲「校验不构造」的性质 |

额外补充：`json_equal`（无上游对应物）把上游 `equalTrustedJson`（`tracker.ts:2158`）的语义
具名化，语义就是 Python 的 `==`（`dict` 忽略键序、`list` 按序）。

### J7 —— `JsonRepresentation<T>` 的忠实度（**近似，非全等**）

上游定义（`json.ts:3-16`）是一个 **TS 类型层函数**：

```ts
type IsAny<T> = 0 extends 1 & T ? true : false;
export type JsonRepresentation<T> =
    IsAny<T> extends true                 ? JsonValue                       // any
  : unknown extends T                     ? JsonValue                       // unknown
  : T extends null|boolean|number|string  ? T                               // 基本类型原样
  : T extends readonly (infer I)[]        ? JsonRepresentation<I>[]          // 数组逐元素
  : T extends object                      ? { [K in keyof T]: JsonRepresentation<T[K]> }  // 对象逐键
  : never;                                                                  // 函数/symbol 等
```

它同时用了**条件类型**（`extends ? :`）、**`infer`**、**同态映射类型**
（`{ [K in keyof T]: … }` —— 保留键名与 `?`/`readonly` 修饰符）和 **`any` 探测**。
**Python 的类型系统没有其中任何一项**；PEP 695 的 `type X[T] = …` 需要 3.12+
（本包 `requires-python >= 3.11`），`TypeAliasType` 要引入 `typing_extensions`
（本包零依赖）。因此不可能全等。

**本包的等价物**：`JsonRepresentation: TypeAlias = JsonValue`。

| 维度 | 忠实度 | 说明 |
|---|---|---|
| **soundness（不会把非法判成合法）** | ✅ **完全忠实** | 上游的 `JsonRepresentation<T>` 对**任何** `T` 都恒为某种 `JsonValue`：前两个分支直接就是 `JsonValue`，数组/对象分支是 JSON 可表达的递归结构，`never` 分支比 `JsonValue` 更窄。所以用 `JsonValue` 当上界**永不漏判** |
| **精度（是否收窄）** | ❌ **更宽** | 上游对具体 `T` 会收窄：`JsonRepresentation<number>` 是 `number`、`JsonRepresentation<Item>` 保留 `Item` 的键。Python 侧一律 `JsonValue`，因此会接受上游会拒绝的赋值 |
| **下标** | ❌ **不可下标** | `JsonRepresentation[SomeType]` 抛 `TypeError`（`JsonValue` 是 `Union`，不是泛型）。移植时把 `JsonRepresentation<X>` **直接写成** `JsonRepresentation` |
| **`undefined` 的消除** | ❌ 无对应物 | 上游对可选属性经 `T[K]` 得到 `X \| undefined`，条件类型分布后 `undefined` 落到 `never`，于是可选属性在结构上不再含 `undefined`。Python 的 `NotRequired[...]` 是另一套机制，本包不代劳 |
| **`never` 分支** | ➖ | 上游对函数/symbol 等给 `never`；Python 侧一律 `JsonValue`，不会报错 |

**下游需要更精确时怎么办**：直接用目标类型自己的 JSON 形态（`TypedDict` 镜像、
`dataclasses` 的 dict 形式），不要指望这层变换。`durable` 侧 `usage.ts` 的
`Record<string, JsonRepresentation<Usage>>` 在 Python 里写成
`dict[str, JsonRepresentation]` 即可 —— 见 `tests/test_json_representation.py`
里那个 `UsageState(TypedDict)` 的例子。

### J8 —— `omitUndefinedProperties` 的移植规则（**16 处调用点**）

上游有 **16 处**传这个开关（`grep -rn omitUndefinedProperties packages/ --include=*.ts`
去掉测试后）：`durable/src/harness/{tool,usage,submissions,inbox,generation}.ts`、
`durable/src/codemode/codemode.ts`、`durable/src/session/transaction.ts:83` 等。

JS 的语义是**只丢弃值为 `undefined` 的键**，`null` 照常保留。Python **没有
`undefined`**，`None` 同时承担 `null` 与「缺席」两种含义 —— 所以**不能**把
`omit_undefined_properties=True` 实现成「删掉值为 `None` 的键」：那会把合法的
JSON `null` 一起删掉，是**语义分叉**而不是平移。

**移植规则**：在**构造处**直接不写入该键，而不是指望开关删键。
`pi_durable` 的 `session/transaction.py:630` 已经是这么做的（上游靠
`copyJson(..., {omitUndefinedProperties: true})` 丢掉未定义的 `head`/`byTaskId`，
Python 侧改为按需构造），并有测试
`tests/test_session_transaction_writes.py:115` 断言 `"head" not in first`。

因此本包的 `copy_json` **保持该开关为 no-op**（J2），这是刻意选择，不是漏做。

---

# 批次 B —— `delta/tracker.ts`（tracker）

上游 `packages/chord/src/delta/tracker.ts`（2,239 行）+ `delta/README.md`（193 行）。
本包 `pi_chord/delta/tracker.py`（705 行）。

| 上游 | 本包 | 上游文件:行 |
|---|---|---|
| `Tracker` / `Change` / `Prepared` 接口 | `Tracker` / `Change` / `Prepared` | `tracker.ts:121-141` |
| `track` | `track` | `tracker.ts:321-323` |
| `TrackerImpl.beginChange` | `Tracker.begin_change` | `tracker.ts:235-240` |
| `TrackerImpl.prepareReplace` | `Tracker.prepare_replace` | `tracker.ts:241-252` |
| `TrackerImpl.adopt` | `Tracker.adopt` | `tracker.ts:254-277` |
| `ChangeImpl.prepare` | `Change.prepare` | `tracker.ts:181-201` |
| `ChangeImpl.abort` | `Change.abort` | `tracker.ts:202-213` |
| `PreparedImpl.abort` | `Prepared.abort` | `tracker.ts:161-165` |
| `materializePrepared` / `materializeOperations` | `Change.prepare` 内联 | `tracker.ts:325-349` |
| `createContext` / `createNode` | `_Context` / `Draft` | `tracker.ts:351-380,432-463` |
| `sharedObjectHandler`（Proxy） | `Draft` 的 dunder 方法 | `tracker.ts:382-430` |
| `getProperty` / `setProperty` / `deleteProperty` / `hasProperty` / `ownKeys` | `Draft.__getitem__` / `__setitem__` / `__delitem__` / `__contains__` / `__iter__` | `tracker.ts:524-663` |
| `arrayMutators`（push/pop/shift/unshift/splice/reverse/sort/fill/copyWithin） | `append`/`pop`/`insert`/`remove`/`extend`/`reverse`/`sort`/`clear` | `tracker.ts:1177-1338` |
| `emitChangedValue`（字符串） | `textdiff.emit_string_ops`（复用） | `tracker.ts:2039-2063` |
| `emitObjectOperations` | `_emit_node` 的 dict 分支 | `tracker.ts:1850-1885` |
| `emitArrayOperations` + piece 机制 | `_diff_list`（**简化**） | `tracker.ts:1979-2027`、`:768-1595` |
| `MAX_DELTA_OPERATIONS` 折叠 | `_emit_operations` 折叠 | `tracker.ts:112`、`:1707` |
| `equalTrustedJson` | `pi_chord.json.json_equal` | `tracker.ts:2158` |

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| T1 | 用 `Proxy` 包一个空对象做 draft；全部读写经 handler 转发（`tracker.ts:382-463`） | `Draft` 是一个真实句柄类，内部维护「base 容器 + 首次写入时的浅拷贝 + 子句柄表」 | Python 没有 `Proxy`。对外可观察行为（嵌套读写返回**活**子 draft、放置校验、settle 后不可用）与上游一致 |
| T2 | 不可变性靠「所有权契约 + 不冻结、不防御性拷贝」（`delta/README.md:6-7`） | 同 | 语义一致；Python 侧同样不 freeze（`dict`/`list` 无冻结概念） |
| T3 | **数组**用 piece/permutation 子系统产出最小 `p`/`m`（`tracker.ts:768-1595`，占上游 ~800 行） | 「公共前缀 + 公共后缀」的 splice；**等长且改动区 ≤ 64** 时逐元素递归 | **`delta/README.md:94`「只有结果值有契约意义」+ `:167-168`「批次精确但非规范」**。上游 op 形态**不是**契约。简化后：等长小改动仍产出 `("s", path+[i], v)`，嵌套字符串仍能拿到 `("a", ...)`；纯追加是一个 `p` splice。**代价**：大范围重排会产生一个较大的 splice 而不是最小 `m` |
| T4 | `MAX_SIMPLE_OBJECT_NODES = 128` 的「简单对象」快速路径（`tracker.ts:113`、`:1689`） | **不使用** | 纯性能优化，不改变结果值 |
| T5 | `applyImmutableTrusted` 快速路径（`tracker.ts:348`，对应 `delta/apply-immutable-trusted.ts`） | 直接用 `apply_immutable` | 结果值一致；本包未移植该快速路径（登记在未移植总表） |
| T6 | 持有的子句柄**跟随数组重排**（`delta/README.md:151`） | 父列表结构性变更时，子句柄被**烘焙进父级并标记 detached**；对它的后续写入**抛 `TypeError("Cannot use a detached overlay")`** | 完整复刻「句柄跟随重排」需要 piece 的身份追踪机制。本实现选择**大声失败**而不是静默丢写入 —— 静默丢数据比抛错危险得多。子句柄在 detach **之前**的改动已被烘焙，不会丢 |
| T7 | `Tracker.adopt` / `Change.prepare` 等抛裸 `Error` | 抛 `TrackerError`（`Exception` 子类） | **消息逐字与上游一致**（`"Prepared change is stale"` 等），便于对拍；具名类型方便调用方区分「用法错误」与「值错误」 |
| T8 | draft 写入的 `undefined` 语义：`obj.k = undefined` **删除**键；`push(undefined)` 等**抛错**；`undefined` 嵌套在放置值里抛错（`delta/README.md:115-119`） | **无对应物**：Python 的 `None` 是 JSON `null`，写入即写 `null` | Python 没有 `undefined`。这是两种语言在「缺失」表示上的根本差异，无法平移；已在 `json.py` 的 J2 与 README 中说明 |
| T9 | `prepare()` 之后调用 `clearContext` / `releaseOverlayReferences` 释放 overlay（`tracker.ts:338-339`） | `_Context.overlay_released` 标志；`prepare()` 时置位 | 对齐 `delta/README.md:33`：「After `prepare()`, `abort()`, or another adoption, every use throws」。`prepared.value` 仍然可读（`:134-136`） |
| T10 | `sort` 支持比较器、`fill`、`copyWithin` 等 JS 数组方法 | `sort(key=, reverse=)`；无 `fill`/`copyWithin` | Python 的 `list` 没有后两者；`fill`/`copyWithin` 在 durable 中零使用。`slice` 赋值（`xs[1:3] = [...]`）本包**未实现**，用 `p` 语义的 insert/delete 组合替代 |

## tracker 的完成度：全部

上游 `tracker.ts` 的公开契约 **4 类型 + 5 方法全部实现**，无半成品：

| 契约 | 状态 | 测试 |
|---|---|---|
| `track(initial)` | ✅ | `test_track_starts_at_revision_zero` |
| `Tracker.value` / `.revision` | ✅ | `test_adopt_swaps_the_root_and_advances_revision` |
| `Tracker.begin_change()` | ✅ | 全部 draft 测试 |
| `Tracker.prepare_replace(value)` | ✅ | `test_prepare_replace_*`（3 条） |
| `Tracker.adopt(prepared)` | ✅ | `test_adopt_*`（5 条，含 stale / consumed / aborted / foreign / revision-mismatch） |
| `Change.state`（Draft） | ✅ | 全部 draft 测试 |
| `Change.prepare()` | ✅ | 全部 op 测试 |
| `Change.abort()` | ✅ | `test_change_abort_is_idempotent_and_revokes_the_draft` |
| `Prepared.base/.value/.ops/.base_revision` | ✅ | 全部 |
| `Prepared.abort()` | ✅ | `test_aborted_prepared_cannot_be_adopted` |

**核心不变量**（每一项产生 op 的测试都断言）::

    apply_immutable(prepared.base, prepared.ops) == prepared.value

外加 12 组固定种子的随机变更 fuzz（`test_random_mutations_always_round_trip`），
每组 40 次随机写/删/追加/重排，逐步断言该不变量。

### 折叠 / 合并语义的专门测试

| 语义 | 上游依据 | 本包测试 |
|---|---|---|
| 超预算折叠为根替换 | `tracker.ts:1707` | `test_exceeding_the_operation_budget_folds_to_a_root_replacement` |
| 预算内保持增量 | `tracker.ts:1666` | `test_below_the_budget_keeps_incremental_ops` |
| 写回原值 → 空 ops | `README.md:140` | `test_write_restored_to_original_normalizes_to_empty` |
| 深度相等的容器赋值 → 空 ops | `README.md:140` | `test_deeply_equal_container_assignment_normalizes_to_empty` |
| 相等忽略键序 | `README.md:141` | `test_equality_ignores_key_order` |
| 空 ops 仍推进修订并 stale 竞争者 | `README.md:143` | `test_adopting_a_noop_still_advances_revision_and_stales_competitors` |

> ⚠️ **不过度承诺**：上游 `README.md:140-142` 只说写回原值「**usually** normalize to
> empty」，且「Structural array edits **may** emit a nonempty exact batch even when the
> result is deeply equal」。本实现因为用值比较做差分，实际**总是**归一到空 ops ——
> 这比上游更紧，但**不应被当作契约**：调用方不得依赖「值相等 ⇒ ops 为空」。
> 同理，`prepared.ops` 的具体形态不得被推断（`README.md:94`）。

---

# 批次 B/D —— `output.ts`

上游 `packages/chord/src/output.ts`（383 行）。**全部移植。**

`pi_durable` 的两处消费者：

- `durable/src/truncate.ts:1` 与 `tools/read.ts:2` 取 `utf8ByteLength` / `characterEnd`
  （这两个函数在 task-15 已落地，**本任务未改动它们**，有专门的回归测试
  `tests/test_output_regression.py` 22 条钉住签名与语义）。
- `durable/src/harness/tool.ts:4` 取 `boundOutput` / `OutputBuffer` / `OutputLimits` /
  `Progress` / `PROGRESS_BYTES_PER_SECOND`（本任务新增）。

| 上游 | 本包 | 上游文件:行 |
|---|---|---|
| `OutputLimits` / `BoundedOutput` / `OutputSlice` / `OutputSkip` | 同名 frozen dataclass | `output.ts:7-31` |
| `sanitizeOutput` | `sanitize_output` | `output.ts:39-42` |
| `boundOutput` | `bound_output` | `output.ts:44-59` |
| `headRange` / `tailRange` | `_head_range` / `_tail_range` | `output.ts:61-99` |
| `characterEnd` | `character_end` | `output.ts:101-106` |
| `characterStart`（上游未导出） | `_character_start`（同样未导出） | `output.ts:108-113` |
| `lineCount` | `line_count`（公开；上游未导出但被测试需要） | `output.ts:115-120` |
| `OutputBuffer` | `OutputBuffer` | `output.ts:122-241` |
| `tailMargin` / `lines` | `_tail_margin` / `_lines` | `output.ts:243-266` |
| `utf8ByteLength` | `utf8_byte_length` | `output.ts:275-294` |
| `PROGRESS_BYTES_PER_SECOND` | 同名常量 | `output.ts:302-303` |
| `Progress` | `Progress` | `output.ts:305-383` |

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| O1 | `characterEnd(bytes: Uint8Array, index: number)` | `character_end(data: bytes \| bytearray, index: int) -> int` | `Uint8Array` 的对应物是可索引的只读字节序列 |
| O2 | `bytes[index] ?? 0`（越界读得 0，故 `index === length` 不算续字节） | `_byte_at` 显式做边界检查返回 0 | 行为一致；Python 越界索引会抛 `IndexError`，必须显式对齐 |
| O3 | `utf8ByteLength` 逐 UTF-16 码元计数：`<=0x7f`→1、`<=0x7ff`→2、代理对→4、其余→3 | 逐**码点**计数：`<=0x7f`→1、`<=0x7ff`→2、`<=0xFFFF`→3（含孤立代理）、`>0xFFFF`→4；并**先合并相邻的高+低代理** | Python `str` 是码点序列，非 BMP 是单个码点（JS 里是一个代理对），所以 `>0xFFFF` 这一支是 Python 侧必须补的。先合并代理对是为了让未合并的 `"\ud83d\ude00"` 也得到 4 而非 6 —— 与 JS 逐码元语义完全一致 |
| O4 | 使用运行时的 `Buffer.byteLength(content, "utf8")` 快路径（`output.ts:272,277`） | 无快路径，纯 Python 逐字符 | Python 里不能直接 `len(s.encode("utf-8"))`：**孤立代理会抛 `UnicodeEncodeError`**，而上游规定孤立代理按 U+FFFD 计 3 字节（`output.ts:275`）。这是本函数存在的主要理由，已单测 |
| O5 | `TextEncoder` / `TextDecoder` 是运行时全局；编码时孤立代理变 U+FFFD | `_encode_utf8` / `_decode_utf8` 手写 | 直接 `text.encode("utf-8")` 遇孤立代理**抛异常**；`errors="surrogatepass"` 虽也是 3 字节，但产出 WTF-8 (`ED A0 80`)，而 `TextEncoder` 产出 U+FFFD (`EF BF BD`) —— **长度相同、字节不同**，解码回文本能看出差别，所以必须真的替换。解码用 `errors="replace"` 对齐 `TextDecoder` |
| O6 | `new TextDecoder("utf-8", { ignoreBOM: true })`（切片解码保留 U+FEFF） | Python 的 `utf-8` 编解码器本来就不吃 BOM | `utf-8-sig` 才会剥 BOM；语义天然一致 |
| O7 | `OutputBuffer` 的 `#decoder` 是 `TextDecoder` 的流式模式（`decode(chunk, {stream:true})` / `decode()` 冲刷） | `codecs.getincrementaldecoder("utf-8")("replace")`：`decode(chunk, False)` / `decode(b"", True)` | 行为逐项验证过：半截 emoji 缓冲为 `""`、冲刷得 U+FFFD、冲刷后可继续复用、非法字节得 U+FFFD、BOM 保留 |
| O8 | `OutputLimits` / `BoundedOutput` / `OutputSlice` / `OutputSkip` 是结构化类型（TS type） | 同名 **frozen dataclass** | Python 里 frozen dataclass 是结构化只读值的自然对应物；`bound_output` 在「什么都没丢」时仍返回**原字符串对象**（`output.ts:54`），这一点被测试用 `is` 钉住 |
| O9 | `Progress` 用 `setTimeout` / `queueMicrotask` / `Date.now()` / `Promise.withResolvers` | 用 **asyncio**：`loop.call_later` / `loop.create_task` / `loop.create_future`，时间用 `time.monotonic()` | Python 没有微任务队列与隐式定时器。**`mark()` / `mark_and_wait()` 需要运行中的事件循环**，否则抛 `RuntimeError`。`durable.harness.tool` 的调用点都在 async 上下文里，不受影响。用单调时钟而非墙钟是**更好**的语义（不受系统时间调整影响），且 `_next_at` 只做差值比较 |
| O10 | `Progress.stop()` 返回 `PromiseWithResolvers<void>[]` | 返回 `list[asyncio.Future[None]]` | `Promise.withResolvers` 的对应物是「future + 外部 set_result」；Python 侧把 future 直接交给调用方落定 |
| O11 | `Progress.#write: () => Promise<number>` | `Callable[[], int \| Awaitable[int]]` | Python 侧同步与异步写都接受（上游只接受 Promise，但同步返回值在 Python 里很自然） |
| O12 | `lineCount` 是模块私有 | `line_count` 公开在 `__all__` 里 | `durable` 侧的截断逻辑需要同口径的行数；上游把它私有只是内部约定，公开不改变语义 |

> ⚠️ **下游保护**：`character_end` 与 `utf8_byte_length` 的**签名与语义在本任务中零改动**。
> `tests/test_output_regression.py` 用 `inspect.signature` 钉住参数名/默认值/返回类型，
> 并复算全部已知语义（含 task-15 修过的「BMP 多算 1 字节」缺陷），
> 任何改动都会在破坏 `pi-durable` 之前失败。

---

# 批次 B/D —— `state.ts`

上游 `packages/chord/src/state.ts`（423 行）。**全部移植。**

`pi_durable` 的消费者：`harness/view.py`（`replicatedState` + `AttachedReplicatedState`）、
`session/observation.py`（`ReplicatedStateSource*`）、`harness/task_graph.py`、
`session/session.py`、`harness/harness.py`。

| 上游 | 本包 | 上游文件:行 |
|---|---|---|
| `ReplicatedStateDelivery` | 同名 frozen dataclass | `state.ts:9-12` |
| `ReplicatedState` | `ReplicatedState`（ABC） | `state.ts:14-28` |
| `MutableReplicatedState` | `MutableReplicatedState`（ABC） | `state.ts:30-42` |
| `ReplicatedStateSourceFrame` | 同名 frozen dataclass | `state.ts:44-53` |
| `ReplicatedStateSourceAttachment` | 同名 ABC | `state.ts:55-66` |
| `ReplicatedStateSource` | 同名 **Protocol** | `state.ts:68-78` |
| `ReplicatedStateSourceOptions` | 同名 frozen dataclass | `state.ts:80-83` |
| `AttachedReplicatedState` | 同名 ABC | `state.ts:85-90` |
| `StateSubscriber` | `_StateSubscriber` | `state.ts:101-171` |
| `ReplicatedStatePublisher` | `_ReplicatedStatePublisher` | `state.ts:179-239` |
| `MutableReplicatedStateImpl` | `_MutableReplicatedState` | `state.ts:241-290` |
| `AttachedReplicatedStateImpl` | `_AttachedReplicatedState` | `state.ts:292-360` |
| `attachReplicatedStateSource` | `_attach_source` | `state.ts:362-380` |
| `assertCursor` / `isPromiseLike` / `toError` | `_assert_cursor` / `inspect.isawaitable` / `_to_error` | `state.ts:382-402` |
| `replicatedState`（两个重载） | `replicated_state`（运行时按 `attach` 是否可调用分派） | `state.ts:404-423` |

## 有意偏离上游

| # | 上游 | 本包 | 原因 / 证据 |
|---|---|---|---|
| S1 | `interface` / `implements`（结构化类型） | `abc.ABC` + `@abstractmethod`；`ReplicatedStateSource` 用 `Protocol` | Python 里显式继承更利于 mypy strict；`ReplicatedStateSource` 保持 Protocol 是因为上游按**结构**（有可调用的 `attach`）判定（`state.ts:421-423`） |
| S2 | 监听器可返回 Promise；`drain` 在其落定后 `#resume()`（`state.ts:133-141`） | 监听器返回 **awaitable** 时，用**运行中的事件循环** `create_task`，完成回调里 `_resume()` | Python 没有「普通函数返回挂起值」。**无事件循环时**：关闭协程并把它当作监听器失败上报（不静默丢弃） |
| S3 | `reportErrorAsync` = `queueMicrotask(() => { throw error })`（`state.ts:394-398`） | 有运行中的循环 → `loop.call_soon(reraise)`（由循环异常处理器上报，不打断投递）；无循环 → 写 `sys.stderr` | Python 没有微任务队列。两个分支都保持「让错误可见但不打断调用方」 |
| S4 | `toError` 把非 Error 包成 `Error(String(x))` | `_to_error` 把非 `BaseException` 包成 `Exception(str(x))` | 语言等价物 |
| S5 | `AggregateError`（`state.ts:347`、`:376`） | `BaseExceptionGroup` | Python 3.11+ 的对应物；用 `BaseExceptionGroup` 而非 `ExceptionGroup`，因为 dispose 可能抛 `BaseException` |
| S6 | `assertCursor` 用 `Number.isSafeInteger` | `_assert_cursor` 显式检查 `int`、排除 `bool`、并限定 `abs(cursor) <= 2**53 - 1` | Python 的 `int` 无界，必须补上 safe-integer 上界；`bool` 是 `int` 子类（与 delta 的 `is_int` 同一陷阱） |
| S7 | `ReplicatedStateDelivery.sequence` 是 **publisher 的本地序号** | 同 | 注意它**不是**源游标：`AttachedReplicatedState` 的水合投递序号是 0，源游标只用于断层检测（`state.ts:202-206` vs `:326-339`）。有专门的测试钉住这一点 |
| S8 | `change(context, mutate)` 的 `mutate` 返回 `unknown`，用 `isPromiseLike` 检查（`state.ts:262-266`） | 返回类型是 `Any`，用 `inspect.isawaitable` 检查 | 与上游同构 |
| S9 | `Promise.withResolvers` 之类在 `subscribe` 里返回 `() => void` | 返回 `Callable[[], None]` | 语言等价物 |
| S10 | 取消监听器（Promise 被 reject 为 `CancelledError`）会被当作失败上报 | `asyncio.CancelledError` **不再上报** | asyncio 收尾时的取消不是监听器失败；吞掉它会让每个提前返回的调用方都收到假错误 |

## `state.ts` 的完成度：全部

| 契约 | 状态 | 测试 |
|---|---|---|
| `replicatedState(initial)` → 可变权威状态 | ✅ | `test_state.py` 前半 |
| `replicatedState(source, options)` → 附着只发布状态 | ✅ | `test_state.py` 后半 |
| `ReplicatedState.value` / `.subscribe()` | ✅ | 全部 |
| `MutableReplicatedState.change()` | ✅ | 含 no-op 不发布、回调失败不采纳、重入拒绝、异步回调拒绝 |
| `MutableReplicatedState.replace()` | ✅ | 含「深相等则不发布」 |
| `AttachedReplicatedState.dispose()` | ✅ | 幂等、最后值仍可读 |
| 水合（`hydrate`）与更新（`update`）的序号语义 | ✅ | `test_subscribe_hydrates_immediately_with_the_current_value` 等 |
| 100 条积压溢出只保留最新、**序号会跳跃** | ✅ | `test_backlog_overflow_keeps_the_newest_and_skips_sequences`（卡住异步监听器制造积压） |
| 失败隔离：一个监听器抛错不影响其他订阅者 | ✅ | `test_listener_failure_is_isolated` |
| 监听器串行化（异步监听器未落定前不投递下一条） | ✅ | `test_async_listener_is_awaited_before_the_next_delivery` |
| 源游标断层 → 上报 + dispose + 不发布断层帧 | ✅ | `test_cursor_gap_is_reported_and_disposes` |
| 附着失败 → dispose；dispose 也失败 → 异常组 | ✅ | `test_attach_failure_disposes_the_attachment`、`test_dispose_failure_during_fail_is_reported_as_a_group` |
| 游标必须是 safe integer | ✅ | `test_bad_snapshot_cursor_is_rejected` |

---

# 未移植总表

| 上游文件 | 状态 | 理由 |
|---|---|---|
| `context/index.ts`（133 行） | ✅ 已移植 | 批次 A |
| `json.ts`（135 行） | ✅ 已移植 | 批次 B（`JsonRepresentation` 除外，纯类型层，见 J1） |
| `delta/index.ts`（op 层 + apply，738 行） | ✅ 已移植 | 批次 B |
| `delta/tracker.ts`（2,239 行） | ✅ 已移植 | 批次 B；**数组 piece/permutation 子系统有意简化**，见 T3 |
| `delta/diff.ts`（523 行） | ❌ 不移植 | `pi_durable` 零引用（`grep -rn diffRevisions packages/durable/` 无命中） |
| `delta/apply-immutable-trusted.ts`（128 行） | ❌ 不移植 | 上游的快速路径；本包直接用 `apply_immutable`，结果值一致（见 T5） |
| `delta/draft.ts`（10 行） | 🟡 以类代替 | 上游是纯类型（`Draft<T>`）；本包 `Draft` 是真实句柄类（见 T1） |
| `delta/revision-validator.ts`（75 行） | ❌ 不移植 | 在 chord 内无 importer，疑似死代码 |
| `output.ts`（383 行） | ✅ 已移植 | 含 `OutputBuffer` / `Progress` / `boundOutput` / `sanitizeOutput` |
| `state.ts`（423 行，`replicatedState`） | ✅ 已移植 | 两种形态（可变权威 / 附着只发布）全部落地 |
| `errors.ts`（76 行） | 🟡 部分 | `PathError` / `UnsafePathError` 已随 delta 落地；`ChordError` 家族属宿主半边，不移植 |
| 宿主半边：`host.ts`、`contract.ts`、`object.ts`、`cell.ts`、`connection.ts`、`link.ts`、`reload.ts`、`remote.ts`、`wire.ts`、`manager.ts`、`schema.ts`、`cluster.ts`、`publish.ts`、`registry.ts`、`loader.ts`、`declare.ts`、`types.ts`、`node.ts`、`websocket.ts` | ❌ **不移植** | v1.3.0 全仓库零消费：`createHost`/`defineFacet`/`defineRegistry`/`ipcLink`/`webSocketLink`/`memoryLinkPair`/`acceptWebSockets`/`hostNamed`/`local(`/`method(` 在 `packages/` 下（除 chord 自身）使用数均为 0 |

## 本包仍待做

**`pi_durable` 的 chord 前置已全部就绪** —— 下列各项都**不阻塞任何东西**，
是按需才做的可选项。

| # | 项 | 阻塞什么 | 预估 |
|---|---|---|---|
| 1 | `delta/diff.py`（`diffRevisions`） | **不阻塞** —— `pi_durable` 零引用。除非将来需要「两棵树求差」的 API | S（上游 523 行，接口面小） |
| 2 | 数组 piece/permutation 子系统（T3） | **不阻塞** —— op 形态非契约。仅当需要 op 级与上游对拍、或大数组重排的性能成为问题时 | L（上游 ~800 行，piece 树 + 排序令牌 + 稠密区折叠） |
| 3 | `delta/apply-immutable-trusted.ts` 快速路径（T5） | **不阻塞** —— 纯性能优化，结果值一致 | S（上游 128 行） |
| 4 | `Progress` 的墙钟语义 | **不阻塞** —— 本包用单调时钟，语义更好 | — |
| 5 | 宿主半边 | **不阻塞** —— 无人消费 | — |

---

# 证据索引

## context

| 主题 | 位置（上游） |
|---|---|
| `ContextKey` / `Context` 接口 | `chord/src/context/index.ts:1-13` |
| abort 信号键 | `chord/src/context/index.ts:15-17` |
| `BaseContext.abortSignal` | `chord/src/context/index.ts:19-26` |
| 空上下文与两个预置上下文 | `chord/src/context/index.ts:28-43,67-68` |
| 值遮蔽与查找 | `chord/src/context/index.ts:45-65` |
| `createContextKey` | `chord/src/context/index.ts:70-72` |
| `withContextValue` | `chord/src/context/index.ts:74-77` |
| `withAbortSignal` / `withoutAbortSignal` | `chord/src/context/index.ts:79-92` |
| `withCancel` | `chord/src/context/index.ts:94-104` |
| `awaitWithContext` / `abortError` | `chord/src/context/index.ts:106-133` |
| 根入口只再导出两个类型 | `chord/src/index.ts:3` |

## delta

| 主题 | 位置（上游） |
|---|---|
| `Seg` / `Path` / `NonEmptyPath` / `PathRef` | `chord/src/delta/index.ts:14-18` |
| op 是内存/线上/磁盘同一形态 | `chord/src/delta/index.ts:19-27` |
| `Op` 词表（7 动词） | `chord/src/delta/index.ts:29-38` |
| `WireOp` | `chord/src/delta/index.ts:42-67` |
| `isReplace` / `isBase` | `chord/src/delta/index.ts:70-76` |
| `overlap`（含有界探测的说明） | `chord/src/delta/index.ts:87-112` |
| `RESERVED_SEGMENTS` | `chord/src/delta/index.ts:131` |
| `UnsafePathError` | `chord/src/delta/index.ts:133-149` |
| `assertValidOp` | `chord/src/delta/index.ts:152-191` |
| `assertPathArg` | `chord/src/delta/index.ts:193-197` |
| `assertPermutation` | `chord/src/delta/index.ts:199-208` |
| `assertValidWireOp` | `chord/src/delta/index.ts:211-277` |
| `assertSafePath` | `chord/src/delta/index.ts:279-287` |
| `assertIndexInRange`（含保 `JsonValue` 与防 DoS 的理由） | `chord/src/delta/index.ts:289-306` |
| `PathError` | `chord/src/delta/index.ts:310-317` |
| `apply` / `applyOps` | `chord/src/delta/index.ts:326-406` |
| `r` 的 adopt 语义与扇出别名警告 | `chord/src/delta/index.ts:336-342` |
| `applyImmutable` | `chord/src/delta/index.ts:408-411` |
| `applyImmutableBatches`（不暴露中间修订） | `chord/src/delta/index.ts:413-441` |
| `appendCopy` | `chord/src/delta/index.ts:443-473` |
| `copyContainers` | `chord/src/delta/index.ts:475-521` |
| `resolveValue` / `resolve` | `chord/src/delta/index.ts:523-540` |
| 字符串 diff 发射（UTF-16 计数来源） | `chord/src/delta/tracker.ts:2039-2063` |
| `emitString`（同一逻辑第二处） | `chord/src/delta/diff.ts:63-76` |
| `DEFAULT_OVERLAP_SCAN = 65_536` | `chord/src/delta/diff.ts:4` |
| `MAX_DELTA_OPERATIONS = 4096` / `MAX_SIMPLE_OBJECT_NODES = 128` | `chord/src/delta/tracker.ts:112-113` |
| 折叠阈值使用点 | `chord/src/delta/tracker.ts:1654-1710,1869-2016` |
| `adopt` 状态机与身份比较 | `chord/src/delta/tracker.ts:254-277` |
| delta 契约总述 | `chord/src/delta/README.md`（193 行） |
| 「只有结果值有契约意义」 | `chord/src/delta/README.md:94` |
| 「批次精确但非规范」 | `chord/src/delta/README.md:167-168` |
| op 词表说明 | `chord/src/delta/README.md:157-165` |
| 无 freeze、静默损坏的警告 | `chord/src/delta/README.md:6-7,186` |

## output

| 主题 | 位置（上游） |
|---|---|
| `characterEnd` | `chord/src/output.ts:101-106` |
| `characterStart` | `chord/src/output.ts:108-113` |
| `lineCount` | `chord/src/output.ts:115-120` |
| `utf8ByteLength` | `chord/src/output.ts:275-294` |
| 孤立代理按 U+FFFD 计 3 字节 | `chord/src/output.ts:275` |
| `Buffer.byteLength` 快路径 | `chord/src/output.ts:272,277` |
| `OutputLimits` / `BoundedOutput` / `OutputSlice` / `OutputSkip` | `chord/src/output.ts:7-31` |
| `sanitizeOutput` 的正则（tab/LF 保留，CR 去掉） | `chord/src/output.ts:34,39-42` |
| `boundOutput`（未丢内容时返回原字符串） | `chord/src/output.ts:44-59`（`:54`） |
| `headRange` / `tailRange`（含行对齐取舍） | `chord/src/output.ts:61-99` |
| `OutputBuffer`（head 满即停 / tail 丢首块 / BOM / skip） | `chord/src/output.ts:122-241` |
| `storedBytes` 上界 = 上限 + 一个 chunk | `chord/src/output.ts:149-150` |
| `tailMargin` | `chord/src/output.ts:243-261` |
| `PROGRESS_BYTES_PER_SECOND` | `chord/src/output.ts:302-303` |
| `Progress`（首次即提交 / 按写入量推迟 / 至多一次在途） | `chord/src/output.ts:305-383` |
| 下游消费者 | `durable/src/tools/read.ts:2`、`durable/src/truncate.ts:1`、`durable/src/harness/tool.ts:4` |

## 证据索引 —— state

| 主题 | 位置（上游） |
|---|---|
| 投递语义总述（串行化 / 100 条积压 / 失败隔离） | `chord/src/state.ts:20-27` |
| `ReplicatedStateDelivery` | `chord/src/state.ts:9-12` |
| `ReplicatedState` / `MutableReplicatedState` | `chord/src/state.ts:14-42` |
| `ReplicatedStateSourceFrame`（cursor 必须 `snapshot.cursor + 1`） | `chord/src/state.ts:44-53` |
| `ReplicatedStateSourceAttachment`（`activate` 单次、按序、含重入） | `chord/src/state.ts:55-66` |
| `ReplicatedStateSource`（`attach` 必须同步原子） | `chord/src/state.ts:68-78` |
| `ReplicatedStateSourceOptions` | `chord/src/state.ts:80-83` |
| `AttachedReplicatedState`（dispose 幂等、最后值可读） | `chord/src/state.ts:85-90` |
| `StateSubscriber.push` 的 100 条溢出与保留水合 | `chord/src/state.ts:115-124` |
| `StateSubscriber.drain` 的异步续跑 | `chord/src/state.ts:126-148` |
| `StateSubscriber.#report` 的双层兜底 | `chord/src/state.ts:164-170` |
| `ReplicatedStatePublisher.subscribe`（水合用 `BACKGROUND_CONTEXT`） | `chord/src/state.ts:201-212` |
| `ReplicatedStatePublisher.publish`（投递循环 + `delivering` 防重入） | `chord/src/state.ts:214-238` |
| `MutableReplicatedStateImpl.change`（重入拒绝 / 同步回调 / no-op 不发布） | `chord/src/state.ts:255-278` |
| `MutableReplicatedStateImpl.replace` | `chord/src/state.ts:280-285` |
| `AttachedReplicatedStateImpl`（游标断层 → fail → dispose） | `chord/src/state.ts:292-360` |
| `attachReplicatedStateSource`（activate 失败也 dispose） | `chord/src/state.ts:362-380` |
| `assertCursor`（safe integer） | `chord/src/state.ts:382-385` |
| `replicatedState` 重载与源判定 | `chord/src/state.ts:404-423` |
| 下游消费者 | `durable/src/harness/view.ts:1,118`、`durable/src/session/observation.ts:4-9`、`durable/src/harness/task-graph.ts:1`、`durable/src/session/session.ts:1` |

## 本包测试与上游的对应

| 本包测试 | 上游来源 |
|---|---|
| `test_delta_string_ops.py::test_string_ops_utf16`（20 条） | 无直接上游来源 —— **上游对非 BMP 零覆盖**；V16 对齐 `delta.test.ts:171-177` |
| `test_delta_string_ops.py::test_overlap_matches_upstream_vectors` | `delta.test.ts:181-182` |
| `test_delta_apply.py::test_append_then_rolling_window` | `delta.test.ts:163-177` |
| `test_delta_apply.py::test_mutable_and_immutable_agree_and_share` | `delta.test.ts:188-198` |
| `test_delta_apply.py::test_root_replacement_root_splice_and_permutation` | `delta.test.ts:201-208` |
| `test_delta_apply.py::test_touched_containers_copied_once_payload_ownership` | `delta-apply-immutable.test.ts:24-82` |
| `test_delta_apply.py::test_root_replacement_payload_protected_from_later_edits` | `delta-apply-immutable.test.ts:84-95` |
| `test_delta_apply.py::test_array_root_ops` | `delta-apply-immutable.test.ts:97-104` |
| `test_delta_apply.py::test_immutable_batches_equal_sequential_and_flattened` | `delta-apply-immutable.test.ts:107-140` |
| `test_delta_apply.py` 的校验/错误向量 | `delta.test.ts:210-229,466-467` |
| `test_output.py` | `output.ts:101-113,275-294`（上游无独立单测对拍，按语义构造） |
| `test_tracker.py` | `test/delta-tracker/tracker.test.ts`（1,507 行）逐条对齐；上游无对应物的部分按 `delta/README.md:124-168` 构造 |
| `test_json.py` | `json.ts:30-135`（上游 `test/json.test.ts` 仅 65 行，覆盖不足，故按语义逐条构造） |
| `test_json_representation.py`（16 条） | `json.ts:5-16` —— 纯类型，上游无运行时测试可对拍。断言**可导入**（两个落点）、**与 `JsonValue` 同一**、**不可下标**、**近似方向是「更宽」**，把 J7 的忠实度缺口全部钉住 |

## 证据索引 —— delta/tracker

| 主题 | 位置（上游） |
|---|---|
| `Prepared` / `Change` / `Tracker` 接口 | `chord/src/delta/tracker.ts:121-141` |
| `PreparedImpl`（abort） | `chord/src/delta/tracker.ts:143-165` |
| `ChangeImpl`（prepare / abort） | `chord/src/delta/tracker.ts:166-213` |
| `TrackerImpl`（beginChange / prepareReplace / adopt） | `chord/src/delta/tracker.ts:214-320` |
| `track` | `chord/src/delta/tracker.ts:321-323` |
| `materializePrepared` | `chord/src/delta/tracker.ts:325-341` |
| `materializeOperations`（含 `applyImmutableTrusted` 分派） | `chord/src/delta/tracker.ts:343-349` |
| `createContext` | `chord/src/delta/tracker.ts:351-380` |
| Proxy handler（`get`/`set`/`deleteProperty`/`has`/`ownKeys`） | `chord/src/delta/tracker.ts:382-430` |
| `createNode` | `chord/src/delta/tracker.ts:432-463` |
| `getProperty` / `getArrayIndex` | `chord/src/delta/tracker.ts:524-562` |
| `setProperty` / `deleteProperty` / `hasProperty` / `ownKeys` | `chord/src/delta/tracker.ts:563-663` |
| `isSettledContext` / `assertReadable` / `assertWritable` | `chord/src/delta/tracker.ts:506-522` |
| 数组变更方法（`arrayMutators`） | `chord/src/delta/tracker.ts:1177-1338` |
| piece 树 / 排序令牌 / 稠密区折叠 | `chord/src/delta/tracker.ts:768-1595`、`:1716-1837` |
| `emitOperations`（含预算折叠） | `chord/src/delta/tracker.ts:1602-1715` |
| `emitChangedValue`（字符串 `a`/`t`） | `chord/src/delta/tracker.ts:2039-2063` |
| `equalTrustedJson` | `chord/src/delta/tracker.ts:2158-2174` |
| `abortContext` / `clearContext` | `chord/src/delta/tracker.ts:2175-2236` |
| 生命周期总述 | `chord/src/delta/README.md:124-136` |
| 只有结果值有契约意义 | `chord/src/delta/README.md:94` |
| 批次精确但非规范 | `chord/src/delta/README.md:167-168` |
| no-op 归一（上游亦不保证） | `chord/src/delta/README.md:138-143` |
| 变更权限表（谁能被改） | `chord/src/delta/README.md:28-40` |
| 放置拷贝与校验 | `chord/src/delta/README.md:96-122` |
| 持有的句柄跟随重排 | `chord/src/delta/README.md:151` |
| 性能边界（tracker 代理读取代价） | `chord/src/delta/README.md:188-191` |

## 证据索引 —— json

| 主题 | 位置（上游） |
|---|---|
| `JsonValue` | `chord/src/json.ts:1` |
| `JsonRepresentation` | `chord/src/json.ts:5-16` |
| `CopyJsonOptions` | `chord/src/json.ts:25-28` |
| `copyJson` | `chord/src/json.ts:30-80` |
| 非有限数拒绝 | `chord/src/json.ts:37-40` |
| 非 JSON 值拒绝 | `chord/src/json.ts:41-42` |
| 循环拒绝 | `chord/src/json.ts:43-45` |
| 稠密普通数组要求 | `chord/src/json.ts:47-51` |
| 普通对象 / null 原型要求 | `chord/src/json.ts:62-65` |
| symbol 键拒绝 | `chord/src/json.ts:68` |
| `isJsonValue` | `chord/src/json.ts:88-135` |
| 下游消费者 | `durable/src/documents.ts:1`、`durable/src/entries.ts:1`、`durable/src/types.ts:1` |

## 本包测试与上游的对应 —— state / output

| 本包测试 | 上游来源 |
|---|---|
| `test_state.py`（25 条） | `test/replicated-state.test.ts`（351 行）、`test/replicated-state-delivery.test.ts`（248 行）、`test/state.test.ts`、`test/state-value.test.ts`；语义依据 `state.ts:20-27` 的投递契约 |
| `test_output_buffer.py`（44 条） | `test/output.test.ts`（311 行）、`test/output-skip.test.ts`（143 行） |
| `test_output_regression.py`（22 条） | **回归护栏**，无直接上游来源：钉住 `character_end` / `utf8_byte_length` 的签名与语义（`output.ts:101-106`、`:275-294`），保护 `pi-durable` 的既有用法 |
| `test_output.py`（17 条） | `output.ts:101-113,275-294`（task-15 的原始覆盖） |

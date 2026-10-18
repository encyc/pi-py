# 08 — chord delta 子层规格与测试向量（pi-durable 批次 B 前置）

> 任务：`task-13` ｜ 调研员：`chord-scout` ｜ 日期：2026-10-18
> 上游源码（只读）：`/tmp/pi-up/v1.3.0/packages/chord/src/delta/`
> 本规格所有语义均来自实读源码，附 `文件:行号`。**本文件不写 `packages/`，实现由 `impl-durable-a` 在 task-8 完成。**

---

## 0. 摘要（给实现者的 5 条要点）

1. **op 词表 7 个动词**：`r` / `s` / `d` / `a` / `t` / `p` / `m`。**只有 `r` 能替换整个值**；`s`/`d`/`a`/`t` 的路径**类型上禁止为空**；`p`/`m` 允许空路径（此时根自身必须是数组）。词表是**内存 / 线上 / 磁盘同一形态**（`delta/index.ts:19-20`）。

2. **UTF-16 陷阱的精确定位**：只有 **`["t", count]` 的 `count`** 是按 **UTF-16 code unit** 计数的（`delta/index.ts:400` 的 `String.prototype.slice`）。`["a", text]` 携带的是**文本本身**，在两种约定下**永远相同**（30,000 例 fuzz：`a`-op 差异 = 0）。所以需要 UTF-16 的代码面极小：**字符串 diff 的计数 + `t` 的应用**。

3. **两条路都自洽，但必须选一条**：code-point 约定与 UTF-16 约定**各自都能正确往返**（30,000 例 fuzz：两种约定 round-trip 失败均为 0）。差异只在**跨约定混用**时出现，且是**静默错误**：把 JS 产出的 `["t",2]` 用 Python 码点切片应用，`"😀abc"` 会变成 `"bc"` 而不是 `"abc"`。**推荐选 UTF-16**（§2.5）。

4. **契约边界（§3）**：契约只约束 **(a) 结果值**、**(b) op 元组的词表/元数**、**(c) 校验拒绝集**、**(d) 所有权与结构共享的可观察行为**。**不约束**「给定 (before, after) 该产出哪一串 op」——`delta/README.md:94` 明说 "Don't infer meaning from op shape. Only the resulting value is contractual."，`:167` 说 "Batches are exact but not canonical."。**这是实现自由度最大的一块，能省掉大量逐 op 逆向。**

5. **上游对非 BMP 字符零测试**：`grep -rn 'emoji\|surrogate\|code unit\|utf16\|UTF-16' delta*.test.ts delta-tracker/*.ts state*.test.ts` → **0 hits**。即 UTF-16 行为是 JS 语义的**副产品**，不是被测试固化的契约。pi-py 有选择自由，但**必须一次性选定并全局一致**。

---

## 1. op 词表与精确语义

### 1.1 类型定义（实读）

`delta/index.ts:24-38`：

```ts
export type Seg = string | number;
export type Path = readonly Seg[];
export type NonEmptyPath = readonly [Seg, ...Seg[]];

export type Op =
	| readonly ["r", JsonValue]
	| readonly ["s", NonEmptyPath, JsonValue]
	| readonly ["d", NonEmptyPath]
	| readonly ["a", NonEmptyPath, string]
	| readonly ["t", NonEmptyPath, number]
	| readonly ["p", Path, number, number, JsonValue[]]
	/** Reorder an array in place: `new[i] = old[permutation[i]]`. */
	| readonly ["m", Path, number[]];
```

类型注释的关键三条（`delta/index.ts:19-27`，原文）：

- `r` 是**唯一**替换整个值的 op。
- `s`/`d`/`a`/`t` **不能指向根**——类型禁止；`p`/`m` 可以，因为被追踪的值本身可能是数组。
- **操作形态不是规范的**（"Operation shape is not canonical"）：一个数组既可以用替换清空，也可以用根 splice 清空。

### 1.2 逐 op 语义

> 应用逻辑集中在 `applyOps()`（`delta/index.ts:330-406`），校验在 `assertValidOp()`（`:152-191`）。

#### `["r", value]`

| 项 | 规格 |
|---|---|
| 元数 | 2（`:156`） |
| 语义 | `root = op[1]`（`:343`） |
| 所有权 | **adopt，不拷贝**（`:336-342` 注释）。消费者拥有这个 batch |
| 别名警告 | 同一 batch 扇出给多个进程内消费者会让副本互相别名（`:337-342`）。扇出点必须拷贝 batch，或让每个消费者自行 `decode` |
| Python 等价 | `root = op[1]`（直接引用，不深拷贝） |

#### `["s", path, value]`

| 项 | 规格 |
|---|---|
| 元数 | 3；`path` **必须非空**（`:160` `assertPathArg(op[1], true)`） |
| 语义 | 解析父容器，写入 `path[-1]` 键（`:377-384`） |
| 数组父容器 | `assertIndexInRange(parent, key)`：**`index > len` 抛 `UnsafePathError`**；`index == len` **允许**（等价 append） |
| 写入方式 | `Object.defineProperty`（`:378`），**不是赋值**——避免原型链上的 setter 被执行 |
| Python 等价 | `parent[key] = value`；若 `parent` 是 list：`i > len(parent)` → 抛；`i == len` → `append` |

`assertIndexInRange` 的完整语义（`delta/index.ts:289-306` 注释，**必须保留**）：索引可以指向已有元素，或**恰好越界一个位置**。这不是任意上限——它保证值仍是 `JsonValue`：稀疏数组过不了 JSON 往返（洞变 `null`，回来成了真属性）。同时消除 DoS：`["s", ["xs", 4294967290], 1]` 否则会一次分配 42.9 亿元素的数组。

#### `["d", path]`

| 项 | 规格 |
|---|---|
| 元数 | 2；`path` 必须非空（`:163-164`） |
| 数组父容器 | `typeof key === "number" && key < parent.length` 否则 `PathError`；执行 `splice(key, 1)`（**删除并左移**）（`:386-388`） |
| 对象父容器 | `delete parent[key]`（`:389`） |
| Python 等价 | list：`del arr[i]`；dict：`del d[k]` |

#### `["a", path, text]`

| 项 | 规格 |
|---|---|
| 元数 | 3；`op[2]` 必须是 `string`（`:167`） |
| 语义 | `write(current + text)`（`:391-395`） |
| 边界 | 当前值**必须是 string**，否则 `PathError`（`:392-393`）。`text` 可为空串（语义 no-op，但仍是合法 op） |
| **UTF-16** | **`text` 是文本本身，不是计数 → 应用与约定无关**。`current + text` 在 Python 中即正确 |

#### `["t", path, count]`

| 项 | 规格 |
|---|---|
| 元数 | 3；`count` 必须是**整数且 ≥ 0**（`:171`），否则 `TypeError` |
| 语义 | `write(current.slice(count))`（`:397-401`）——**从字符串前端删除 `count` 个 UTF-16 code unit** |
| 边界 | 当前值必须是 string，否则 `PathError`。`count > len` 时 `slice` 返回 `""`，**不报错** |
| **UTF-16** | ⚠️ **唯一的真实陷阱**。见 §2 |

#### `["p", path, index, remove, items]`

| 项 | 规格 |
|---|---|
| 元数 | 5；`index`、`remove` 为非负整数；`items` 必须是数组（`:174-180`） |
| 路径 | `Path`——**可为空**；空路径时根自身必须是数组（`:351`） |
| 语义 | 先 `target.splice(index, remove)`，再在 `index` 处插入 `items`（`:350-358`） |
| 分块 | JS 侧按 `chunkSize = 10_000` 分块插入（`:354-357`），这是 **JS 展开运算符的栈限制 workaround，无语义内容** |
| 目标校验 | 非数组 → `PathError` |
| Python 等价 | `target[index:index+remove] = items`（**不需要分块**） |

#### `["m", path, permutation]`

| 项 | 规格 |
|---|---|
| 元数 | 3（`:182-185`） |
| 语义 | `new[i] = old[permutation[i]]`（`:360-365`），**同时**做读和写——用 `previous = target.slice()` 快照后再写 |
| 长度校验 | `target.length !== permutation.length` → `PathError`（`:362`） |
| 双射校验 | `assertPermutation`（`:199-208`）：每项必须是整数、`0 ≤ p < len`、**无重复**，否则 `TypeError("m permutation is not a bijection")` |
| Python 等价 | `prev = arr[:]`；`arr[:] = [prev[p] for p in permutation]` |

### 1.3 路径安全

`assertSafePath()`（`delta/index.ts:279-287`）+ `RESERVED_SEGMENTS`（`:131`）：

```ts
export const RESERVED_SEGMENTS: ReadonlySet<string> = new Set(["__proto__", "constructor", "prototype"]);
```

- 字符串段若属保留集 → `UnsafePathError`
- 非字符串段若不是非负整数 → `UnsafePathError`
- 解析时，数组遇到非数字段 → `UnsafePathError`（`:527`）
- 遍历只走**自有属性**（`Object.hasOwn`，`:530`）——继承的 getter 不得执行，遍历不得逃逸到原型链

**Python 侧必须保留这套保留字**（即便 Python 无原型链）：路径是**持久化格式的一部分**，保留字决定了「同一编辑是否折叠为祖先 set」。丢弃它会让 pi-py 产出的 op 与上游在含这些键的文档上分叉。

### 1.4 三种 apply 的差异

| API | 位置 | 语义 |
|---|---|---|
| `apply(target, ops)` | `:326` | **可变**。就地修改 `target`；返回根（因为 `r` 可能换根）。`r` 的 payload 被 **adopt** |
| `applyImmutable(target, ops)` | `:409` | `applyImmutableBatches(target, [ops])`。不修改输入，结果与两个输入**共享容器** |
| `applyImmutableBatches(target, batches)` | `:419` | **一次 copy-on-write 作用域跨所有批次**，**不暴露任何中间修订**（`:413-418` 注释） |

`applyImmutableBatches` 的两条优化（实现可自由取舍，但语义必须等价）：

- `appendCopy()`（`:448-473`）：对「尚未在本轮拷贝过、且 `index === target.length`、且 `deleteCount === 0`」的数组追加，用一次 `concat` 构造，避免「先拷贝再增长」的二次拷贝。
- `copyContainers()`（`:475-`）：沿路径做路径拷贝（path copying），未触及的兄弟子树直接共享。

---

## 2. ⚠️ UTF-16 陷阱：完整规格

### 2.1 源码证据（实读，非推测）

**计数来源**——字符串 diff 的发射点，`delta/tracker.ts:2039-2063`：

```ts
function emitChangedValue(operations, path, before, after): boolean {
	if (!isContainer(after) && before === after) return false;
	if (isContainer(before) && isContainer(after) && equalTrustedJson(before, after)) return false;
	if (typeof before === "string" && typeof after === "string") {
		// Slice/equality stays on V8's rope fast path; startsWith scans the entire accumulated prefix on every append.
		if (after.length > before.length && after.slice(0, before.length) === before) {
			operations.push(["a", path, after.slice(before.length)]);      // :2051
			return true;
		}
		const shared = overlap(before, after, 65_536);                     // :2054
		if (shared > 0) {
			operations.push(["t", path, before.length - shared]);          // :2056  ← UTF-16 计数
			if (after.length > shared) operations.push(["a", path, after.slice(shared)]);  // :2057
			return true;
		}
	}
	operations.push(["s", path, after]);                                   // :2061
	return true;
}
```

同样的逻辑在 `delta/diff.ts:63-76`（`emitString`）中重复一次。

**关键事实**：JS 的 `String.prototype.length` 与 `String.prototype.slice` 都按 **UTF-16 code unit** 计数。因此：

- `before.length`（`:2050`、`:2056`）= UTF-16 码元数
- `after.slice(before.length)`（`:2051`）= 按码元切片
- `before.length - shared`（`:2056`）= **`["t"]` 的计数，单位是 UTF-16 码元**
- `overlap(a, b, scan)`（`delta/index.ts:87-112`）内部用 `indexOf`/`slice`，同样在码元空间工作，返回值也是码元数。其正确性保证写在其 doc 注释 `:93`：*"Always correct: the returned n satisfies a.slice(a.length - n) === b.slice(0, n)."*

**应用侧**——`delta/index.ts:397-401`：

```ts
case "t": {
	const current = read();
	if (typeof current !== "string") throw new PathError(path);
	write(current.slice(op[2]));      // ← String.slice(n) 丢弃前 n 个 UTF-16 码元
	break;
}
```

`"a"` 侧（`:391-396`）是 `write(\`${current}${op[2]}\`)`——**纯拼接，不含计数**。

### 2.2 精确规则（含 fuzz 验证）

对 `before, after ∈ string`，记 `u16(s)` = UTF-16 码元数，`cp(s)` = Python 码点数：

> **规则 1**：`["a"]` 的 payload 在两种约定下**总是相同**。
> 因为 `before` 本身是合法字符串，其末尾必在字符边界上；`after[:len(before)] == before` 的切片点在两种约定下都落在同一字符边界，故 `after[len(before):]` 是同一个字符串。
> **验证**：30,000 例随机混合字符串 fuzz（字母 / BMP / 非 BMP / 组合符 / ZWJ / 区域指示符），`a`-op 差异 = **0**。

> **规则 2**：`["t", n]` 是**唯一**在两种约定下会不同的 op，且
> **`n_utf16 − n_cp = before 被删前缀中「非 BMP 码点」的个数`**。
> **验证**：同一次 fuzz 中 `t`-op 差异 = **1,229 / 30,000（4.1%）**，且每条差异都等于被删前缀中 `ord(c) > 0xFFFF` 的字符数（见 §2.4 样例）。

> **规则 3**：两种约定**各自都自洽**——用哪个约定发射就用哪个约定应用，round-trip 都正确。
> **验证**：30,000 例 × 2 约定，round-trip 失败 **0**。

> **规则 4**：**跨约定混用是静默错误**（不抛异常，直接算错值）。
> **验证**（`/tmp/spec/hazard.py`）：
> | before | after | JS(UTF-16) ops | 用码点切片应用 JS ops | 结果 |
> |---|---|---|---|---|
> | `"😀abc"` | `"abc"` | `[["t", 2]]` | `"😀abc"[2:]` | `"bc"` ❌（应 `"abc"`） |
> | `"😀😀"` | `"😀"` | `[["t", 2]]` | `"😀😀"[2:]` | `""` ❌（应 `"😀"`） |
> | `"👨‍👩‍👧x"` | `"x"` | `[["t", 8]]` | 码点切片 | `""` ❌（应 `"x"`） |

> **规则 5**：**上游对非 BMP 字符零测试覆盖**。
> `grep -rn 'emoji\|surrogate\|code unit\|utf16\|UTF-16' delta*.test.ts delta-tracker/*.ts state*.test.ts` → **0 hits**。
> 即：UTF-16 行为是 JS 字符串语义的副产品，**没有被上游测试固化为契约**。

### 2.3 测试向量（20 条，可直接落 pytest）

全部向量已用参考实现验证：两种约定 round-trip 均通过；`before`/`after` 经码元↔码点往返不变。

| id | 场景 | before | after | len(cp) | len(u16) | ops_utf16 | ops_codepoint | 约定一致 |
|---|---|---|---|---|---|---|---|---|
| V1 | append, 纯 BMP | `abc` | `abcdef` | 3 | 3 | `[["a", "def"]]` | `[["a", "def"]]` | ✅ |
| V2 | 向空串追加 | `` | `hello` | 0 | 0 | `[["a", "hello"]]` | `[["a", "hello"]]` | ✅ |
| V3 | 清空为 `""` | `hello` | `` | 5 | 5 | `[["s", ""]]` | `[["s", ""]]` | ✅ |
| V4 | BMP+emoji 后追加 | `a😀` | `a😀b` | 2 | 3 | `[["a", "b"]]` | `[["a", "b"]]` | ✅ |
| V5 | **丢弃前导 emoji** | `😀abc` | `abc` | 4 | 5 | `[["t", 2]]` | `[["t", 1]]` | ❌ **不同** |
| V6 | emoji 位移 → 整体 set | `xxx😀` | `xxxxx😀` | 4 | 5 | `[["s", "xxxxx😀"]]` | `[["s", "xxxxx😀"]]` | ✅ |
| V7 | 追加两个 emoji | `😀😀😀` | `😀😀😀😀😀` | 3 | 6 | `[["a", "😀😀"]]` | `[["a", "😀😀"]]` | ✅ |
| V8 | 日志行追加 | `log: 😀` | `log: 😀 done` | 6 | 7 | `[["a", " done"]]` | `[["a", " done"]]` | ✅ |
| V9 | 丢弃组合序列 | `e´tail` | `tail` | 6 | 6 | `[["t", 2]]` | `[["t", 2]]` | ✅ |
| V10 | 丢弃预组合字符 | `étail` | `tail` | 5 | 5 | `[["t", 1]]` | `[["t", 1]]` | ✅ |
| V11 | 追加 ZWJ 家庭 | `ab` | `ab👨‍👩‍👧` | 2 | 2 | `[["a", "👨‍👩‍👧"]]` | `[["a", "👨‍👩‍👧"]]` | ✅ |
| V12 | **两个 emoji → 一个** | `😀😀` | `😀` | 2 | 4 | `[["t", 2]]` | `[["t", 1]]` | ❌ **不同** |
| V13 | 区域指示符后追加 | `start🇨🇳` | `start🇨🇳!` | 7 | 9 | `[["a", "!"]]` | `[["a", "!"]]` | ✅ |
| V14 | BMP 缩短 1 | `aaaa` | `aaa` | 4 | 4 | `[["t", 1]]` | `[["t", 1]]` | ✅ |
| V15 | **孤立高位代理被丢弃** | `\ud800abc` | `abc` | 4 | 4 | `[["t", 1]]` | `[["t", 1]]` | ✅ |
| V16 | 滚动窗口（上游对拍） | `abcdefghij` | `defghijxyz` | 10 | 10 | `[["t", 3], ["a", "xyz"]]` | 同左 | ✅ |
| V17 | 无公共前后缀 | `abc` | `xyz` | 3 | 3 | `[["s", "xyz"]]` | `[["s", "xyz"]]` | ✅ |
| V18 | 后缀==前缀 | `abcXYZ` | `XYZdef` | 6 | 6 | `[["t", 3], ["a", "def"]]` | 同左 | ✅ |
| V19 | 完全相同 | `same` | `same` | 4 | 4 | `—`（无 op） | `—` | ✅ |
| V20 | **ZWJ 家庭被丢弃** | `👨‍👩‍👧x` | `x` | 6 | 9 | `[["t", 8]]` | `[["t", 5]]` | ❌ **不同** |

> **V16 与上游 `delta.test.ts:171-177` 完全对齐**（`abcdefghij` → `defghijxyz` 产出 `[["t",3],["a","xyz"]]`），可作为「实现与上游 op 级对拍」的锚点。
>
> 覆盖度对照任务要求：BMP（V1/V14/V16）、emoji 代理对（V4/V5/V7/V8/V12）、组合字符（V9/V10）、空串边界（V2/V3）、ZWJ（V11/V20）、区域指示符（V13）、孤立代理（V15）。**20 条 ≥ 要求的 8 条**。

#### pytest 参数化块（直接粘贴）

```python
# 字段: (id, before, after, ops_utf16, 两种约定是否一致)
# 注意: 非 BMP 字符写成 \UXXXXXXXX（一个码点），不要用 \uD83D\uDE00（那是两个孤立代理）
STRING_VECTORS = [
    ("V1", "abc", "abcdef", [["a", "def"]], True),
    ("V2", "", "hello", [["a", "hello"]], True),
    ("V3", "hello", "", [["s", ""]], True),
    ("V4", "a\U0001f600", "a\U0001f600b", [["a", "b"]], True),
    ("V5", "\U0001f600abc", "abc", [["t", 2]], False),
    ("V6", "xxx\U0001f600", "xxxxx\U0001f600", [["s", "xxxxx\U0001f600"]], True),
    ("V7", "\U0001f600\U0001f600\U0001f600", "\U0001f600\U0001f600\U0001f600\U0001f600\U0001f600", [["a", "\U0001f600\U0001f600"]], True),
    ("V8", "log: \U0001f600", "log: \U0001f600 done", [["a", " done"]], True),
    ("V9", "e\u0301tail", "tail", [["t", 2]], True),
    ("V10", "\u00e9tail", "tail", [["t", 1]], True),
    ("V11", "ab", "ab\U0001f468\u200d\U0001f469\u200d\U0001f467", [["a", "\U0001f468\u200d\U0001f469\u200d\U0001f467"]], True),
    ("V12", "\U0001f600\U0001f600", "\U0001f600", [["t", 2]], False),
    ("V13", "start\U0001f1e8\U0001f1f3", "start\U0001f1e8\U0001f1f3!", [["a", "!"]], True),
    ("V14", "aaaa", "aaa", [["t", 1]], True),
    ("V15", "\ud800abc", "abc", [["t", 1]], True),
    ("V16", "abcdefghij", "defghijxyz", [["t", 3], ["a", "xyz"]], True),
    ("V17", "abc", "xyz", [["s", "xyz"]], True),
    ("V18", "abcXYZ", "XYZdef", [["t", 3], ["a", "def"]], True),
    ("V19", "same", "same", None, True),
    ("V20", "\U0001f468\u200d\U0001f469\u200d\U0001f467x", "x", [["t", 8]], False),
]

@pytest.mark.parametrize("cid,before,after,ops_utf16,agree", STRING_VECTORS,
                         ids=[v[0] for v in STRING_VECTORS])
def test_string_ops_utf16(cid, before, after, ops_utf16, agree):
    """UTF-16 约定：发射 + 应用都必须与上游一致。"""
    if ops_utf16 is None:
        assert before == after
        return
    emitted = emit_string_ops(before, after)          # 被测实现
    assert emitted == ops_utf16, cid
    assert apply_utf16(before, emitted) == after, cid

@pytest.mark.parametrize("cid,before,after,ops_utf16,agree", STRING_VECTORS,
                         ids=[v[0] for v in STRING_VECTORS])
def test_convention_divergence_is_known(cid, before, after, ops_utf16, agree):
    """把「哪些向量两种约定不同」固化成断言，防止约定被无意间改动。"""
    assert (emit_string_ops(before, after) == emit_string_ops_codepoint(before, after)) is agree
```

### 2.4 参考实现（Python，UTF-16 约定）

这份代码已在 `/tmp/spec/vectors.py` 中跑通 30,000 例 fuzz，可直接移植：

```python
import struct

def to_units(s: str) -> list[int]:
    """Python str -> UTF-16 码元序列（JS 字符串的等价表示）。"""
    b = s.encode("utf-16-le", errors="surrogatepass")
    return list(struct.unpack(f"<{len(b)//2}H", b))

def from_units(u: list[int]) -> str:
    """码元序列 -> Python str：合并合法代理对，保留真正的孤立代理。

    ⚠️ 不要写成 b.decode('utf-16-le', 'surrogatepass')：
    那样会把一个合法代理对拆成两个孤立代理，得到长度 2 的 str，
    与源的 1 个码点不等价。
    """
    out, i = [], 0
    while i < len(u):
        c = u[i]
        if 0xD800 <= c <= 0xDBFF and i + 1 < len(u) and 0xDC00 <= u[i+1] <= 0xDFFF:
            out.append(chr(0x10000 + ((c - 0xD800) << 10) + (u[i+1] - 0xDC00)))
            i += 2
        else:
            out.append(chr(c))
            i += 1
    return "".join(out)

def u16_slice(s: str, start: int, end: int | None = None) -> str:
    """等价于 JS s.slice(start[, end])——按 UTF-16 码元。"""
    u = to_units(s)
    return from_units(u[start:end] if end is not None else u[start:])

def u16_len(s: str) -> int:
    return len(to_units(s))
```

**发射（对应 `tracker.ts:2047-2060`）**：在码元数组上做比较与切片，最后 `from_units` 还原 payload。
**应用（对应 `delta/index.ts:397-401`）**：`case "t": write(u16_slice(current, n))`。

> **实现提示**：只有字符串 diff 与 `"t"` 的应用需要这层转换。`"a"` 的应用直接用 `current + text`（规则 1）。为性能可缓存 `to_units` 结果，但要注意 draft 生命周期。

### 2.5 Python 方案建议：**选 UTF-16**

| 维度 | UTF-16（推荐） | code point |
|---|---|---|
| 正确性（单语言内） | ✅ 自洽 | ✅ 自洽 |
| 与上游 op 级对拍 | ✅ 可直接复用上游测试向量做 oracle | ❌ 含非 BMP 的用例会分叉 |
| 未来读写上游数据 / 迁移 | ✅ 兼容 | ❌ 静默算错 |
| 实现成本 | ~40 行，局限在 2 个函数 + diff | 0 |
| 性能 | 每次字符串 op 一次 encode/decode | 更快 |

**结论：选 UTF-16。** 理由：(1) 成本极低且**局部化**；(2) 项目既定目标是「忠实镜像上游 v1.3.0」，op 是**磁盘格式**，约定一旦落盘再改就要迁移所有含非 BMP 的历史数据；(3) 它让「用上游测试当差分 oracle」这条最强的正确性验证路径保持可用。

> **若明确排除一切与上游的数据互通**，code point 也可接受——但必须在 §3 的「内部一致性」清单上显式登记这个决定，并**在代码里写死一处常量/注释**，禁止两处实现各自为政。

---

## 3. 契约边界：Python 实现可以偏离到哪里

`delta/README.md:94` 原文：**"Don't infer meaning from op shape. Only the resulting value is contractual."**
`delta/README.md:167-168` 原文：**"Batches are exact but not canonical. The same change may use different tuples, and large edit sets may fold into a region splice, an ancestor `s`, or `r`."**

据此把契约切成三档：

### 3.1 ✅ 必须一致（契约面）

| # | 项 | 依据 |
|---|---|---|
| C1 | **结果值**：`apply(base, ops)` 必须等于语义上的目标值 | `README.md:94` |
| C2 | **op 元组的词表与元数**：7 个动词、各自 arity 固定 | `index.ts:24-38`、`:152-191`；`README.md:19-20`「Tuples are the form — in memory, on the wire, on disk」 |
| C3 | **校验拒绝集**：哪些输入必须抛错（保留段、越界索引、非双射置换、非字符串上做 `a`/`t`、未知动词） | `assertValidOp` `:152-191`、`assertSafePath` `:279-287`、`assertIndexInRange` `:304-306`。durable 依赖这些拒绝保证安全 |
| C4 | **`isBase` 语义**：`ops[0][0] === "r"`，空批次为 false | `:76` |
| C5 | **`assertIndexInRange` 的上限**：`index > len` 拒绝、`index == len` 允许 | `:289-306`（保 `JsonValue` + 防 DoS，非任意选择） |
| C6 | **所有权规则**：`apply` adopt `r` 的 payload；`applyImmutable` 不得修改输入；结果的未触及子树与输入共享 | `:336-342`、`:475-`；`delta-apply-immutable.test.ts:69-70` 用 `toBe` 断言共享 |
| C7 | **`applyImmutableBatches` 不暴露中间修订** | `:413-418` |
| C8 | **`"m"` 的语义**：`new[i] = old[perm[i]]`（用快照，不可就地下标读写） | `:360-365` |
| C9 | **`"p"` 的语义**：`arr[index:index+remove] = items` | `:350-358` |
| C10 | **`"t"` 的计数单位**（若选 UTF-16）与 **`"a"` 的 payload 是文本** | `:400`、`:394` |

### 3.2 🔓 可以不同（实现自由度）

| # | 项 | 依据 / 说明 |
|---|---|---|
| F1 | **给定 (before, after) 产出哪一串 op** | `README.md:94`、`:167`。**自由度最大的一项** |
| F2 | **op 折叠粒度**：大编辑集可折叠为区域 splice / 祖先 `s` / `r` | `README.md:168`。上游自身阈值：`MAX_DELTA_OPERATIONS = 4096`、`MAX_SIMPLE_OBJECT_NODES = 128`（`tracker.ts:112-113`）——**这是上游的选择，不是契约**，pi-py 可自定 |
| F3 | **`p` 的 10,000 分块** | `index.ts:354`，JS 展开运算符栈限制 workaround，**无语义**。Python 直接切片 |
| F4 | **内部表示**（overlay vs 拷贝）、draft handle 机制（Proxy vs 包装类） | 纯实现 |
| F5 | **`defineProperty` vs 赋值** | Python 无原型 setter，`d[k] = v` 即可（但保留段校验仍须做，见 C3） |
| F6 | **性能特征** | 上游 README `:188-191` 自己列了性能边界；pi-py 初期不必追平 |
| F7 | **no-op 是否规范化为空 ops** | `README.md:138-143`：**上游自己也不保证**——「Writes restored to their original value and deeply equal container assignments **usually** normalize to empty」，且「Structural array edits **may emit a nonempty exact batch even when the result is deeply equal**」 |
| F8 | **`["r"]` 之后 path 字典是否重置**（仅 wire 相关） | 单进程不需要 encoder/decoder |
| F9 | **错误类型名** | 只要「该拒绝的拒绝」，具体异常类名可自定（但建议保留 `PathError` / `UnsafePathError` 以便对照上游测试） |

### 3.3 ⚠️ 必须内部一致（选一次，全局统一）

| # | 项 | 后果 |
|---|---|---|
| I1 | **UTF-16 vs code point** | 混用 → **静默错值**（规则 4）。发射与应用的约定必须来自**同一处常量** |
| I2 | **保留段集合** | 决定折叠边界，影响 op 形态；若丢弃则含 `__proto__` 等键的文档与上游分叉 |
| I3 | **`overlap` 的 scan 上限** | 上游两处都是 **65,536**：tracker 字面量 `tracker.ts:2054`，diff 经 `DEFAULT_OVERLAP_SCAN = 65_536`（`diff.ts:4`，用于 `diff.ts:69`）。取值影响 op 形态但不影响结果值 |

### 3.4 一句话回答「能偏离到哪里」

> **只要 `apply(base, ops)` 的结果值、op 的词表/元数、校验的拒绝集、以及所有权/结构共享的可观察行为与上游一致，pi-py 可以用任何方式决定「产出哪一串 op」。**
> 换言之：**结果值 + 格式语法 + 拒绝行为是契约；op 的选取策略、折叠阈值、内部数据结构、性能是自由。**
> 唯一不能自由的是 **UTF-16 约定与保留段集合**——它们属于「格式语义」，且必须全局唯一。

---

## 4. delta 链收敛语义

durable 用 `base + delta*` 链持久化（`durable/src/session/transaction.ts:1019-1024`），回放见 `storage/sqlite/storage.ts:636-656`。收敛规则如下。

### 4.1 必须成立的不变量

对任意 base `B` 与批次序列 `[O₁, O₂, …, Oₙ]`：

| # | 不变量 | 说明 |
|---|---|---|
| P1 | `applyImmutable(B, O) ≡ apply(clone(B), clone(O))` 且 `B` 不变 | 上游 `delta-apply-immutable.test.ts:64-67` 直接断言 `result` 与 `mutableResult` 相等 |
| P2 | `applyImmutableBatches(B, [O₁…Oₙ]) ≡ applyImmutable(applyImmutable(…applyImmutable(B,O₁)…), Oₙ)` | `delta-apply-immutable.test.ts:136-140` 断言 `streamed == sequential` |
| P3 | `applyImmutableBatches(B, batches) ≡ applyImmutable(B, flatten(batches))` | 同测试 `:138` 断言 `flattened` |
| P4 | 每批一个 `prepared`：`applyImmutable(prepared.base, prepared.ops) ≡ prepared.value` | tracker 的核心保证（`README.md:22-25`） |
| P5 | `"r"` 重置链：`isBase(ops)` 为真的批次可以独立于之前所有批次应用 | `index.ts:76` + `README.md:182`「an `r` resets both dictionaries」 |

### 4.2 允许的不等价

| 项 | 说明 |
|---|---|
| **中间修订的容器身份** | `applyImmutableBatches` 明确**不暴露中间修订**（`index.ts:413-418`）；只有最终值有契约意义。调用方不得保留或比较中间结果 |
| **相同结果的不同 op 串** | 见 F1/F2；`prepared.ops` 与 `diffRevisions(before, after)` 可以不同，只要结果值相同 |
| **no-op 批次** | 空 `ops` 表示 `prepared.value === prepared.base`（`README.md:140`）；但反向不成立——非空 ops 也可能结果深相等（`README.md:142`）。**durable 侧因此用 `prepared.ops.length > 0` 而非值比较来决定是否写盘**（`transaction.ts:710`、`:742`） |

### 4.3 回放失败处理

`README.md:181-182`：*"After a decode or apply error, discard the decoder and replica and recover from a later `r`."*
→ pi-py 的存储回放遇到坏 delta 时，**必须**从更晚的 base 重新开始，而不是尝试「跳过坏 op 继续应用」。

---

## 5. 测试向量集（从上游测试提取）

除 §2.3 的字符串向量外，以下为上游客观存在的 op 级向量，可直接表驱动落地。全部附 `文件:行号`。

### 5.1 应用语义向量

| # | 来源 | base | ops | 期望值 |
|---|---|---|---|---|
| A1 | `delta.test.ts:164-168` | `{text:"abcdefgh"}` | `[["a",["text"],"ij"]]` | `{text:"abcdefghij"}` |
| A2 | `delta.test.ts:171-177` | A1 结果 | `[["t",["text"],3],["a",["text"],"xyz"]]` | `{text:"defghijxyz"}` |
| A3 | `delta.test.ts:188-198` | `{text:"a",values:[1,2],nested:{value:1},stable:{value:9}}` | `[["a",["text"],"b"],["p",["values"],1,1,[3,4]],["s",["nested","value"],2]]` | `{text:"ab",values:[1,3,4],nested:{value:2},stable:{value:9}}`；且 `result.stable is base.stable`（结构共享）；且 base 不变 |
| A4 | `delta.test.ts:201-208` | `undefined` | `[["r",[1,2,3]]]` → 再 `[["p",[],1,1,[4]]]` → 再 `[["m",[],[2,0,1]]]` | `[1,2,3]` → `[1,4,3]` → `[3,1,4]` |
| A5 | `delta-apply-immutable.test.ts:97-104` | `[1,2,3]` | `[["p",[],1,1,[4,5]],["m",[],[3,0,1,2]],["d",[1]]]` | `[3,4,5]`；base 不变 |
| A6 | `delta-apply-immutable.test.ts:44-82` | 见原文件 | 17 条混合 op（含 `d`、重复 `s`、`p`、`m`、共享引用与深拷贝断言） | `text=="cdef!"`；`stable`/`untouched` 共享；`copy`/`placed`/`left` 不共享；`right` 共享；`rows[0]=={id:4,label:"moved"}` |
| A7 | `delta-apply-immutable.test.ts:86-95` | `undefined` | `[[["r",replacement]], [["s",["nested","value"],2]], [["p",["values"],1,1,[4,5]],["m",["values"],[3,0,1,2]]]]` | `{nested:{value:2},values:[3,1,4,5]}`；`replacement` 不变 |

### 5.2 校验 / 错误向量

| # | 来源 | 输入 | 期望 |
|---|---|---|---|
| E1 | `delta.test.ts:211` | `apply({}, [["s",["constructor","prototype","x"],true]])` | `UnsafePathError` |
| E2 | `delta.test.ts:212` | `apply({values:[1]}, [["s",["values",3],2]])` | `UnsafePathError`（`3 > 1`） |
| E3 | `delta.test.ts:213` | `apply({value:1}, [["a",["value"],"x"]])` | 抛错（目标非字符串） |
| E4 | `delta.test.ts:214` | `assertValidOp(["s","value",1])` | 抛错（path 非数组） |
| E5 | `delta.test.ts:215` | `assertValidOp(["m",[],[0,0]])` | 抛 `/bijection/` |
| E6 | `delta.test.ts:466` | `apply({value:"abc"}, [["t",["value"],-1]])` | 抛错（count < 0） |
| E7 | `delta.test.ts:227` | `applyImmutable(target, [["s","bad-path",1]])` | 抛 `/path/`，**且不触发任何 getter 读取**（`reads === 0`）——校验先于遍历 |

### 5.3 overlap 向量

| # | 来源 | 调用 | 期望 |
|---|---|---|---|
| O1 | `delta.test.ts:181` | `overlap("abcdefgh","defghxyz",65_536)` | `5` |
| O2 | `delta.test.ts:182` | `overlap("abcdef","defghi",0)` | `0`（`scan === 0` 直接返回 0） |

### 5.4 tracker 生命周期向量

| # | 来源 | 场景 | 期望 |
|---|---|---|---|
| T1 | `tracker.test.ts:142` | 同一 revision 上 prepare 两个 change，adopt 第一个后 `adopt(secondPrepared)` | 抛 `/stale/` |
| T2 | `tracker.test.ts:146-162` | stale 的 open overlay 再 `prepare()` | 抛 `/settled/`；`abort()` 幂等 |
| T3 | `tracker.test.ts:118` | `abort()` 后持有的子 handle | 被撤销，使用抛错 |
| T4 | `tracker.test.ts:224` | abort / stale 的**已物化候选** | 仍可读（候选保持可读） |
| T5 | `tracker.test.ts:319,331,670,998,1295` | 写回原值等 no-op | `prepared.ops == []` |
| T6 | `src/delta/tracker.ts:254-277` | `adopt()` 校验顺序 | owner 不符 / consumed / aborted / stale / not-ready / `baseRevision` 不符 / `value !== prepared.base`（**身份比较**）→ 各自抛错；成功后 `revision += 1` |

### 5.5 不建议移植的向量

`encoder`/`decoder`（`delta.test.ts:239-256` 的 path interning、`["#",0,path]`、短形态 `["t",2]`、`r` 重置字典）：**单进程不需要**（`recon/05-chord-feasibility.md` §3.4 已判定 durable 零引用）。若将来要做跨进程复制再补。

---

## 6. 实现清单与工作量估算

### 6.1 模块划分（建议落在 `packages/pi-chord/src/pi_chord/`）

> ⚠️ 该目录由 `impl-durable-a` 在 task-8 创建；本节只给划分与估算，**本任务不写 `packages/`**。

| 文件 | 内容 | 对应上游 | 预估行数 |
|---|---|---|---|
| `delta/errors.py` | `PathError`、`UnsafePathError`、`RESERVED_SEGMENTS` | `index.ts:128-152`、`:310-317` | ~50 |
| `delta/ops.py` | `Seg`/`Path`/`NonEmptyPath`/`Op` 类型、`assert_valid_op`、`assert_safe_path`、`assert_index_in_range`、`assert_permutation`、`is_replace`、`is_base` | `index.ts:14-38`、`:70-76`、`:131`、`:152-306` | ~260 |
| `delta/textdiff.py` | **UTF-16 层**：`to_units`/`from_units`/`u16_len`/`u16_slice`；`overlap`；`emit_string_ops` | `index.ts:87-112`；`tracker.ts:2047-2060`；`diff.ts:63-76` | ~140 |
| `delta/apply.py` | `apply`、`apply_immutable`、`apply_immutable_batches`、`copy_containers`、`append_copy` | `index.ts:326-520` | ~330 |
| `delta/tracker.py` | `Tracker`/`Change`/`Prepared`、overlay draft、脏路径记录、op 生成、`adopt` 状态机、no-op 归一 | `tracker.ts`（多模块，见下） | ~1,200–1,800 |
| `delta/__init__.py` | 重导出 | `index.ts` | ~30 |
| **小计（src）** | | | **~2,000–2,600** |

`tracker.py` 内部分解（对应上游 `tracker.ts` 2,239 行 + 拆出模块）：

| 子模块 | 对应 | 说明 |
|---|---|---|
| overlay / draft | `tracker.ts:382+`（Proxy handler） | Python 用包装类或「脏路径 dict」 |
| 值 diff 发射 | `tracker.ts:1826-2070` | 数组 splice / permutation / 字符串 / 对象 |
| 折叠与阈值 | `tracker.ts:1654-1710`、`:1869-2016` | `MAX_DELTA_OPERATIONS`/`MAX_SIMPLE_OBJECT_NODES` 可自定（F2） |
| 状态机 | `tracker.ts:143-320` | prepared/consumed/aborted/stale 生命周期 |
| JSON 严格性 | `json.ts:33+` `copyJson` | placement 校验（拒绝循环/非有限数/稀疏数组/class 实例/`undefined`） |

> **注**：`json.py`（`copyJson` 对应物）若已由队友在 task-8 落地 `context`/`json`，此处只需 import；上表按独立实现估。

### 6.2 工作量

| 工作包 | 量级 | 说明 |
|---|---|---|
| `errors` + `ops` + `apply` + `textdiff` | **M** | 逻辑直白、无状态；`apply` 是机械翻译。**可最先完成并用 §5.1/5.2 向量验证** |
| `tracker.py` | **L** | 唯一硬骨头：overlay + 精确 op 生成 + 生命周期状态机 |
| 测试（§2.3 + §5） | **M–L** | 字符串向量 20 条 + 应用 7 组 + 校验 7 条 + 生命周期 6 组；建议补 fuzz（上游 `state-fuzz.test.ts` 123 行思路） |
| **delta 子层合计** | **L** | 与 `recon/05` 的判断一致 |

### 6.3 建议的实施顺序（降风险）

1. `errors.py` + `ops.py` + `apply.py` + `textdiff.py` —— 先让 §5.1/5.2 全绿（**此时已验证 op 语义与 UTF-16 层**）。
2. `tracker.py` 的最小闭环：`track` → `begin_change` → `prepare` → `adopt`，只支持对象 `s`/`d` 与数组 `p`/`m`。
3. 加字符串 `a`/`t`（跑 §2.3 的 20 条向量）。
4. 加折叠阈值与 no-op 归一（§3.2 F2/F7 自定策略）。
5. 补 fuzz：随机 (before, after) 对，断言 `apply_immutable(base, ops) == value` —— 这是最高性价比的正确性护栏。
6. **（可选但强烈建议）差分对拍**：把 §5 的上游向量做成 JSON 夹具，Python 侧跑同一批；选 UTF-16 约定后这项才能覆盖非 BMP 用例。

### 6.4 明确不要做

| 不做 | 理由 |
|---|---|
| `encoder`/`decoder`（wire 编解码、path interning） | durable 零引用（`recon/05` §3.4）；单进程无 wire |
| `diff_revisions`（`diff.ts` 523 行） | durable 零引用 |
| `revision_validator` | chord 内无 importer，疑似死代码 |
| `"p"` 的 10,000 分块 | JS 栈限制 workaround（F3） |
| `Object.defineProperty` 语义 | Python 无原型 setter（F5）；但保留段校验要做（C3） |

---

## 7. 无法确定的项

| # | 项 | 说明 |
|---|---|---|
| 1 | **pi-py 是否需与上游做数据互通** | 若明确「永不互通」，§2.5 的 code point 方案也成立。属产品决定 |
| 2 | ~~**`overlap` 的 scan 取值**~~ | **已解决**：上游两处均为 65,536（`tracker.ts:2054`、`diff.ts:4` 的 `DEFAULT_OVERLAP_SCAN`）。pi-py 直接沿用此值即可与上游 op 形态最大程度对齐 |
| 3 | **折叠阈值的上游实际效果** | `MAX_DELTA_OPERATIONS=4096` / `MAX_SIMPLE_OBJECT_NODES=128` 的具体触发路径未逐一验证；pi-py 可自定（F2），但如需 op 级对拍需复刻 |
| 4 | **`state-fuzz.test.ts` 的完整策略** | 仅读了 123 行的规模，未提取其生成器与断言策略；建议实现者自行设计 fuzz |
| 5 | **孤立代理在真实数据中的出现频率** | V15 是构造用例；上游未测试。若实际数据不含孤立代理，此向量的优先级可降 |
| 6 | **`equalTrustedJson` 的精确等价规则** | `tracker.ts:2046` 使用它判断容器深相等，未读其实现。影响 no-op 判定（F7），不影响结果值 |

---

## 附录：证据与复现

```bash
# op 类型与校验
sed -n '19,38p'   /tmp/pi-up/v1.3.0/packages/chord/src/delta/index.ts   # Op 类型
sed -n '152,191p' /tmp/pi-up/v1.3.0/packages/chord/src/delta/index.ts   # assertValidOp
sed -n '279,306p' /tmp/pi-up/v1.3.0/packages/chord/src/delta/index.ts   # assertSafePath + assertIndexInRange
sed -n '326,406p' /tmp/pi-up/v1.3.0/packages/chord/src/delta/index.ts   # applyOps（含 a/t 应用）
sed -n '409,441p' /tmp/pi-up/v1.3.0/packages/chord/src/delta/index.ts   # applyImmutable{,Batches}

# UTF-16 计数来源
sed -n '2039,2063p' /tmp/pi-up/v1.3.0/packages/chord/src/delta/tracker.ts  # 字符串 diff 发射
sed -n '63,76p'     /tmp/pi-up/v1.3.0/packages/chord/src/delta/diff.ts     # 同逻辑第二处
sed -n '87,112p'    /tmp/pi-up/v1.3.0/packages/chord/src/delta/index.ts    # overlap

# 上游对非 BMP 零覆盖
cd /tmp/pi-up/v1.3.0/packages/chord/test && \
  grep -rn 'emoji\|surrogate\|code unit\|utf16\|UTF-16' delta*.test.ts delta-tracker/*.ts state*.test.ts
#   → 无输出

# 契约边界原文
sed -n '94p;138,143p;167,168p' /tmp/pi-up/v1.3.0/packages/chord/src/delta/README.md

# 本报告向量的参考实现与 fuzz（30,000 例）
#   /tmp/spec/vectors.py   发射/应用双约定参考实现
#   /tmp/spec/fuzz.py      随机混合字符串 fuzz（0 round-trip 失败 / 0 个 'a'-op 差异 / 1229 个 't'-op 差异）
#   /tmp/spec/hazard.py    跨约定静默错值演示
```

# pi-chord

Python port of [`@earendil-works/chord`](https://github.com/earendil-works/pi) — the
**context microkernel**, the **strict-JSON base**, the **transactional delta tracker**,
**observable replicated state**, and **bounded output streams**.

The host half of upstream chord (host / contract / object / link / reload / …) is
**not** ported and is not planned: it has zero consumers in upstream v1.3.0 outside
`packages/chord` itself. See [`PORTING.md`](./PORTING.md).

| Upstream subpath | This package | Status |
|---|---|---|
| `@earendil-works/chord/context` (133 lines) | `pi_chord.context` | ported |
| `@earendil-works/chord/json.ts` (135 lines) | `pi_chord.json` | ported |
| `@earendil-works/chord/delta` (op layer) | `pi_chord.delta` | ported |
| `@earendil-works/chord/delta/tracker.ts` (2,239 lines) | `pi_chord.delta` (`track`) | ported |
| `@earendil-works/chord/state.ts` (423 lines) | `pi_chord.state` | ported |
| `@earendil-works/chord/output.ts` (383 lines) | `pi_chord.output` | ported |
| `@earendil-works/chord` root — host, contract, object, link, reload, registry | — | not ported (zero consumers upstream) |

## Install

```bash
pip install pi-py-chord
```

Import name has no prefix: `from pi_chord.context import ...`

## Quick Start

```python
from pi_chord.context import BACKGROUND_CONTEXT, AbortController, with_abort_signal, with_context_value

controller = AbortController()
context = with_abort_signal(controller.signal, BACKGROUND_CONTEXT)  # a cancellable invocation
key = ...

controller.abort()               # the derived context is cancelled
assert context.abort_signal.aborted
```

`Context` is an immutable, explicitly-passed carrier of invocation-scoped
values. Deriving a value never mutates its parent:

```python
from pi_chord.context import BACKGROUND_CONTEXT, create_context_key, with_context_value

REQUEST_ID = create_context_key("requestId")
child = with_context_value(REQUEST_ID, "req-1", BACKGROUND_CONTEXT)
assert child.value(REQUEST_ID) == "req-1"
assert BACKGROUND_CONTEXT.value(REQUEST_ID) is None
```

## Delta: transactional JSON operations

An op is a tuple; the vocabulary is shared by memory, the wire, and disk
(upstream `delta/index.ts:19-20`).

| op | meaning |
|---|---|
| `("r", value)` | replace the whole value (**only** op that can) |
| `("s", path, value)` | set a property or array element |
| `("d", path)` | delete a property / remove an array element |
| `("a", path, text)` | append to a string |
| `("t", path, count)` | remove `count` **UTF-16 code units** from a string's front |
| `("p", path, index, remove, items)` | array splice |
| `("m", path, permutation)` | reorder: `new[i] = old[permutation[i]]` |

```python
from pi_chord.delta import apply, apply_immutable, apply_immutable_batches

base = {"text": "a", "values": [1, 2], "stable": {"value": 9}}
ops = [("a", ("text",), "b"), ("p", ("values",), 1, 1, [3, 4])]

result = apply_immutable(base, ops)
assert result == {"text": "ab", "values": [1, 3, 4], "stable": {"value": 9}}
assert base["text"] == "a"          # the input is untouched
assert result["stable"] is base["stable"]   # untouched subtrees are shared
```

## Tracker: immutable revisions from a mutable draft

`track()` turns a plain JSON value into a revision sequence. A *change* opens a
mutable draft over the current revision, `prepare()` materialises the candidate
and its op batch **without** changing authority, and `adopt()` validates and swaps
the root pointer.

```python
from pi_chord.delta import apply_immutable, track

tracker = track({"text": "abcdefgh", "rows": [{"n": 1}, {"n": 2}]})

change = tracker.begin_change()
change.state["text"] += "ij"            # a mutable-looking draft
change.state["rows"].append({"n": 3})   # nested containers return live sub-drafts
prepared = change.prepare()

assert tracker.value["text"] == "abcdefgh"     # prepare does not change authority
assert prepared.ops == [("a", ("text",), "ij"),
                        ("p", ("rows",), 2, 0, [{"n": 3}])]
# the one invariant this package guarantees:
assert apply_immutable(prepared.base, prepared.ops) == prepared.value

tracker.adopt(prepared)                 # pointer swap; revision += 1
assert tracker.revision == 1
```

Lifecycle rules (upstream `delta/README.md:124-136`):

- The draft never modifies the base. After `prepare()`, `abort()`, or another
  change's adoption, **every** use of that draft raises.
- Several changes may be open or prepared from one revision; adopting one makes
  all the others **stale** — their `prepare()` raises and their prepared
  competitors are rejected by `adopt()`.
- `prepare_replace(value)` is a whole-root operation, not a diff: it emits
  `[("r", value)]`, or nothing when `value` is deeply equal to the current root.

### Op shape is not the contract

Upstream `delta/README.md:94`: *"Don't infer meaning from op shape. Only the
resulting value is contractual."* This port relies on that: object sets/deletes
and string appends/truncations match upstream, but **array** edits use a
common-prefix/common-suffix splice instead of upstream's minimal
piece/permutation machinery (`PORTING.md` T3). Equal-length edits still stay
element-wise, so nested string appends keep producing `("a", …)`.

Two Python-specific notes:

- **There is no `undefined`.** Upstream's `obj.k = undefined` *deletes* the key;
  in Python `None` is JSON `null` and writes `null`. See `PORTING.md` T8/J2.
- **Held sub-handles detach loudly.** When a parent list changes structure,
  already-handed-out sub-drafts are baked into the parent and marked detached;
  writing through one raises `TypeError` rather than silently losing the write
  (`PORTING.md` T6).

## Strict JSON

```python
from pi_chord.json import copy_json, is_json_value

copy_json({"a": [1, {"b": 2}]})   # deep, alias-free copy; validates while copying
is_json_value({"a": 1})           # -> True
```

`copy_json` rejects cycles, non-finite floats, non-plain containers (subclasses),
non-string keys, and non-JSON scalars — the same walk the tracker uses to validate
placements.

`JsonRepresentation` is the strict-JSON representation of an application data type.
Upstream defines it as a TypeScript conditional + mapped type (`json.ts:5-16`),
which Python's type system cannot express; this package ships the **sound upper
bound** instead:

```python
from pi_chord.json import JsonRepresentation, JsonValue

assert JsonRepresentation is JsonValue   # an approximation, not an equivalence
state: dict[str, JsonRepresentation] = {"anthropic/claude": {"input": 10}}
```

It never accepts anything upstream would reject, but it is **wider** (it does not
narrow per type) and is **not subscriptable** — write `JsonRepresentation` where
upstream writes `JsonRepresentation<X>`. See `PORTING.md` J7 for the full fidelity
table, and J8 for the `omitUndefinedProperties` porting rule (16 upstream call
sites; build the object without the key rather than relying on the flag).

`apply` mutates in place; `apply_immutable_batches` replays several batches under
one copy-on-write scope and exposes no intermediate revision.

### Strings are counted in UTF-16 code units

`("t", path, count)` counts **UTF-16 code units**, matching JS
`String.prototype.slice` (upstream `delta/index.ts:400`). Python `str` is a
sequence of code points, so this package converts at the boundary:

```python
from pi_chord.delta import emit_string_ops, u16_len

assert u16_len("😀") == 2 and len("😀") == 1
assert emit_string_ops("😀abc", "abc") == [("t", 2)]   # not ("t", 1)
```

The rule: `n_utf16 - n_codepoint` equals the number of non-BMP code points in the
removed prefix. `("a", path, text)` carries text, not a count, so it is identical
under either convention. See [`PORTING.md`](./PORTING.md) for why UTF-16 was chosen.

## Replicated state: observable immutable revisions

`replicated_state()` gives an immutable JSON value with ordered, serialized change
notifications. Either **authoritative and mutable**, or a **publication-only view**
of an external revision source.

```python
from pi_chord.context import BACKGROUND_CONTEXT
from pi_chord.state import replicated_state

state = replicated_state({"count": 0})
seen = []
unsubscribe = state.subscribe(
    lambda value, context, delivery: seen.append((delivery.kind, delivery.sequence, value))
)
assert seen == [("hydrate", 0, {"count": 0})]      # subscribing hydrates immediately

state.change(BACKGROUND_CONTEXT, lambda draft: draft.__setitem__("count", 1))
assert seen[-1] == ("update", 1, {"count": 1})
assert state.value == {"count": 1}
```

Delivery rules (upstream `state.ts:20-27`):

- Each subscription **serializes** its callbacks and awaits hydration before updates;
  an async listener is awaited before the next delivery.
- At most **100** deliveries wait behind the running callback; overflow keeps only the
  newest, so **update sequences may skip**.
- Failures are reported **in isolation** — one bad listener does not stop the others.
- Unsubscribing discards pending work without aborting or joining a running callback.
- A `change` callback runs **once and synchronously** and publishes exactly one atomic
  revision; changing a value back to what it was publishes nothing. Reentrant changes
  raise.

Pass an object with a callable `attach()` instead of an initial value to attach a
publication-only view to an authoritative source:

```python
state = replicated_state(source, ReplicatedStateSourceOptions(on_error=report))
state.dispose()          # idempotent; the last published value stays readable
```

A source's frames must be consecutive (`snapshot.cursor + 1`, then `+1` each time);
a gap is reported and disposes the attachment without publishing the gap frame.

## Output: byte utilities, bounded buffers, paced progress

```python
from pi_chord.output import OutputLimits, OutputBuffer, bound_output, character_end, utf8_byte_length

assert utf8_byte_length("😀") == 4
assert character_end("😀".encode(), 2) == 0   # backs up to a UTF-8 character start

bound_output("one\ntwo\nthree\n", OutputLimits(max_bytes=100, max_lines=2, retain="head"))
# OutputSlice(text='one\ntwo\n', bytes=8, dropped_bytes=6, dropped_lines=1)

buffer = OutputBuffer(OutputLimits(max_bytes=10, max_lines=10, retain="tail"))
buffer.push("hello\n")
buffer.push("world\n")
assert buffer.snapshot().text == "world\n"
```

`OutputBuffer` accepts string or byte chunks — byte chunks run through an
incremental UTF-8 decoder, so a multi-byte character split across chunks is
reassembled (and a trailing incomplete one flushes to U+FFFD on `end()`). Only a
BOM at the very start of a byte stream is dropped.

`Progress` paces commits: the first change after idle commits at once, then each
commit delays the next by at least `min_interval_ms` and by its written size at
100 KiB/s. At most one commit is in flight; changes made meanwhile coalesce.
It is asyncio-based, so `mark()` needs a running event loop.

⚠️ `character_end` works on **UTF-8 bytes**; `u16_len` (in `pi_chord.delta`) works on
**UTF-16 code units**. They are different units — do not mix them.
⚠️ `character_end` works on **UTF-8 bytes**; `u16_len` works on **UTF-16 code
units**. They are different units — do not mix them.

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream

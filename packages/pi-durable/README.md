# pi-durable

Python port of [`@earendil-works/pi-durable`](https://github.com/earendil-works/pi) — **portable execution environments** (the "env island"), the coding tools, and the `harness` define layer.

This package currently mirrors these upstream subpaths:

| Subpath | Upstream | Content |
|---|---|---|
| `pi_durable.env` | `@earendil-works/pi-durable/env` | capability interfaces: filesystem, shell, watcher (`env/index.ts`) |
| `pi_durable.env.node` | `@earendil-works/pi-durable/env/node` | `NodeExecutionEnv` — the local host environment (`env/node.ts`, `env/node-watch.ts`) |
| `pi_durable.testing` | `@earendil-works/pi-durable/testing` | the upstream **executable specification** for environments (`testing/env-conformance.ts`) |
| `pi_durable.tools` | `@earendil-works/pi-durable/tools` | the `read` / `write` / `edit` / `bash` / `powershell` tools, their diff layer, image detection, path normalization, per-file mutation queue |
| `pi_durable.images` | `@earendil-works/pi-durable/images` (part) | EXIF orientation; a Pillow-backed `ImageProcessor` (optional extra) |
| `pi_durable.truncate` | root entry | head truncation, `format_size`, `utf8_byte_length` |
| `pi_durable.search` | root entry | BM25 ranking behind `tool_search` and code mode |
| `pi_durable.harness` | `harness/define.ts` + a narrow slice of `harness/types.ts` | `define_tool` / `define_extension` and the tool-execution types |
| `pi_durable.tool_search` | `@earendil-works/pi-durable/tool-search` | the `tool_search` tool: BM25 over deferred tools |
| `pi_durable.types` / `ids` / `errors` | root entry | the durable records and the `Storage` contract |
| `pi_durable.storage.memory` | `@earendil-works/pi-durable/storage/memory` | `MemoryStorage`, the reference backend |
| `pi_durable.storage.sqlite` | `@earendil-works/pi-durable/storage/sqlite` | `SqliteStorage`, the file-backed backend plus migrations |

Still not ported: the rest of `harness/` (`scheduler.ts` alone is 1,953 lines — about 8,100 lines
in total), the `session/` kernel, and `codemode/`. The tools and the storage kernel are here, but
the **runtime that would execute them** (tasks, the scheduler, the registry) is not.
See [`PORTING.md`](./PORTING.md) for the batch plan, the full list of what is still missing, and
the plan for folding `pi-storage-sqlite` into this package.

## Storage

Both backends implement the same `Storage` contract and pass the same executable specification —
upstream's `testing/storage-conformance.ts`, 24 cases, ported to
`pi_durable.testing.storage_conformance`:

```python
from pi_durable.storage.memory import MemoryStorage
from pi_durable.storage.sqlite.node import open_node_sqlite_storage

storage = MemoryStorage()                                  # in-memory
storage = await open_node_sqlite_storage("durable.db")     # file-backed (WAL)
```

```bash
uv run pytest packages/pi-durable/tests/test_storage_conformance.py   # 48 passed (24 x 2 backends)
```

## Install

```bash
pip install pi-py-durable            # pulls in pi-py-chord, pi-py-ai and pydantic
pip install 'pi-py-durable[images]'  # adds Pillow, for pi_durable.images
```

Import name has no prefix. Pillow is an **optional extra**: `import pi_durable.images`
works without it; only calling the image processor raises, with an install hint.

## Quick Start

```python
import asyncio

from pi_chord.context import BACKGROUND_CONTEXT
from pi_durable.env import get_or_throw
from pi_durable.env.node import NodeExecutionEnv

async def main() -> None:
    env = NodeExecutionEnv(cwd=".")
    get_or_throw(await env.write_file("hello.txt", "hello world", BACKGROUND_CONTEXT))
    text = get_or_throw(await env.read_text_file("hello.txt", BACKGROUND_CONTEXT))
    print(text)  # hello world

asyncio.run(main())
```

Operations return `Result` values instead of raising, exactly like upstream's
`Result<TValue, TError>`:

```python
from pi_durable.env import Err, Ok, get_or_throw

result = await env.read_text_file("missing.txt", BACKGROUND_CONTEXT)
match result:
    case Ok(value):
        print(value)
    case Err(error):
        print(error.code)  # not_found
```

## Tools and images

```python
from pi_durable.tools import detect_supported_image_mime_type, image_dimensions
from pi_durable.truncate import format_size, truncate_head

with open("photo.png", "rb") as handle:
    png = handle.read()

detect_supported_image_mime_type(png)          # "image/png"
image_dimensions(png, "image/png")             # (width, height)

truncate_head("a\nb\nc", TruncationOptions(max_lines=2))   # content == "a\nb"
format_size(1536)                              # "1.5KB"
```

With the `images` extra, prepare an image for a model (orient, fit the limits,
re-encode) — upstream's `createPhotonImages()`, on Pillow:

```python
from pi_durable.images import create_pillow_images
from pi_durable.tools import DEFAULT_IMAGE_LIMITS

processor = create_pillow_images()
prepared = await processor.prepare(png, "image/png", DEFAULT_IMAGE_LIMITS)
if prepared is not None:
    prepared.mime_type   # e.g. "image/jpeg" when PNG did not fit the byte budget
    prepared.resized     # None, or Resized(from_=ImageSize(...), to=ImageSize(...))
```

`read` and `write` on one file are serialized per canonical path via
`with_file_mutation_queue`, so concurrent edits cannot interleave.

## Tools

```python
from pi_durable.tools import CodingTools, create_coding_tools

CodingTools.name                       # "coding-tools"
[t.name for t in CodingTools.tools]    # ["read", "write", "edit", "bash"]
```

Each tool is a `ToolRegistration` built by `define_tool`; `edit` also declares
`prepare_arguments`, which repairs the shapes models commonly send (`edits` as a JSON string,
a single edit object, or a top-level `oldText`/`newText` pair).

The `edit` tool's diff layer is a stdlib stand-in for the npm `diff` package. It is verified
against the real `diff@8.0.4` in `tests/test_edit_diff_parity.py`: of 36 recorded cases,
**35 match byte-for-byte and all 36 patches apply correctly**. The one difference is a
moved-line tie-break; see [`PORTING.md`](./PORTING.md) for the difference list.

## Running the executable specification

Upstream ships the environment contract as a runnable suite. This port wires it
into pytest unchanged in substance:

```bash
uv run pytest packages/pi-durable
```

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream, with upstream file:line evidence

"""pi-durable: Python port of ``@earendil-works/pi-durable`` — env island, testing, tools.

Batch A ports two upstream subpaths, keeping upstream's entry points:

* :mod:`pi_durable.env` — ``@earendil-works/pi-durable/env`` (+ ``/env/node``)
* :mod:`pi_durable.testing` — ``@earendil-works/pi-durable/testing``

Batch C adds the parts of the tool layer that do **not** need ``harness/``:

* :mod:`pi_durable.tools` — image detection/limits, path normalization, mutation queue
* :mod:`pi_durable.images` — EXIF orientation and a Pillow-backed image processor
  (Pillow is an optional extra: ``pip install 'pi-py-durable[images]'``)
* :mod:`pi_durable.truncate`, :mod:`pi_durable.search` — root-entry utilities

Batch D-1 adds the `harness` define layer, which unblocks batch C₂:

* :mod:`pi_durable.harness` — ``define_tool`` / ``define_extension`` plus a **narrow**
  tool-execution type surface (``ToolExecutionApi`` has the 7 members batch C₂ uses,
  not upstream's 20+). See that module's docstring for what is deliberately left out.

The rest of the upstream root entry (harness, session, storage, codemode) and the tool
factories that need ``harness/define.ts`` are not ported yet; see ``PORTING.md``.
"""

from __future__ import annotations

__version__ = "1.3.0"
__upstream_ref__ = "earendil-works/pi@v1.3.0"

__all__ = [
    "__version__",
    "__upstream_ref__",
]

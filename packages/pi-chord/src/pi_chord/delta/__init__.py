"""``@earendil-works/chord/delta`` 的 Python 移植。

对应上游 ``chord/src/delta/index.ts`` 与 ``chord/src/delta/README.md``。

本子路径提供**不可变 JSON 修订的事务性追踪**：op 词表、静态校验、以及三种应用
方式。本包实现的是 **op 语义层**（``ops`` / ``apply`` / ``textdiff`` / ``errors``）；
``tracker``（``Tracker`` / ``Change`` / ``Prepared`` / ``Draft``，上游
``delta/tracker.ts`` 2,239 行）是下一个工作项，**尚未移植**。

UTF-16 约定
-----------

``("t", path, count)`` 的 ``count`` 按 **UTF-16 code unit** 计数（上游
``index.ts:400`` 的 ``String.prototype.slice``）。本包采用同一约定，理由与派生
规则见 :mod:`pi_chord.delta.textdiff` 与 ``PORTING.md``。

尚未移植（上游有、本包无）
--------------------------

- ``tracker``：``track`` / ``Tracker`` / ``Change`` / ``Prepared`` / ``Draft``
- ``diffRevisions``（``delta/diff.ts``）：``pi_durable`` 零引用
- wire 词表：``WireOp`` / ``PathRef`` / ``encoder`` / ``decoder`` /
  ``assertValidWireOp``：单进程不需要 path interning
"""

from __future__ import annotations

from .apply import apply, apply_immutable, apply_immutable_batches
from .errors import RESERVED_SEGMENTS, PathError, Seg, UnsafePathError
from .ops import (
    AppendOp,
    DeleteOp,
    JsonValue,
    NonEmptyPath,
    Op,
    Path,
    PermuteOp,
    ReplaceOp,
    SetOp,
    SpliceOp,
    TruncateOp,
    assert_index_in_range,
    assert_permutation,
    assert_safe_path,
    assert_valid_op,
    is_base,
    is_replace,
    resolve,
    resolve_value,
)
from .textdiff import (
    MAX_OVERLAP_SCAN,
    AppendStringOp,
    ReplaceStringOp,
    StringOp,
    TruncateStringOp,
    apply_string_ops,
    attach_path,
    emit_string_ops,
    from_units,
    overlap,
    to_units,
    u16_len,
    u16_slice,
)
from .tracker import (
    MAX_DELTA_OPERATIONS,
    MAX_SIMPLE_OBJECT_NODES,
    Change,
    Draft,
    Prepared,
    Tracker,
    TrackerError,
    track,
)

__all__ = [
    "MAX_DELTA_OPERATIONS",
    "MAX_OVERLAP_SCAN",
    "MAX_SIMPLE_OBJECT_NODES",
    "RESERVED_SEGMENTS",
    "AppendOp",
    "AppendStringOp",
    "Change",
    "DeleteOp",
    "Draft",
    "JsonValue",
    "NonEmptyPath",
    "Op",
    "Path",
    "PathError",
    "PermuteOp",
    "Prepared",
    "ReplaceOp",
    "ReplaceStringOp",
    "Seg",
    "SetOp",
    "SpliceOp",
    "StringOp",
    "Tracker",
    "TrackerError",
    "TruncateOp",
    "TruncateStringOp",
    "UnsafePathError",
    "apply",
    "apply_immutable",
    "apply_immutable_batches",
    "apply_string_ops",
    "assert_index_in_range",
    "assert_permutation",
    "assert_safe_path",
    "assert_valid_op",
    "attach_path",
    "emit_string_ops",
    "from_units",
    "is_base",
    "is_replace",
    "overlap",
    "resolve",
    "resolve_value",
    "to_units",
    "track",
    "u16_len",
    "u16_slice",
]

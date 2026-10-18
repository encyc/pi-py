"""Session 运行时 —— ``durable/src/session/``。

**分阶段交付**（见 ``PORTING.md``）：

* ✅ ``observation.py`` —— Session ↔ chord 的提交观察桥（阶段 1）
* ✅ ``forks.py`` —— 分叉时的文档复制选择（阶段 1）
* ⚠️ ``transaction.py``(1,043) —— **第 1–2 步已到位**（提交变更纯函数 + 表读取）；
  写入暂存/装配、任务状态机、文档三步未做
* ⚠️ ``session.py``(1,081) —— **阶段 3 第 1–2 步已到位**（``GuardedStorage`` 失败守卫 + 单写线）；
  ``commit`` 与提交发布（第 3 步）、文档缓存逐出（第 4 步）未做

**独占的权威状态**仍在 :class:`~pi_durable.types.Storage`；本包不重复它。
"""

from __future__ import annotations

from pi_durable.session.forks import (
    SCAN_PAGE_SIZE,
    ForkDocumentCopy,
    prepare_fork_document_copies,
)
from pi_durable.session.observation import (
    MAX_PENDING_WATCH_FRAMES,
    RETIREMENT_OPERATIONS,
    CommittedStateSource,
    CommittedWatch,
    ObservedDocumentValue,
)
from pi_durable.session.session import GuardedStorage, SessionImpl
from pi_durable.session.transaction import (
    INTERNAL_SCAN_PAGE_SIZE,
    LoadedDocument,
    Transaction,
    TransactionHost,
    TransactionScope,
    apply_submission_change,
)

__all__ = [
    "INTERNAL_SCAN_PAGE_SIZE",
    "MAX_PENDING_WATCH_FRAMES",
    "RETIREMENT_OPERATIONS",
    "SCAN_PAGE_SIZE",
    "CommittedStateSource",
    "CommittedWatch",
    "ForkDocumentCopy",
    "GuardedStorage",
    "LoadedDocument",
    "ObservedDocumentValue",
    "SessionImpl",
    "Transaction",
    "TransactionHost",
    "TransactionScope",
    "apply_submission_change",
    "prepare_fork_document_copies",
]

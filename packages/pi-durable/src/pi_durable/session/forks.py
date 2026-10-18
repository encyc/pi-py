"""会话分叉时的文档复制选择 —— 移植 ``durable/src/session/forks.ts``（97 行）。

一次分叉要复制**两组**文档，各按自己的策略选源：

1. **as-of 组**：父会话在 ``entry`` 所在化身上已经拥有的文档——用 ``entry.commitSeq``
   作为时间点，策略 ``asOf``。
2. **current 组**：分叉之后仍然要被继承的「跟当前走」的文档——父会话的
   ``current``，策略 ``current``。

两组可能选中同一个逻辑地址；那时**报错**而不是静默取一个（``forks.ts:88-92``），
因为分叉出来的子会话在一个地址上只能有一个化身。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from pi_chord.context import Context
from pi_durable.documents import address_id
from pi_durable.types import (
    ConversationId,
    Cursor,
    DocumentAddress,
    DocumentCreate,
    DocumentPoint,
    DocumentRecord,
    EntryId,
    JsonObject,
    Storage,
)

__all__ = ["SCAN_PAGE_SIZE", "ForkDocumentCopy", "prepare_fork_document_copies"]

#: 上游 ``SCAN_PAGE_SIZE``（``forks.ts:14``）。
SCAN_PAGE_SIZE = 256

ForkPolicy = str  # "asOf" | "current"


class ForkDocumentCopy:
    """一个要随分叉创建的无定义文档副本（``forks.ts:19-22``）。"""

    __slots__ = ("record", "source")

    def __init__(self, record: DocumentCreate, source: JsonObject) -> None:
        self.record = record
        self.source = source

    def __repr__(self) -> str:
        return f"ForkDocumentCopy(record={self.record.get('id')}, source={self.source})"


async def prepare_fork_document_copies(
    storage: Storage,
    parent_conversation_id: ConversationId,
    at: EntryId,
    child_conversation_id: ConversationId,
    context: Context,
) -> list[ForkDocumentCopy]:
    """挑出一次分叉要复制的全部已持久化会话文档（``forks.ts:25-55``）。"""
    found = await storage.entry_in_conversation(parent_conversation_id, at, context)
    if found is None:
        raise RuntimeError(f"Entry {at} is not visible from conversation {parent_conversation_id}")

    copies: list[ForkDocumentCopy] = []
    copied_addresses: set[str] = set()
    # as-of 组：以该条目所属会话 + 它的提交序号为时间点
    await _collect_copies(
        storage,
        {"kind": "conversation", "conversationId": found["entry"]["conversationId"]},
        found["commitSeq"],
        "asOf",
        child_conversation_id,
        copies,
        copied_addresses,
        context,
    )
    # current 组：父会话当下「跟当前走」的文档
    await _collect_copies(
        storage,
        {"kind": "conversation", "conversationId": parent_conversation_id},
        "current",
        "current",
        child_conversation_id,
        copies,
        copied_addresses,
        context,
    )
    return copies


async def _collect_copies(
    storage: Storage,
    scope: Any,
    at: DocumentPoint,
    policy: str,
    child_conversation_id: ConversationId,
    copies: list[ForkDocumentCopy],
    copied_addresses: set[str],
    context: Context,
) -> None:
    cursor: Cursor | None = None
    while True:
        page = await storage.scan_documents(
            {"scope": scope, "at": at}, SCAN_PAGE_SIZE, cursor, context
        )
        for source in page["items"]:
            if source["scope"]["kind"] != "conversation" or source.get("fork") != policy:
                continue
            id = await storage.mint_id()
            identity: dict[str, Any] = {
                "id": id,
                "kind": source["kind"],
                "scope": {"kind": "conversation", "conversationId": child_conversation_id},
            }
            key = source.get("key")
            if key is not None:
                identity["key"] = key
            # 子会话继承源的 history，但 fork 保持源自己的策略
            record: dict[str, Any] = {
                **identity,
                "history": "latest" if source.get("history") == "latest" else "rewindable",
                "fork": source.get("fork"),
            }
            copy_address = address_id(cast(DocumentAddress, record))
            if copy_address in copied_addresses:
                member = record["kind"] if key is None else f"{record['kind']}/{key}"
                raise RuntimeError(f"Fork selects multiple source documents for {member}")
            copied_addresses.add(copy_address)
            copies.append(
                ForkDocumentCopy(
                    record=cast(DocumentCreate, record),
                    source={"id": source["id"], "at": at},
                )
            )
        cursor = page.get("next")
        if cursor is None:
            break


def fork_document_records(copies: Sequence[ForkDocumentCopy]) -> list[DocumentRecord]:
    """便利函数：只取副本的记录部分。"""
    return [copy.record for copy in copies]

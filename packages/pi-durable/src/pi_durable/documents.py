"""文档定义与逻辑地址 —— 移植 ``durable/src/documents.ts``（208 行）。

这一层**完全不碰存储**：它只回答「这个逻辑地址指向哪个化身」「这条记录能不能被这个定义读」
「这个值能不能被这个定义物化」三个问题。Session 内核（``session/``）建在它上面。

上游用 TS 的重载把一个 ``defineDoc`` 分派到四种作用域。Python 没有重载，
所以 :func:`define_doc` **只做运行期校验**，作用域/历史/分叉语义由定义字典里的
``scope`` / ``history`` / ``fork`` 字段表达，静态形状交给
:class:`~pi_durable.types.CommonDocDefinition`。这是 PORTING.md 偏离 #48 的同一取舍。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pi_chord.json import JsonValue, copy_json
from pi_durable.types import (
    DocumentAddress,
    DocumentCreate,
    DocumentId,
    DocumentRecord,
    JsonObject,
    ResolvedAddress,
    StoredDocument,
)

__all__ = [
    "DocFamilyToken",
    "DocToken",
    "address_id",
    "check_record_scope",
    "check_record_version",
    "define_doc",
    "define_doc_family",
    "document_create",
    "materialize_document",
    "materialize_document_value",
    "resolve_address",
    "validate_definition",
]


class DocToken:
    """一个已定义的文档（上游 ``{ definition }``，``documents.ts:60-63``）。"""

    __slots__ = ("definition",)

    def __init__(self, definition: Mapping[str, Any]) -> None:
        self.definition: dict[str, Any] = dict(definition)

    def __repr__(self) -> str:
        return f"DocToken(kind={self.definition.get('kind')!r}, version={self.definition.get('version')!r})"


class DocFamilyToken(DocToken):
    """一个已定义的文档族（``documents.ts:84-87``）。``initial`` 接收一个种子。"""

    __slots__ = ()


def validate_definition(definition: Mapping[str, Any]) -> None:
    """版本必须是 ≥ 1 的安全整数（``documents.ts:95-99``）。"""
    version = definition.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TypeError(f"Document {definition.get('kind')} version must be a positive integer")


def define_doc(definition: Mapping[str, Any]) -> DocToken:
    """定义一个单例文档（``documents.ts:39-63``）。

    ``initial`` 无参。
    """
    validate_definition(definition)
    return DocToken(definition)


def define_doc_family(definition: Mapping[str, Any]) -> DocFamilyToken:
    """定义一个文档族（``documents.ts:66-87``）；``initial`` 接收一个种子值。"""
    validate_definition(definition)
    return DocFamilyToken(definition)


def _owner_id(value: object, definition: Mapping[str, Any]) -> int:
    """拥有者 ID 必须是一个安全整数（``documents.ts:129-134``）。"""
    if not isinstance(value, int) or isinstance(value, bool) or abs(value) > 2**53 - 1:
        raise TypeError(
            f"Document {definition.get('kind')} requires a {definition.get('scope')} ID"
        )
    return value


def resolve_address(definition: Mapping[str, Any], args: Sequence[Any]) -> ResolvedAddress:
    """解析重载后的参数表，返回「拥有者 + 族键」之后的下标（``documents.ts:109-127``）。

    参数布局由定义的 ``scope`` 与 ``family`` 决定::

        session       → ()
        conversation  → (conversationId,)
        task          → (taskId,)
        再若 family   → (..., key)
    """
    index = 0
    scope: dict[str, Any]
    kind = definition.get("scope")
    if kind == "session":
        scope = {"kind": "session"}
    elif kind == "conversation":
        scope = {"kind": "conversation", "conversationId": _owner_id(args[index], definition)}
        index += 1
    elif kind == "task":
        scope = {"kind": "task", "taskId": _owner_id(args[index], definition)}
        index += 1
    else:
        raise TypeError(f"Document {definition.get('kind')} has an unknown scope {kind!r}")

    key: str | None = None
    if definition.get("family") is True:
        key = args[index]
        index += 1

    address: dict[str, Any] = {"kind": definition.get("kind"), "scope": scope}
    if key is not None:
        address["key"] = key
    return ResolvedAddress(
        address=cast(DocumentAddress, address),
        id=address_id(cast(DocumentAddress, address)),
        next_argument=index,
    )


def address_id(address: DocumentAddress) -> str:
    """一个逻辑地址的**稳定字符串身份**（``documents.ts:137-145``）。

    与 JS 的 ``JSON.stringify([kind, scopeKind, owner, key ?? null])`` 逐字节一致——
    包括 ``session`` 作用域下 ``owner`` 是 ``null``。这个字符串只在本进程内做 map 键，
    不落库，所以不必做孤立代理项转义（``ensure_ascii=False`` 与上游一致）。
    """
    scope: Mapping[str, Any] = address["scope"]
    scope_kind = scope["kind"]
    if scope_kind == "session":
        owner: Any = None
    elif scope_kind == "conversation":
        owner = scope["conversationId"]
    else:
        owner = scope["taskId"]
    key = address.get("key")
    return json.dumps(
        [address["kind"], scope_kind, owner, None if key is None else key],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def document_create(
    definition: Mapping[str, Any], address: DocumentAddress, id: DocumentId
) -> DocumentCreate:
    """为某个地址上的新化身构造存储用的创建记录（``documents.ts:148-165``）。

    ``key`` 只在族文档上出现；``history`` / ``fork`` **只**出现在会话文档上——
    会话文档是唯一会声明这两个语义的作用域。
    """
    base: dict[str, Any] = {"id": id, "kind": address["kind"]}
    key = address.get("key")
    if key is not None:
        base["key"] = key
    scope = address["scope"]
    base["scope"] = scope
    if scope["kind"] == "conversation":
        base["history"] = definition.get("history")
        base["fork"] = definition.get("fork")
    return cast(DocumentCreate, base)


def check_record_scope(definition: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    """拒绝「令牌与已持久化的作用域/历史/分叉语义不一致」的类型化访问（``documents.ts:168-176``）。"""
    scope: Mapping[str, Any] = record["scope"]
    mismatched = scope["kind"] != definition.get("scope")
    if not mismatched and scope["kind"] == "conversation":
        mismatched = record.get("history") != definition.get("history") or record.get(
            "fork"
        ) != definition.get("fork")
    if mismatched:
        raise TypeError(
            f"Document {record['id']} ({record['kind']}) does not match the supplied "
            "definition semantics"
        )


def check_record_version(
    definition: Mapping[str, Any], record: Mapping[str, Any], version: int
) -> None:
    """拒绝「存储里是比较新的版本」或「需要迁移但没给 migrate」的读取（``documents.ts:179-190``）。"""
    declared = int(definition.get("version", 0))
    if version > declared:
        raise RuntimeError(
            f"Document {record['id']} ({record['kind']}) has newer version {version} "
            f"than {declared}"
        )
    if version < declared and definition.get("migrate") is None:
        raise RuntimeError(
            f"Document {record['id']} ({record['kind']}) requires migration from version {version}"
        )


def materialize_document(definition: Mapping[str, Any], stored: StoredDocument) -> JsonObject:
    """把一份已存文档物化成当前定义版本的值（``documents.ts:193-195``）。"""
    return materialize_document_value(
        definition, stored["record"], stored["version"], stored["value"]
    )


def materialize_document_value(
    definition: Mapping[str, Any],
    record: DocumentCreate | DocumentRecord,
    version: int,
    value: JsonObject,
) -> JsonObject:
    """在首次持久化之前校验并物化一份解除关联的值（``documents.ts:198-208``）。

    版本相同则原样返回（调用方已持有一份解除关联的值）；版本更低则调用 ``migrate``，
    并且**只返回迁移后的值**——不写回存储。
    """
    check_record_scope(definition, record)
    check_record_version(definition, record, version)
    if version == definition.get("version"):
        return value
    migrate = definition.get("migrate")
    if migrate is None:  # pragma: no cover - check_record_version 已经拦下
        raise RuntimeError(f"Document {record['id']} has no migration")
    migrated: JsonObject = migrate(value, version)
    return cast(JsonObject, copy_json(cast(JsonValue, migrated)))


def definition_initial(definition: Mapping[str, Any], seed: JsonValue = None) -> JsonObject:
    """调用定义的 ``initial``（族文档传种子）并返回一份解除关联的值。"""
    initial = definition.get("initial")
    if initial is None:
        raise TypeError(f"Document {definition.get('kind')} has no initial()")
    produced = initial(seed)
    return cast(JsonObject, copy_json(cast(JsonValue, produced)))

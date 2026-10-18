"""ID 与序列号的**信任边界** —— 移植 ``durable/src/ids.ts``（11 行）。

上游的 ``Id<Kind>`` / ``Seq`` 是 TypeScript 的**擦除品牌类型**：编译期区分「会话 ID」与
「条目 ID」，运行期都是普通 ``number``。Python 的 :class:`typing.NewType` 语义相同——
运行期是裸 ``int``，``mypy`` 视作不同类型。

这两个函数就是上游注释所说的「trusted numeric allocation or decoding boundary」：
把已经在别处验证过的数字**擦除**成对应品牌。本端它们退化为恒等函数，但保留下来，
让调用点与上游逐一对应、且语义（「这里是信任边界」）在代码里可见。
"""

from __future__ import annotations

from pi_durable.types import Id, Seq

__all__ = ["id_from_number", "seq_from_number"]


def id_from_number(value: int) -> Id:
    """在受信的分配/解码边界给一个数字套上被擦除的 ID 品牌（``ids.ts:3-6``）。"""
    return value


def seq_from_number(value: int) -> Seq:
    """在受信的存储边界给一个数字套上被擦除的提交序列品牌（``ids.ts:8-11``）。"""
    return value

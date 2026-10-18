"""工具参数的类型来源 —— 用一个 pydantic 模型同时给出「Python 类型」和「JSON Schema」。

上游用 typebox：``Type.Object({...})`` 既是运行时的 JSON Schema，又通过 ``Static<typeof schema>``
在类型层抽出 ``args`` 的类型——**一个来源两用**。Python 没有类型级提取，所以早期设计里
``ToolRegistration.parameters``（JSON Schema）与 ``TArgs``（Python 类型）会变成两个来源，
需要人工保持一致（PORTING.md 偏离 #40）。

本模块用 pydantic 把这条缝补上：模型就是 ``TArgs``，``model_json_schema()`` 就是
``parameters``，**不可能不同步**。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

__all__ = ["json_schema_of"]


def _strip_titles(node: Any) -> Any:
    """递归去掉 pydantic 自动加的 ``title``。

    typebox 的 ``Type.Object`` / ``Type.String`` 不产生 ``title``，所以不剥掉的话
    线上 schema 会比上游多出一串字段，模型的输入分布也会随之变。
    """
    if isinstance(node, dict):
        return {key: _strip_titles(value) for key, value in node.items() if key != "title"}
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


def json_schema_of(model: type[BaseModel]) -> dict[str, Any]:
    """把 pydantic 模型转成给模型看的 JSON Schema。

    两处对齐上游的加工：

    * 去掉顶层 ``description``（模型类若有 docstring，pydantic 会把它放进去；
      typebox 的 ``Type.Object({...})`` 不带）
    * 去掉所有层级的 ``title``（pydantic 自动生成；typebox 不生成。
      pi-ai 的 ``Tool.to_json_schema()`` 也只 pop 顶层 ``title``，所以这里做在源头）
    """
    schema: dict[str, Any] = model.model_json_schema()
    schema.pop("description", None)
    return _strip_titles(schema)  # type: ignore[no-any-return]

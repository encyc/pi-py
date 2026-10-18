"""兼容转发：上游子路径 ``pi-ai/utils/validation`` 的 Python 对应写法。

**不要在本文件添加任何逻辑。** 实现位于 :mod:`pi_ai.validation`；此处仅转发，使
``from pi_ai.validation import ...`` 与 ``from pi_ai.utils.validation import ...``
解析到**同一批对象**。

.. note::
   上游 ``validation.ts`` 有一个模块级 ``validatorCache``（``WeakMap``，按 schema
   对象身份记忆化验证器）。那是**透明缓存**、无可观察语义；本端**不复刻**它，
   因此本模块与转发层都不含模块级可变状态。
"""

from __future__ import annotations

from ..validation import validate_tool_arguments, validate_tool_call

__all__ = ["validate_tool_arguments", "validate_tool_call"]

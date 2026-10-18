"""兼容转发：上游子路径 ``pi-ai/utils/overflow`` 的 Python 对应写法。

**不要在本文件添加任何逻辑。** 实现位于 :mod:`pi_ai.overflow`；此处仅转发，使
``from pi_ai.overflow import ...`` 与 ``from pi_ai.utils.overflow import ...``
解析到**同一批对象**。

.. note::
   ``overflow`` **没有**模块级可变状态（三个模式表都是不可变正则常量），
   故转发层不存在 ``uuid`` 那种"状态被复制成两份"的风险。
"""

from __future__ import annotations

from ..overflow import get_overflow_patterns, is_context_overflow, is_recoverable_length

__all__ = ["get_overflow_patterns", "is_context_overflow", "is_recoverable_length"]

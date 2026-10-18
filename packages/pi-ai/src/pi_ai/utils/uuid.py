"""兼容转发：上游子路径 ``pi-ai/utils/uuid`` 的 Python 对应写法。

**不要在本文件添加任何逻辑。** 实现位于 :mod:`pi_ai.uuid`；此处仅转发，使下列三种
写法都可用，且解析到**同一批函数对象**（``is`` 同一性成立）：

.. code-block:: python

    from pi_ai.uuid import uuidv7              # 规范位置
    from pi_ai.utils.uuid import uuidv7        # 对齐上游子路径
    from pi_ai import uuidv7                   # 顶层（上游 index.ts 亦从根导出）

.. warning::
   ``uuidv7`` 依赖 :mod:`pi_ai.uuid` 的**模块级序列状态**。本转发模块不得复制
   任何状态（例如另存一个 ``_sequence``），否则两条路径会产生两个独立序列、
   破坏单调性。这一约束有测试守护
   （``test_uuid.py::test_both_import_paths_share_the_same_generator_state``）。
"""

from __future__ import annotations

from ..uuid import uuidv7

__all__ = ["uuidv7"]

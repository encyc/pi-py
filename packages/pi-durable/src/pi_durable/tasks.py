"""定义一个可执行任务 —— 移植 ``durable/src/tasks.ts``（8 行）。

上游整个文件就是一个薄包装：``defineTask(definition) => ({ definition })``。
它的价值不在实现，而在**把「任务定义」变成一个带名字的值**——
注册表按名字装它，:class:`~pi_durable.types.Task` 协议按它取 ``definition``。

⚠️ 上游的完整 ``TaskDefinition`` 带 ``phases`` / ``abort`` / ``migrate`` / ``hooks``；
本包目前只有**窄提取**（``name`` / ``version`` / ``initial``，见
``harness/types.py`` 的范围说明）。``define_task`` **不校验**这些字段是否存在——
上游也不校验（它是个纯构造），真正的校验发生在任务运行时，
而那是**批次 D 的 scheduler**。这里保持同样的「不校验」，
免得凭空发明一套比上游更严的规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pi_durable.types import TaskDefinition

__all__ = ["DefinedTask", "define_task"]

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class DefinedTask(Generic[_T]):
    """``define_task()`` 的返回值：一个**带名字的任务定义**（上游 ``{ definition }``）。"""

    definition: Any


def define_task(definition: TaskDefinition | Any) -> DefinedTask[Any]:
    """定义一个可执行任务（``tasks.ts:4-8``）。

    把它注册进注册表，Harness 就能跑这个种类（``kind``）的任务。
    """
    return DefinedTask(definition=definition)

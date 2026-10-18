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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from pi_durable.types import TaskDefinition

__all__ = ["DefinedTask", "Definition", "define_task"]

_T = TypeVar("_T")


class Definition(dict[str, Any]):  # noqa: FURB189 - 故意的：要**两种访问都支持**
    """一个**同时支持两种访问**的任务定义。

    ⚠️ **这是本端的一个必要适配，JS 没有对应物**：上游的 ``TaskDefinition`` 是**对象字面量**，
    所以 ``definition.initial`` 与 ``definition["initial"]`` 是**同一件事**。
    Python 里它们是两件事，而本仓**两半各选了一种**：

    * **批次 B** 的 ``Transaction._create_task`` 用**属性**（``definition.initial``）；
    * **批次 D** 的 ``scheduler`` / ``live`` 用**下标**（``definition["name"]``）。

    **它们各自都测过、都过了——而合起来才炸**（``AttributeError: 'dict' object has no attribute 'initial'``）。
    **这是只有端到端测试才能发现的缺陷。**

    ⇒ 用一个 ``dict`` 子类 + ``__getattr__`` 把两种访问**都**给出去，
    这样**两侧都不用改**，而 ``define_task`` 仍然**不校验**（照上游）。
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass(frozen=True, slots=True)
class DefinedTask(Generic[_T]):
    """``define_task()`` 的返回值：一个**带名字的任务定义**（上游 ``{ definition }``）。"""

    definition: Any


def define_task(definition: TaskDefinition | Any) -> DefinedTask[Any]:
    """定义一个可执行任务（``tasks.ts:4-8``）。

    把它注册进注册表，Harness 就能跑这个种类（``kind``）的任务。
    """
    # ⚠️ **只包 Mapping**：dataclass 定义（批次 B 的测试与既有调用方用的那种）
    # 本身就有属性，包成 dict 子类会报 `'X' object is not iterable`。
    if isinstance(definition, Mapping):
        return DefinedTask(definition=Definition(definition))
    return DefinedTask(definition=cast(Any, definition))

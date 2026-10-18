"""``tasks.py``（``durable/src/tasks.ts``，8 行）的测试。

它是 ``tool.ts`` 与 ``generation.ts`` 的**共同前置**——两者都运行时导入 ``defineTask``。
"""

from __future__ import annotations

from typing import Any

from pi_durable.harness import DefinedTask, define_task


def definition(name: str = "pi.demo") -> dict[str, Any]:
    return {
        "name": name,
        "version": 1,
        "initial": lambda input_: {"phase": "ready", "input": input_},
        "phases": {"ready": object()},
        "abort": object(),
    }


def test_define_task_wraps_the_definition_unchanged() -> None:
    """上游就是 ``defineTask(definition) => ({ definition })``——**原样包起来**，不改内容。

    ⚠️ **本端不保留身份，只保留内容**：传进来的是 dict 时，`define_task` 会把它包成
    :class:`~pi_durable.tasks.Definition`（一个同时支持**下标与属性**访问的 dict 子类）。

    为什么要包：**批次 B 的 `_create_task` 用属性访问、批次 D 的调度器用下标**——
    两半各自都测过、都过了，**合起来才炸**（`AttributeError: 'dict' object has no attribute 'initial'`）。
    见 `tool_task_e2e_wip.py` 的 bug 表第 2 条。
    """
    original = definition()
    task = define_task(original)
    assert task.definition == original  # 内容不变
    # ⚠️ **两种访问都要能用**——这正是包装的目的
    assert task.definition["name"] == original["name"]
    assert task.definition.name == original["name"]


def test_define_task_leaves_a_non_mapping_definition_alone() -> None:
    """⚠️ **只包 Mapping**：dataclass 定义本身就有属性，
    包成 dict 子类会报 `'X' object is not iterable`。"""
    from dataclasses import dataclass

    @dataclass
    class Demo:
        name: str = "pi.demo"
        version: int = 1

        def initial(self, input_: object) -> dict[str, object]:
            return {}

    demo = Demo()
    task = define_task(demo)
    assert task.definition is demo


def test_define_task_returns_a_value_with_a_definition_member() -> None:
    """它把「任务定义」变成**带名字的值**——注册表按名字装它，``Task`` 协议按它取 ``definition``。"""
    task = define_task(definition("pi.generation"))
    assert isinstance(task, DefinedTask)
    assert task.definition["name"] == "pi.generation"


def test_define_task_does_not_validate() -> None:
    """⚠️ **上游不校验**，本包也不校验。

    完整 ``TaskDefinition`` 的 ``phases`` / ``abort`` / ``migrate`` / ``hooks``
    在**任务运行时**才被用到，而那是批次 D 的 scheduler。
    凭空发明一套比上游更严的规则会让合法的定义在这里就被拒掉。
    """
    bare = define_task({"name": "pi.minimal", "version": 1})
    assert bare.definition == {"name": "pi.minimal", "version": 1}


def test_defined_task_is_frozen() -> None:
    import dataclasses

    task = define_task(definition())
    assert dataclasses.is_dataclass(task)
    try:
        task.definition = {}  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("DefinedTask 应当是冻结的")


def test_two_definitions_are_independent() -> None:
    first = define_task(definition("pi.a"))
    second = define_task(definition("pi.b"))
    assert first.definition["name"] == "pi.a"
    assert second.definition["name"] == "pi.b"

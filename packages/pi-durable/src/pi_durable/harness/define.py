"""``harness/define.ts`` 的**部分**移植 —— 批次 D-1。

上游 ``durable/src/harness/define.ts`` 共 44 行、6 个函数。本模块只移植批次 C₂
需要的两个（值依赖）：

============  ====================  ==================================================
本模块         上游位置              被谁用
============  ====================  ==================================================
define_tool   ``define.ts:13-17``   ``tools/{bash,read,write,edit}.ts``、``tool-search.ts``
define_extension ``define.ts:6-10`` ``tools/index.ts``、``tool-search.ts``
============  ====================  ==================================================

**本段未移植的四个**（都不被 C₂ 使用，且需要批次 B 的 L0 内核）：

* ``hook()``（``define.ts:29-31``）—— 需要 ``HooksOf`` → ``Task``（``../types.ts``）
* ``section()``（``define.ts:20-26``）、``wrapTool()``（``define.ts:34-36``）、
  ``wrapSection()``（``define.ts:39-44``）—— 需要 ``PromptSection`` → ``PromptInput``
  → ``DocumentReader``（``../types.ts``）

与上游的形状差异
----------------

上游这两个都是**恒等函数**（``return extension`` / ``return tool``），存在的意义是让 TS
推断出泛型参数。Python 没有这个需要，所以本端把它们写成**关键字构造器**：调用形状与上游
的「传一个具名字段的对象字面量」一一对应，同时让函数本身有实事可做（构造 + 默认值）。

于是 C₂ 的调用点与上游保持视觉平行::

    # 上游
    defineTool({ name: "write", description: DESC, parameters: writeSchema, async execute(args, api, ctx) {…} })
    # 本端
    define_tool(name="write", description=DESC, parameters=WRITE_SCHEMA, execute=_execute)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from pi_chord.context import Context
from pi_durable.harness.types import (
    Extension,
    TArgs,
    ToolExecutionApi,
    ToolExecutionMode,
    ToolExecutionResult,
    ToolOutputLimits,
    ToolRegistration,
)

__all__ = [
    "define_extension",
    "define_tool",
]


def define_tool(
    *,
    name: str,
    description: str,
    parameters: Mapping[str, Any],
    execute: Callable[[TArgs, ToolExecutionApi, Context], Awaitable[ToolExecutionResult[Any]]],
    replay: Literal["safe", "unsafe"] | None = None,
    execution_mode: ToolExecutionMode | None = None,
    prepare_arguments: Callable[[Any], Any] | None = None,
    structured_output_schema: Mapping[str, Any] | None = None,
    callers: Sequence[Literal["model", "tools"]] | None = None,
    deferred: bool | None = None,
    output_limits: ToolOutputLimits | None = None,
) -> ToolRegistration[TArgs]:
    """定义一个工具（上游 ``defineTool()``，``harness/define.ts:13-17``）。

    ``parameters`` 是给模型看的 JSON Schema（上游是 typebox 的 ``TSchema``）；
    ``execute`` 的 ``args`` 类型由 :data:`~pi_durable.harness.types.TArgs` 承担，
    对应上游从 schema 提取的 ``Static<TParameters>``。mypy 会从 ``execute`` 的注解
    推断 ``TArgs``，所以像这样写就能得到类型安全的 ``args``::

        def _execute(args: WriteToolInput, api: ToolExecutionApi[Any], context: Context):
            ...
            return ToolExecutionResult(output=[TextContent(text=f"wrote {args.path}")])

        WRITE_TOOL = define_tool(name="write", description=…, parameters=…, execute=_execute)

    ``structured_output_schema`` 是给**程序调用方**的；``output_limits`` 决定保留输出
    的头还是尾（``bash`` 用 ``retain="tail"``）。
    """
    return ToolRegistration(
        name=name,
        description=description,
        parameters=parameters,
        execute=execute,
        replay=replay,
        execution_mode=execution_mode,
        prepare_arguments=prepare_arguments,
        structured_output_schema=structured_output_schema,
        callers=callers,
        deferred=deferred,
        output_limits=output_limits,
    )


def define_extension(
    *,
    name: str,
    tools: Sequence[ToolRegistration[TArgs]] = (),
) -> Extension[TArgs]:
    """定义一个扩展（上游 ``defineExtension()``，``harness/define.ts:6-10``）。

    上游的 ``Extension`` 还有 ``sections`` / ``hooks`` / ``wraps`` / ``tasks`` 四个字段
    （``harness/types.ts:365-375``）；它们都要批次 B 的 L0 内核，本段未移植，
    详见 :mod:`pi_durable.harness.types` 的模块文档。

    ``tools/index.ts:31-36`` 与 ``tool-search.ts:90-92`` 都只用 ``name`` + ``tools``，
    所以本签名足以覆盖 C₂。
    """
    return Extension(name=name, tools=tuple(tools))

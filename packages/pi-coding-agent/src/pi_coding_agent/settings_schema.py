"""设置 schema 的**类型专用切分**。

> ⚠️ **这不是 ``core/settings-schema.ts`` 的移植。**
> 上游那个模块（以及它 re-export 的 ``core/settings-manager.ts``，1,443 行）**未移植**；
> 这里**只取 ``CodemodeMode`` 一个类型**，因为 codemode 扩展需要它，而它的值域是自包含的
> 两个字面量。与该类型无关的上游内容一律不在本模块。

上游出处（逐条可核）：

* ``core/settings-schema.ts:148-150`` —— schema 实体：
  ``function codemodeMode(options?) { return Type.Union([Type.Literal("on"), Type.Literal("only")], options); }``
* ``core/settings-schema.ts:469`` —— ``export type CodemodeMode = Static<typeof CodemodeModeSchema>``
* ``core/settings-manager.ts:32`` —— 只是把该类型 **re-export**（**不是**定义处；
  按 ``settings-manager.ts`` 去找会白费一轮）
* 默认值 ``"on"`` 在 :data:`pi_coding_agent.settings_defaults.SETTINGS_DEFAULTS` 的
  ``codemode.mode``（上游 ``settings-defaults.ts`` 同位置）
"""

from __future__ import annotations

from typing import Literal

#: codemode 工具的模式（对应上游 ``CodemodeMode`` = ``"on" | "only"``）。
#:
#: * ``"on"`` —— codemode 与其它工具并存。
#: * ``"only"`` —— 只暴露 codemode；``index.ts`` 的 ``readMode()`` 会把它读出来。
CodemodeMode = Literal["on", "only"]

__all__ = ["CodemodeMode"]

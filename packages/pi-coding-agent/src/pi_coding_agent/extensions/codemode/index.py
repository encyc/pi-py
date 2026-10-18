"""``codemode`` 扩展本体：把工具注册进宿主。

对应上游 ``extensions/codemode/index.ts``（49 行）。

上游文件头：*"The `codemode` tool as an extension. The CLI loads it as a built-in extension;
SDK users add `createCodemodeExtension()` to their extension factories. `codemode` is registered
inactive. Activate it with `--tools`, the `defaultTools` setting, or `setActiveTools()`; the MCP
extension activates it when MCP tools are only reachable from scripts."*

**注册为不活跃**（``default_active=False``）—— 与 MCP 那轮的 ``create_mcp_extension`` 不同，
codemode 不默认开启，要显式激活。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...extensions.types import ExtensionAPI, ExtensionFactory
from ...settings_defaults import SETTINGS_DEFAULTS
from ...settings_schema import CodemodeMode
from .tool import CodemodeToolOptions, create_codemode_tool_definition


@dataclass
class CodemodeExtensionOptions:
    """``create_codemode_extension`` 的选项（上游 ``CodemodeExtensionOptions``）。"""

    #: 覆盖 ``codemode.mode`` 设置。
    mode: CodemodeMode | None = None
    #: 覆盖 ``codemode.inlineBudget`` 设置。
    inline_budget: int | None = None
    #: 把模型目录与分类器作为 ``models`` 暴露给脚本。默认 ``True``。
    models: bool = True


def _codemode_settings(pi: ExtensionAPI) -> Mapping[str, Any]:
    """``pi.get_settings()["codemode"]``，不是映射时按空处理。"""
    settings = pi.get_settings()
    section = settings.get("codemode") if isinstance(settings, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def read_mode(pi: ExtensionAPI) -> CodemodeMode:
    """脚本模式：设置的 ``"only"`` 优先，否则取默认值（上游 ``readMode``）。"""
    if _codemode_settings(pi).get("mode") == "only":
        return "only"
    return "on" if SETTINGS_DEFAULTS["codemode"]["mode"] == "on" else "only"


def read_inline_budget(pi: ExtensionAPI) -> int | None:
    """描述里工具声明的 token 预算（上游 ``readInlineBudget``）。

    只有**有限且非负**的数才被接受 —— ``bool`` 与 ``NaN`` 都当没设。
    """
    budget = _codemode_settings(pi).get("inlineBudget")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        return None
    if budget != budget or budget in (float("inf"), float("-inf")):  # NaN / ±inf
        return None
    return int(budget) if budget >= 0 else None


def create_codemode_extension(options: CodemodeExtensionOptions | None = None) -> ExtensionFactory:
    """把 ``codemode`` 注册成扩展（上游 ``createCodemodeExtension``，``index.ts:32``）。

    SDK 用法::

        session = create_agent_session(..., extension_factories=[create_codemode_extension()])
    """
    opts = options or CodemodeExtensionOptions()

    def factory(pi: ExtensionAPI) -> None:
        def get_mode() -> CodemodeMode:
            return opts.mode if opts.mode is not None else read_mode(pi)

        def get_inline_budget() -> int | None:
            return opts.inline_budget if opts.inline_budget is not None else read_inline_budget(pi)

        def get_tool_namespace(name: str) -> Any:
            # 上游：`pi.getAllTools().find(t => t.name === name)?.namespace`
            return next((tool.namespace for tool in pi.get_all_tools() if tool.name == name), None)

        def get_tool_guidelines() -> dict[str, list[str]]:
            return {tool.name: list(tool.prompt_guidelines or ()) for tool in pi.get_all_tools()}

        tool_options = CodemodeToolOptions(
            models=opts.models,
            get_mode=get_mode,
            get_inline_budget=get_inline_budget,
            get_tool_namespace=get_tool_namespace,
            get_tool_guidelines=get_tool_guidelines,
            append_entry=lambda custom_type, data: pi.append_entry(custom_type, data),
        )
        # **不预先填充 description**：上游注册的就是 `createCodemodeToolDefinition` 的产物，
        # 其 description 是「空工具列表」版本；真正的（带声明的）版本由 `prepare_loadout`
        # 在工具活跃时算出 —— 这样描述不会因为宿主的注册顺序而不同。
        pi.register_tool(create_codemode_tool_definition(tool_options), default_active=False)

    return factory

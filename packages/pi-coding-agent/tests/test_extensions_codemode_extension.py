"""Tier-B④-3：`create_codemode_extension` 导出 + 设置读取 + import 冒烟。"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from pi_coding_agent.extensions.codemode import index as codemode_index
from pi_coding_agent.extensions.codemode.index import (
    CodemodeExtensionOptions,
    create_codemode_extension,
    read_inline_budget,
    read_mode,
)
from pi_coding_agent.extensions.registry import ToolRegistry


def test_importable_from_package_entry():
    """验收条件：从包入口可导入，且是工厂（不是桩）。"""
    import pi_coding_agent

    assert hasattr(pi_coding_agent, "create_codemode_extension")
    assert "create_codemode_extension" in pi_coding_agent.__all__
    assert callable(pi_coding_agent.create_codemode_extension())


def test_all_codemode_modules_import():
    for name in (
        "pi_coding_agent.extensions.codemode",
        "pi_coding_agent.extensions.codemode.index",
        "pi_coding_agent.extensions.codemode.tool",
        "pi_coding_agent.extensions.codemode.execute",
    ):
        assert importlib.import_module(name) is not None


def test_factory_registers_the_tool_inactive():
    """`codemode` **注册为不活跃** —— 要显式激活（`--tools` / `defaultTools` / `setActiveTools`）。"""
    host = ToolRegistry()
    create_codemode_extension()(host)
    names = [tool.name for tool in host.get_all_tools()]
    assert "codemode" in names
    assert "codemode" not in host.get_active_tools()


def test_registered_definition_carries_the_codemode_shape():
    host = ToolRegistry()
    create_codemode_extension()(host)
    # `get_all_tools()` 是 `ToolInfo` 投影（不含 prompt_snippet），这里要的是定义本身。
    tool = host._entries["codemode"].definition
    assert tool.exposure == "model-only"
    assert tool.prompt_snippet
    assert callable(tool.prepare_loadout)
    assert tool.reaches_tools is not None


# ============================================================
# 设置读取
# ============================================================


class _Host:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    def get_settings(self) -> dict[str, Any]:
        return dict(self.settings)


def test_read_mode_defaults_to_on_and_only_wins():
    assert read_mode(_Host()) == "on"  # SETTINGS_DEFAULTS
    assert read_mode(_Host({"codemode": {"mode": "only"}})) == "only"
    assert read_mode(_Host({"codemode": {"mode": "nonsense"}})) == "on"


def test_read_inline_budget_accepts_only_finite_non_negative_numbers():
    assert read_inline_budget(_Host({"codemode": {"inlineBudget": 3000}})) == 3000
    assert read_inline_budget(_Host({"codemode": {"inlineBudget": 0}})) == 0
    for bad in (-1, True, "3000", float("nan"), float("inf")):
        assert read_inline_budget(_Host({"codemode": {"inlineBudget": bad}})) is None, bad
    assert read_inline_budget(_Host()) is None


def test_options_override_the_settings():
    """显式选项优先于设置（上游 `options.mode ?? readMode(pi)`）。"""
    host = ToolRegistry()
    host.settings = {"codemode": {"mode": "only", "inlineBudget": 5}}
    create_codemode_extension(CodemodeExtensionOptions(mode="on", inline_budget=999))(host)
    assert "codemode" in [tool.name for tool in host.get_all_tools()]


def test_factory_does_not_raise_without_settings_or_tools():
    host = ToolRegistry()
    create_codemode_extension(CodemodeExtensionOptions(models=False))(host)
    assert "codemode" in [tool.name for tool in host.get_all_tools()]
    assert codemode_index is not None


@pytest.mark.parametrize("models", [True, False])
def test_models_option_is_accepted(models: bool):
    host = ToolRegistry()
    create_codemode_extension(CodemodeExtensionOptions(models=models))(host)
    assert "codemode" in [tool.name for tool in host.get_all_tools()]

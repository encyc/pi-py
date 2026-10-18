"""Tier-B④-0 测试：codemode 的 ``core/`` 前置（类型专用切分 + 完整移植的小模块）。

对应上游 ``core/settings-defaults.ts``(67) 与 ``core/settings-schema.ts`` 的 ``CodemodeMode``。
"""

from __future__ import annotations

import json

from pi_ai import DEFAULT_MAX_AGENT_RETRY_DELAY_MS
from pi_coding_agent.settings_defaults import SETTINGS_DEFAULTS
from pi_coding_agent.settings_schema import CodemodeMode


def test_settings_defaults_is_json_serializable():
    """这张表要能被发布成 JSON Schema 默认值 —— 必须可直接序列化。"""
    encoded = json.dumps(SETTINGS_DEFAULTS)
    assert json.loads(encoded) == SETTINGS_DEFAULTS


def test_codemode_defaults_match_upstream():
    """上游 ``settings-defaults.ts`` 的 ``codemode: { mode: "on", inlineBudget: 3000 }``。"""
    assert SETTINGS_DEFAULTS["codemode"] == {"mode": "on", "inlineBudget": 3000}


def test_max_agent_delay_comes_from_pi_ai_not_a_local_copy():
    """``maxAgentDelayMs`` 取自 pi-ai（上游 import 的就是 ``pi-ai/utils/retry``）。"""
    assert SETTINGS_DEFAULTS["retry"]["maxAgentDelayMs"] == DEFAULT_MAX_AGENT_RETRY_DELAY_MS


def test_keys_stay_camel_case_because_they_are_published():
    """键名就是设置文件里的写法，**不做** snake_case 转换。"""
    for key in ("sessionShareDestination", "steeringMode", "followUpMode", "hideThinkingBlock"):
        assert key in SETTINGS_DEFAULTS, key
    assert "maxRetries" in SETTINGS_DEFAULTS["retry"]
    assert "reserveTokens" in SETTINGS_DEFAULTS["compaction"]


def test_codemode_default_mode_is_a_valid_codemode_mode():
    """默认值与类型切分的值域一致（``"on" | "only"``）。"""
    allowed = CodemodeMode.__args__  # type: ignore[attr-defined]
    assert allowed == ("on", "only")
    assert SETTINGS_DEFAULTS["codemode"]["mode"] in allowed


def test_settings_schema_module_is_type_only():
    """类型专用切分：只导出 ``CodemodeMode``，**不**假装移植了 settings-schema/manager。"""
    import pi_coding_agent.settings_schema as schema

    assert schema.__all__ == ["CodemodeMode"]
    # 上游同名模块里那些未移植的东西不得出现，免得被当成"已支持"。
    for absent in ("SettingsSchema", "SETTINGS_SCHEMA", "codemode_mode", "SettingsManager"):
        assert not hasattr(schema, absent), absent

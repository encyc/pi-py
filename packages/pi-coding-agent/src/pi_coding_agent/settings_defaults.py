"""运行期默认值。

对应上游 ``core/settings-defaults.ts``（v1.3.0，67 行）—— **完整移植**。

上游注释：*"Runtime defaults shared by settings accessors and the published JSON Schema."*
因此这是一张**会被发布成 JSON Schema 默认值**的表：键名保持 camelCase（就是设置文件里的写法），
用普通嵌套 ``dict`` 而不是 dataclass —— 它要能被 JSON 序列化、也要能直接喂 schema 生成。

访问方式（E3 口径：这是**配置常量表**，不是线上对象）：::

    SETTINGS_DEFAULTS["codemode"]["mode"]          # "on"
    SETTINGS_DEFAULTS["codemode"]["inlineBudget"]  # 3000

``maxAgentDelayMs`` 取自 pi-ai（``@earendil-works/pi-ai/utils/retry`` →
本仓 ``pi_ai.DEFAULT_MAX_AGENT_RETRY_DELAY_MS``），不是本地常量。
"""

from __future__ import annotations

from typing import Any

from pi_ai import DEFAULT_MAX_AGENT_RETRY_DELAY_MS

#: 运行期默认值（对应上游 ``SETTINGS_DEFAULTS``）。
SETTINGS_DEFAULTS: dict[str, Any] = {
    "transport": "auto",
    "sessionShareDestination": "auto",
    "steeringMode": "one-at-a-time",
    "followUpMode": "one-at-a-time",
    "compaction": {
        "enabled": True,
        "reserveTokens": 16384,
        "keepRecentTokens": 20000,
    },
    "branchSummary": {
        "reserveTokens": 16384,
        "skipPrompt": False,
    },
    "retry": {
        "enabled": True,
        "maxRetries": 3,
        "baseDelayMs": 2000,
        "maxAgentDelayMs": DEFAULT_MAX_AGENT_RETRY_DELAY_MS,
        "provider": {
            "maxRetryDelayMs": 60000,
        },
    },
    "hideThinkingBlock": False,
    "showCacheMissNotices": False,
    "quietStartup": False,
    "defaultProjectTrust": "ask",
    "collapseChangelog": False,
    "enableInstallTelemetry": True,
    "enableAnalytics": False,
    "enableSkillCommands": True,
    "terminal": {
        "showImages": True,
        "imageWidthCells": 60,
        "clearOnShrink": False,
        "showTerminalProgress": False,
    },
    "images": {
        "autoResize": True,
        "blockImages": False,
    },
    "doubleEscapeAction": "tree",
    "treeFilterMode": "default",
    "editorPaddingX": 0,
    "outputPad": 1,
    "autocompleteMaxVisible": 5,
    "markdown": {
        "codeBlockIndent": "  ",
        "mermaid": "streaming",
    },
    "warnings": {
        "anthropicExtraUsage": True,
    },
    "codemode": {
        "mode": "on",
        "inlineBudget": 3000,
    },
    "cacheWarming": "streaming",
    "tuiMode": "fullscreen",
    "fullscreenExitOutput": "transcript",
    "fullscreenScrollbar": "auto",
    "fullscreenCopyOnSelect": True,
    "fullscreenWheelScrollLines": "auto",
}

__all__ = ["SETTINGS_DEFAULTS"]

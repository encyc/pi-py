"""pi-coding-agent: Python port of @earendil-works/pi-coding-agent.

编码 agent SDK：AgentSession、工具（bash/read/edit/write/grep/find/ls）、
扩展系统、会话管理。

本包**仅复刻上游 ``core/`` 与 ``modes/rpc/``**，有意偏离：
- 不复刻 ``modes/interactive/``（TS Ink TUI 渲染，约 16.5k LOC，Python 端无移植价值）
- 不复刻 ``cli.ts`` / ``main.ts`` CLI 入口（本仓库定位为 SDK 库）
详见 PORTING.md。
"""

__version__ = "0.81.1"
__upstream_ref__ = "earendil-works/pi@v0.81.1"

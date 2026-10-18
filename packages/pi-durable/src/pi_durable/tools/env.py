"""工具的执行环境入口 —— 移植 ``durable/src/tools/env.ts``（8 行）。"""

from __future__ import annotations

from pi_durable.env import ExecutionEnv
from pi_durable.harness.types import ToolExecutionApi

__all__ = ["require_env"]


def require_env(api: ToolExecutionApi) -> ExecutionEnv:
    """本次调用的执行环境；没有环境时抛一个普通错误（``env.ts:4-8``）。

    上游抛 ``Error("No execution environment is configured")``；这里的消息逐字保留
    （``test/tools.test.ts:199`` 断言 ``rejects.toThrow("No execution environment")``）。
    """
    if api.env is None:
        raise RuntimeError("No execution environment is configured")
    return api.env

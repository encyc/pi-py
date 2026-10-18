"""工具测试共用的 ``ToolExecutionApi`` 替身。

实现 D-1 窄接口的 7 个成员，够 ``bash`` / ``edit`` / ``write`` / ``read`` / ``tool_search``
在真实文件系统上跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pi_chord.context import Context
from pi_durable.env import ExecutionEnv, ShellOutputSkip, ShellOutputWindow
from pi_durable.harness import Agent, RetainedOutput, ToolDiagnostic

__all__ = ["FakeToolApi"]


@dataclass
class FakeToolApi:
    """``ToolExecutionApi`` 的测试替身（只实现本包用到的 7 个成员）。"""

    env: ExecutionEnv | None = None
    models: Any = None
    output_window: ShellOutputWindow | None = None
    agent_value: Agent[Any] | None = None
    #: 保留输出的上限，模拟 harness 的 tail 缓冲；``None`` 表示全留。
    retained_limit: int | None = None
    chunks: list[str] = field(default_factory=list)
    diagnostics: list[ToolDiagnostic] = field(default_factory=list)
    dropped: int = 0

    def output(self, chunk: str | bytes, skipped: ShellOutputSkip | None = None) -> None:
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
        self.chunks.append(text)

    def retained_output(self) -> RetainedOutput:
        text = "".join(self.chunks)
        if self.retained_limit is not None and len(text) > self.retained_limit:
            dropped = len(text) - self.retained_limit
            self.dropped += dropped
            text = text[-self.retained_limit :]
            return RetainedOutput(text=text, truncated=True)
        return RetainedOutput(text=text, truncated=self.dropped > 0)

    def diagnostic(self, diagnostic: ToolDiagnostic) -> None:
        self.diagnostics.append(diagnostic)

    async def agent(self, context: Context) -> Agent[Any]:
        del context
        if self.agent_value is None:
            return Agent()
        return self.agent_value

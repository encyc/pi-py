"""``harness/`` 的**批次 D-1** 切片：define 层 + 工具执行的最小类型面。

上游 ``durable/src/harness/`` 有 21 个文件、8,177 行；批次 D 的完整范围见
``recon/06-durable-breakdown.md`` §6。**本包只做了其中一小段**，目标是解锁批次 C₂
（``tools/`` 与 ``tool-search/``，1,325 行）。

已移植
------

* :mod:`pi_durable.harness.define` —— ``define_tool`` / ``define_extension``
  （上游 ``harness/define.ts:6-17``）
* :mod:`pi_durable.harness.types` —— ``ToolRegistration`` / ``ToolExecutionApi``
  （**窄接口，只有 C₂ 实测用到的 7 个成员**）/ ``ToolExecutionResult`` / ``ToolDiagnostic``
  及其最小传递闭包

**未移植**（批次 D 的其余部分，见 ``PORTING.md``）：``scheduler.ts``（1,953 行）、
``harness.ts``、``generation.ts``、``compaction.ts``、``tool.ts``、``events.ts``、
``live.ts``、``view.ts``、``registry.ts``、``context.ts``、``inbox.ts``、
``submissions.ts``、``task-graph.ts``、``agent.ts``、``prompt.ts``、``provider.ts``、
``usage.ts``、``json.ts``、``util.ts``，以及 ``define.ts`` 里另外四个函数。
"""

from __future__ import annotations

from pi_durable.harness.define import define_extension, define_tool
from pi_durable.harness.runtime import UNSET, TaskRuntime
from pi_durable.harness.types import (
    Agent,
    AgentChange,
    AgentState,
    AnyTask,
    CompactionPolicy,
    CompactionReason,
    CompactionResult,
    ContextView,
    ConversationAbortOptions,
    ConversationHandle,
    ConversationRetryPolicy,
    ConversationStreamOptions,
    DocumentReader,
    Extension,
    HarnessSettings,
    HookRegistration,
    HookRunner,
    InputEntry,
    InputSubmissionDraft,
    ModelRef,
    Models,
    NestedToolExecutionResult,
    ProgressPolicy,
    PromptInput,
    PromptSection,
    QueueMode,
    RegistrySnapshot,
    RetainedOutput,
    Settings,
    SettledSubmissionRecord,
    SettledTask,
    SubmissionDraft,
    ToolControl,
    ToolDiagnostic,
    ToolExecutionApi,
    ToolExecutionMode,
    ToolExecutionResult,
    ToolOutputLimits,
    ToolRegistration,
    Wrap,
)
from pi_durable.tasks import DefinedTask, define_task

__all__ = [
    "Agent",
    "AgentChange",
    "AgentState",
    "AnyTask",
    "CompactionPolicy",
    "CompactionReason",
    "CompactionResult",
    "ContextView",
    "ConversationAbortOptions",
    "ConversationHandle",
    "ConversationRetryPolicy",
    "ConversationStreamOptions",
    "DefinedTask",
    "DocumentReader",
    "Extension",
    "HarnessSettings",
    "HookRegistration",
    "HookRunner",
    "InputEntry",
    "InputSubmissionDraft",
    "ModelRef",
    "Models",
    "NestedToolExecutionResult",
    "ProgressPolicy",
    "PromptInput",
    "PromptSection",
    "QueueMode",
    "RegistrySnapshot",
    "RetainedOutput",
    "Settings",
    "SettledSubmissionRecord",
    "SettledTask",
    "SubmissionDraft",
    "TaskRuntime",
    "ToolControl",
    "ToolDiagnostic",
    "ToolExecutionApi",
    "ToolExecutionMode",
    "ToolExecutionResult",
    "ToolOutputLimits",
    "ToolRegistration",
    "UNSET",
    "Wrap",
    "define_extension",
    "define_task",
    "define_tool",
]

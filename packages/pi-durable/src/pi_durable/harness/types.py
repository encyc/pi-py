"""harness 的**最小类型面** —— 批次 D-1，只覆盖批次 C₂ 需要的符号。

上游 ``durable/src/harness/types.ts`` 是 812 行、46 个导出类型。本模块**不是**它的移植，
而是它针对「工具定义与执行」这一个切面的**窄提取**，用来解锁批次 C₂ 的 1,325 行
（``tools/`` 与 ``tool-search/``）。

范围判定
--------

以「C₂ 实际使用的符号」为根，用脚本算出的类型传递闭包是 **24 / 46**。进一步剔除
需要批次 B 的 L0 内核（``Tx`` / ``Task`` / ``TaskRecord`` / ``ConversationHandle`` /
``DocumentObserver`` / ``DocumentReader``）的那些之后，本模块只保留下列符号：

===============  ====================================  ==============================
本模块符号        上游位置                              截取理由
===============  ====================================  ==============================
define_tool      ``harness/define.ts:13-17``           C₂ 值依赖
define_extension ``harness/define.ts:6-10``            C₂ 值依赖（``tools/index.ts``、``tool-search.ts``）
ToolRegistration ``harness/types.ts:293-317``          只保留 C₂ 会设置的字段
ToolExecutionApi ``harness/types.ts:205-292``          **只保留 C₂ 实测用到的 7 个成员**
ToolExecutionResult ``harness/types.ts:154-167``       output / structuredOutput / isError / details / diagnostics / usage / control
ToolDiagnostic   ``harness/types.ts:144-149``          原样
ToolControl      ``harness/types.ts:137-142``          原样（``tool-search`` 用 ``addTools``）
Agent            ``harness/types.ts:447-466``          只保留 ``model`` / ``tools`` / ``callable`` / ``deferred``
Extension        ``harness/types.ts:365-375``          只保留 ``name`` / ``tools``
ModelRef         ``harness/types.ts:47-50``            原样
===============  ====================================  ==============================

**明确省略**（都依赖批次 B 的 L0 内核，本段做不了也不该做）：

* ``ToolExecutionApi`` 的其余成员：``taskId``、``conversationId``、``callId``、``registry``、
  ``details()``、``commit()``、``memo()``、``createTask()``、``getTask()``、``waitForTask()``、
  ``conversation()``、``executeTool()``（``harness/types.ts:205-292``）
* 它继承的 ``DocumentObserver`` / ``DocumentReader``
* ``Agent`` 的 ``thinkingLevel`` / ``extensions`` / ``sections`` / ``instructions`` / ``cwd``
* ``Extension`` 的 ``sections`` / ``hooks`` / ``wraps`` / ``tasks``
* ``Section`` 家族：``PromptInput`` / ``PromptSection`` / ``Wrap``（都需要 ``DocumentReader``
  或 ``Agent.sections``）
* ``SettledTask``（需要 ``TaskRecord`` / ``TaskState``）
* ``hook()`` / ``section()`` / ``wrapTool()`` / ``wrapSection()``（``harness/define.ts:20-44``）——
  C₂ 不用，且 ``hook()`` 需要 ``HooksOf`` → ``Task``

**这是本端的窄接口，不是上游那个 20+ 成员的类型。** 详见 ``PORTING.md`` 的偏离条目。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import (
    Any,
    Generic,
    Literal,
    NotRequired,
    Protocol,
    Required,
    TypeAlias,
    TypedDict,
    TypeVar,
    runtime_checkable,
)

from pi_ai import ImageContent, TextContent, Usage
from pi_chord.context import Context
from pi_chord.json import JsonValue
from pi_durable.env import ExecutionEnv, ShellOutputSkip, ShellOutputWindow
from pi_durable.types import EntryDraft, EntryId, EntryRecord, SubmissionId

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
    "RegistryReader",
    "RegistrySnapshot",
    "RetainedOutput",
    "Settings",
    "SettledSubmissionRecord",
    "SettledTask",
    "SubmissionDraft",
    "ToolControl",
    "ToolDiagnostic",
    "ToolExecutionApi",
    "ToolExecutionMode",
    "ToolExecutionResult",
    "ToolOutputLimits",
    "ToolRegistration",
    "Wrap",
]

#: 工具参数的类型。上游是 ``Static<TParameters>``（从 typebox schema 提取的 TS 类型）；
#: Python 没有类型级提取，改由工具自己声明——冻结 dataclass、pydantic 模型或 TypedDict 都行。
TArgs = TypeVar("TArgs")

#: 工具 ``details`` 的类型（上游 ``TDetails extends JsonValue``）。
TDetails = TypeVar("TDetails")

#: 上游 ``harness/types.ts:196``。
ToolExecutionMode = Literal["parallel", "sequential"]


@dataclass(frozen=True, slots=True)
class ModelRef:
    """指向一个模型（上游 ``harness/types.ts:47-50``）。"""

    provider: str
    model_id: str


@dataclass(frozen=True, slots=True)
class ToolDiagnostic:
    """关于某次调用的一条说明，给模型和 UI 看；**从不**进入工具的数据
    （上游 ``harness/types.ts:144-149``）。"""

    severity: Literal["info", "warn", "error"]
    message: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class ToolControl:
    """``tool_search`` 用它把匹配到的工具名交给下一次请求（上游 ``harness/types.ts:137-142``）。"""

    add_tools: Sequence[str] | None = None
    terminate: bool | None = None
    handoff: str | None = None


@dataclass(frozen=True, slots=True)
class ToolOutputLimits:
    """单次工具输出的上限（上游 ``harness/types.ts:329-335``）。

    ``retain="tail"`` 是 ``bash`` / ``powershell`` 用的：错误和结果在输出末尾。
    """

    max_bytes: int | None = None
    max_lines: int | None = None
    retain: Literal["head", "tail"] | None = None


@dataclass(frozen=True, slots=True)
class RetainedOutput:
    """``api.retained_output()`` 的返回值：模型将会看到的文本，以及早前输出是否被丢弃
    （上游 ``harness/types.ts:281-284``）。"""

    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class ToolExecutionResult(Generic[TDetails]):
    """工具返回值，三个通道（上游 ``harness/types.ts:154-167``）：

    * ``output`` —— 给**模型**看的（省略时用 ``api.retained_output()`` 的文本）
    * ``structured_output`` —— 给**程序**看的（``execute_tool()`` 调用方、code mode 脚本）
    * ``details`` —— 给 **UI** 看的
    """

    output: Sequence[TextContent | ImageContent] | None = None
    structured_output: Any = None
    is_error: bool | None = None
    details: TDetails | None = None
    #: 由 ``api.diagnostic()`` 记录的诊断**之后**追加的额外诊断。
    diagnostics: Sequence[ToolDiagnostic] | None = None
    usage: Usage | None = None
    control: ToolControl | None = None


@dataclass(frozen=True, slots=True)
class ToolRegistration(Generic[TArgs]):
    """注册表中的可执行工具（上游 ``harness/types.ts:293-317``）。

    上游是 ``Tool<TParameters> & {...}``，其中 ``Tool`` 来自 pi-ai
    （``name`` / ``description`` / ``parameters`` / ``constrainedSampling``）。
    这里把继承**摊平**成三个同名字段——字段与语义完全一致，只是不再共用那个基类，
    因为 pi-ai 的 ``Tool`` 是 pydantic 模型，而本类型要携带可调用的 ``execute``。

    ``parameters`` 是 JSON Schema（对应上游 typebox 的 ``TSchema``）；上游的类型级
    ``Static<TParameters>`` 在本端由 :data:`TArgs` 承担。
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    execute: Callable[[TArgs, ToolExecutionApi, Context], Awaitable[ToolExecutionResult[Any]]]
    #: 被打断的执行能否在恢复时重跑；默认 ``"unsafe"``。
    replay: Literal["safe", "unsafe"] | None = None
    #: 默认取 settings 的 ``toolExecution``。一次顺序调用会让它所在的整轮都变成顺序。
    execution_mode: ToolExecutionMode | None = None
    #: 在验证**之前**修复模型常弄错的参数，例如把数组写成 JSON 字符串。必须是纯函数，
    #: 且不得改动 ``args``：调用被重试时会再跑一次。返回值仍会按 ``parameters`` 验证。
    prepare_arguments: Callable[[Any], Any] | None = None
    #: ``structured_output`` 的 schema（给程序调用方）。省略时它们拿到有界输出。
    structured_output_schema: Mapping[str, Any] | None = None
    #: 谁可以调用：模型、其它工具（``execute_tool()``），或两者（默认）。
    callers: Sequence[Literal["model", "tools"]] | None = None
    #: 在 ``modelTools`` 点名之前不提供给模型（``tool_search`` 加载的就是这类工具）。
    deferred: bool | None = None
    output_limits: ToolOutputLimits | None = None


class HookRegistration(TypedDict):
    """由 ``hook()` 构建；**按名字**匹配任务（``types.ts:357``）。"""

    task: Required[str]
    handlers: Required[Any]


class _WrapTool(TypedDict):
    """``Wrap`` 的 tool 半边（``types.ts:360-363``）。"""

    tool: Required[str]
    wrap: Required[Any]


class _WrapSection(TypedDict):
    """``Wrap`` 的 section 半边。"""

    section: Required[str]
    wrap: Required[Any]


#: 由 ``wrapTool()`` / ``wrapSection()`` 构建；瞄准一个工具名或一个 section key。
#: **包装器必须是纯的**（``types.ts:360-363``）。
Wrap: TypeAlias = _WrapTool | _WrapSection


@dataclass(frozen=True, slots=True)
class Extension(Generic[TArgs]):
    """具名代码包，装进注册表后由会话按名字选中（上游 ``harness/types.ts:365-375``）。

    D-1 只保留了 ``name`` 与 ``tools``（C₂ 只用这两个）。批次 D 落地
    ``PromptSection`` / ``HookRegistration`` / ``Wrap`` / ``AnyTask`` 之后，
    其余四个字段按**完整形状**补齐（见 PORTING.md **R28**）——它们是**纯加的可选字段**，
    不改 ``name`` / ``tools`` 的任何语义，所以不破坏 C₂。
    """

    name: str
    tools: Sequence[ToolRegistration[TArgs]] = field(default_factory=tuple)
    sections: Sequence[Any] = field(default_factory=tuple)
    #: 按名字匹配任务的钩子。**解析时**按扩展顺序取。
    hooks: Sequence[HookRegistration] = field(default_factory=tuple)
    #: 在选中本扩展处**按序**套用。
    wraps: Sequence[Any] = field(default_factory=tuple)
    #: 按名字解析的任务定义，**无论**哪些会话选中了本扩展。
    tasks: Sequence[Any] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Agent(Generic[TArgs]):
    """一次请求解析出的 agent（上游 ``harness/types.ts:447-466``）。

    ``tool_search`` 读 ``deferred``、``read`` 读 ``model``；``tools`` / ``callable``
    一并保留（它们只是 ``ToolRegistration`` 序列，不引入新依赖）。
    """

    model: ModelRef | None = None
    #: 思考级别（``types.ts:447-466``）；``resolve_agent()`` 在未设时给 ``"off"``。
    thinking_level: str = "off"
    #: 选中的扩展，按序。
    extensions: Sequence[Any] = field(default_factory=tuple)
    #: 本次请求提供给模型的工具，按顺序。
    tools: Sequence[ToolRegistration[TArgs]] = field(default_factory=tuple)
    #: 嵌套调用（``execute_tool()``）可以解析到的工具，按顺序。
    callable: Sequence[ToolRegistration[TArgs]] = field(default_factory=tuple)
    #: 已启用、模型可以调用但尚未提供的延迟工具，按注册表顺序——``tool_search`` 加载的就是它们。
    deferred: Sequence[ToolRegistration[TArgs]] = field(default_factory=tuple)
    #: system 提示 section，按渲染顺序。``instructions`` 会作为最后一个 section 追加进来。
    sections: Sequence[Any] = field(default_factory=tuple)
    #: 直接给出的 instructions；同时也会以 ``INSTRUCTIONS_KEY`` 出现在 ``sections`` 末尾。
    instructions: str | None = None
    #: 环境文件系统里的工作目录。
    cwd: str | None = None


@runtime_checkable
class Models(Protocol):
    """模型目录（上游 ``harness/types.ts:215`` 的 ``Models``，来自 pi-ai）。

    上游的 pi-ai ``Models`` 接口有 8 个方法（``getModel`` / ``completeSimple`` /
    ``streamSimple`` / ``fetchDeferred`` / ``cancelDeferred`` / ``getModelsOfType`` /
    ``getAvailableOfType`` / ``getModelOfType``）。**本端只声明 C₂ 用到的那一个**：
    ``read`` 用它把图像解析成具体模型（``tools/read.ts:202``）。其余随
    generation / compaction（批次 D 后续）一起加。
    """

    def get_model(self, provider: str, model_id: str) -> Any | None: ...


@runtime_checkable
class ToolExecutionApi(Protocol):
    """工具执行时可用的能力。

    ⚠️ **本端是窄接口**：上游 ``harness/types.ts:205-292`` 有 20+ 个成员，并继承
    ``DocumentObserver`` / ``DocumentReader``。这里**只声明批次 C₂ 实测用到的 7 个**
    （``grep -rnoE 'api\\.[a-zA-Z_]+' tools/ tool-search/`` → 12 处、7 个成员、无解构）：

    ``output``(3) / ``outputWindow``(2) / ``env``(2) / ``agent``(2) /
    ``retainedOutput``(1) / ``models``(1) / ``diagnostic``(1)

    上游是 ``ToolExecutionApi<TDetails>``，泛型参数只被 ``details(value: TDetails, …)``
    使用（``harness/types.ts:286``）。那个成员本段省略了，所以这里**不带泛型**——
    留一个没人用的类型参数只会误导。批次 D 补回 ``details()`` 时一并恢复泛型。
    """

    @property
    def env(self) -> ExecutionEnv | None:
        """由 ``HarnessOptions.env`` 为本次调用构建；没有环境时为 ``None``
        （``tools/env.ts:5-8`` 的 ``requireEnv`` 依赖它）。"""
        ...

    @property
    def models(self) -> Models:
        """``HarnessOptions.models``：目录、凭据与请求变换（``tools/read.ts:202``）。"""
        ...

    @property
    def output_window(self) -> ShellOutputWindow | None:
        """本次调用保留的输出尾部与进度提交节奏，给 ``ShellExecOptions.window`` 用；
        工具保留**头部**时为 ``None``（``tools/bash.ts:107``）。"""
        ...

    def output(self, chunk: str | bytes, skipped: ShellOutputSkip | None = None) -> None:
        """追加运行中的输出；结果省略 ``output`` 时它会成为结果的 ``output``
        （``tools/bash.ts:104``）。"""
        ...

    def retained_output(self) -> RetainedOutput:
        """到目前为止保留的输出，以及早前输出是否被丢弃（``tools/bash.ts:126``）。"""
        ...

    def diagnostic(self, diagnostic: ToolDiagnostic) -> None:
        """记录一条模型可见的说明（``tools/bash.ts:117``）。"""
        ...

    async def agent(self, context: Context) -> Agent[Any]:
        """本次调用所在会话的 agent（``tools/read.ts:201``、``tool-search.ts:67``）。"""
        ...


# --------------------------------------------------------------------------------------
# 批次 D：宿主提交契约（``types.ts:47-110``）
# --------------------------------------------------------------------------------------


class InputEntry(TypedDict, total=False):
    """输入要落成的那个条目，而不是 ``pi.user``（``types.ts:58``）。

    ``kind`` 是应用自定义的种类，``data`` 是 transcript 展示的东西（比如作者）。
    它的**模型消息**仍然是输入自己的 user message。
    """

    kind: Required[str]
    data: NotRequired[JsonValue]


class _InputDraft(TypedDict, total=False):
    """``SubmissionDraft`` 的 input 半边（``types.ts:63-73``）。"""

    type: Required[Literal["input"]]
    content: Required[Any]
    whenBusy: NotRequired[Literal["steer", "followUp", "reject"]]
    #: 把输入落成这个条目；默认是不带 data 的 ``pi.user``。**kind 不得以 ``pi.`` 开头**。
    entry: NotRequired[InputEntry]


class _WriteDraft(TypedDict, total=False):
    """``SubmissionDraft`` 的 write 半边：一个被动的条目写入（``types.ts:74-79``）。"""

    type: Required[Literal["write"]]
    entry: Required[EntryDraft]


class _SubmissionDraftBase(TypedDict, total=False):
    requestId: str


#: 宿主提交：可能启动一次运行的**用户输入**，或一次**被动的条目写入**（``types.ts:61-79``）。
SubmissionDraft: TypeAlias = _InputDraft | _WriteDraft

#: ``SubmissionDraft`` 里 input 那一支（``types.ts:79``）。
InputSubmissionDraft: TypeAlias = _InputDraft


class SettledSubmissionRecord(TypedDict, total=False):
    """已结算的提交记录：``done`` 或 ``unanswered``（``types.ts:81-84``）。"""

    status: Required[Literal["done", "unanswered"]]


class ConversationAbortOptions(TypedDict, total=False):
    """``Conversation.abort()`` 的选项（``types.ts:111-119``）。"""

    #: 跨越后台边界：忽略后台标志标记**每一个**被触及的活任务，撤回被触及会话里排队的输入，
    #: 并等到那些任务终局、会话按常规空闲。此后创建的后台工作既不被标记也不被等待。
    background: bool


# --------------------------------------------------------------------------------------
# 批次 D：任务契约（``types.ts:93-105``）
# --------------------------------------------------------------------------------------


class SettledTask(TypedDict, total=False):
    """已终局的任务记录（``types.ts:93-96``）。"""

    state: Required[dict[str, Any]]


class AnyTask(TypedDict, total=False):
    """注册表里存的**已擦除**可执行任务定义（``types.ts:98-109``）。

    ``initial`` / ``phases`` / ``abort`` / ``migrate`` 在运行期是函数或映射；
    这里给出的是「注册表只需要知道它存在」的形状，具体类型由各任务自己的定义约束。
    """

    definition: Required[dict[str, Any]]


# --------------------------------------------------------------------------------------
# 批次 D：工具执行契约（``types.ts:176-199``）
# --------------------------------------------------------------------------------------


class NestedToolExecutionResult(TypedDict, total=False):
    """一次**嵌套**工具调用的结果（``types.ts:176-194``）。

    与 :class:`ToolExecutionResult` 的差别：它带 ``taskId``（这次嵌套调用自己的工具任务），
    而 ``structuredOutput`` 的含义取决于**这次调用的工具**有没有 ``structuredOutputSchema``：

    * **有**：是它校验过的 ``structuredOutput``（出错但仍有该字段时也带）；否则缺失，
      错误文本在 ``diagnostics`` 里；
    * **没有**：是它**有界**的输出（成功或失败都算）——单个文本项时是字符串、
      单个图片时是 ``ImageContent``、没有内容时是 ``""``、否则是内容列表；
    * Harness **自己写的**错误结果（不可用 / 参数非法 / 被拦 / 被打断 / 被中止）上缺失，
      由 ``diagnostics`` 说明出了什么事。
    """

    taskId: Required[int]
    isError: Required[bool]
    diagnostics: Required[list[ToolDiagnostic]]
    structuredOutput: NotRequired[JsonValue]
    details: NotRequired[JsonValue]
    usage: NotRequired[Any]
    #: 产出该结果的那一次尝试的执行时长；**从未执行**时缺失。
    durationMs: NotRequired[float]


#: 一次边界放置同一种模式里**多少**排队项：第一个，还是全部（``types.ts:199``）。
QueueMode: TypeAlias = Literal["all", "one-at-a-time"]


# --------------------------------------------------------------------------------------
# 批次 D：压缩契约（``types.ts:521-527``）
# --------------------------------------------------------------------------------------

#: 为什么压缩（``types.ts:521``）。
CompactionReason: TypeAlias = Literal["manual", "threshold", "overflow"]


class CompactionResult(TypedDict, total=False):
    """一次压缩的结果（``types.ts:527``）。"""

    entryId: EntryId
    submissionId: SubmissionId


# --------------------------------------------------------------------------------------
# 批次 D：上下文视图（``types.ts:628-637``）
# --------------------------------------------------------------------------------------


class ContextView(TypedDict):
    """一个对话的已提交上下文的只读视图（``types.ts:628-637``）。

    四个字段都是**派生结果**，不是持久化状态——它们由 ``context.py`` 从已提交条目算出：

    * ``head``：最新的适用 head 标记（没有就是 ``None``）；
    * ``entries``：**原始**活动条目——head 标记本身，后面跟着从它的 ``head`` 到 tail 的**非 head** 条目；
    * ``contributions``：与 ``entries`` 一一对应，每个条目在**应用编辑、剔除被排除的 stop reason 之后**、
      **工具结果排序之前**的模型消息；
    * ``messages``：下一次提供方请求要用的模型上下文（已排序、已前导 system）。
    """

    head: EntryRecord | None
    entries: list[EntryRecord]
    contributions: list[list[Any]]
    messages: list[Any]


# --------------------------------------------------------------------------------------
# 批次 D：agent 契约（``types.ts:405-520``）
# --------------------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    """一个对话已存储的 agent 选择；**名字，不是对象**。未设的字段跟随宿主（``types.ts:405-428``）。"""

    model: ModelRef
    thinkingLevel: Any  # pi_ai 的 ModelThinkingLevel
    #: 数组表示**就是这些**扩展、按序；对象表示在宿主的默认选择上编辑。
    extensions: Any  # list[str] | {"add": [...], "remove": [...]}
    #: 过滤已选扩展的工具：这是**启用**的工具集。
    tools: Any  # list[str] | {"remove": [...]}
    #: 过滤「模型可调用」的启用工具：这是**提供给**它的工具集。**从不加宽**。
    modelTools: Any  # list[str] | {"remove": [...], "add": [...]}
    #: 渲染在每个扩展 section 之后，section key 是 ``instructions``。
    instructions: str
    #: 环境文件系统里的目录，传给 ``HarnessOptions.env``。
    cwd: str


class AgentChange(TypedDict, total=False):
    """对 ``pi.agent`` 的一次改动（``types.ts:429-444``）。

    给定字段**替换**已存的；``None`` **清除**它；**不设**则什么都不改。
    """

    model: Any
    thinkingLevel: Any
    extensions: Any
    tools: Any
    modelTools: Any
    instructions: Any
    cwd: Any


class ConversationRetryPolicy(TypedDict):
    """一次请求尝试**内部**的重试（``types.ts:490-496``）。"""

    enabled: bool
    maxRetries: int
    baseDelayMs: float
    #: 上游是 ``maxAgentDelayMs?``——**可选**。
    maxAgentDelayMs: NotRequired[float]


class CompactionPolicy(TypedDict):
    """自动压缩阈值（spec §8.7）；**手动**压缩忽略 ``enabled``（``types.ts:498-512``）。"""

    #: 阈值触发与溢出触发的压缩。
    enabled: bool
    #: 给回答留出的空间：上下文超过 ``contextWindow - reserveTokens`` 时 generation 阻塞去压缩。
    reserveTokens: int
    #: 摘要**逐字保留**的近期上下文的大致大小。
    keepRecentTokens: int
    #: 后台压缩在阻塞阈值**以下** ``backgroundTokens`` 处开始；``0`` 表示关掉。
    backgroundTokens: int


class ProgressPolicy(TypedDict):
    """运行中的进度多久提交一次（``types.ts:513-520``）。

    每次进度提交都是一次存储写入；存储远端的宿主可以少提交几次，
    代价是实时回答与工具输出会以更大的步子出现。
    """

    #: 正在生成的回答两次提交之间的**最小**间隔。
    partialIntervalMs: float
    #: 运行中的工具输出两次提交之间的**最小**间隔；大块提交还会按其大小成比例地停。
    outputIntervalMs: float


# --------------------------------------------------------------------------------------
# 批次 D：注册表与提示词契约（``types.ts:349-403``）
# --------------------------------------------------------------------------------------


class DocumentReader(Protocol):
    """已提交的文档读（``types.ts`` 的 ``DocumentReader`` = ``Pick<Session, "snapshot" | "snapshotAsOf">``）。

    ⚠️ **按完整形状定义**（见 PORTING.md **R28**）：两个方法的上游形状都是已知的，
    虽然当前消费者（``agent.ts``）一个都不用。
    实现上 ``snapshot_as_of``（``Session.snapshotAsOf``）**尚未落地**——
    那是**实现缺口**，不是形状未知。
    """

    def snapshot(self, token: Any, *args: Any) -> Any:
        """读一个已提交文档的值；不存在返回 ``None``。"""
        ...

    def snapshot_as_of(self, token: Any, *args: Any) -> Any:
        """``snapshot`` 的 as-of 版本：读某个条目时点上的值。"""
        ...


class PromptInput(TypedDict):
    """一次请求准备时，system 提示 section 渲染的输入（``types.ts:336-347``）。"""

    conversationId: int
    #: 本次请求的解析结果；``agent.tools`` 是这次请求**提供**的工具。
    agent: Any  # Agent[Tool]
    #: 由 ``HarnessOptions.env`` 为这次准备构建；没有环境时是 ``None``。
    env: Any  # ExecutionEnv | None
    #: 重放活动 transcript 之后**已经生效**的 section。
    shown: dict[str, str]
    #: 已提交的文档读。
    read: DocumentReader


class PromptSection(TypedDict, total=False):
    """一个 system 提示 section；agent 的 section 在每次请求前**按序**渲染（``types.ts:349-355``）。"""

    key: Required[str]
    #: ``render(input, context) -> str | None``（可以是协程）。
    render: Required[Any]
    #: 默认 ``True``：把文本包成 ``<key>\n...\n</key>``。
    tag: bool


class RegistrySnapshot(Protocol):
    """某个已发布注册表状态的**不可变**视图（``types.ts:377-387``）。

    ⚠️ **按上游完整形状定义 6 个方法**（见 PORTING.md **R28**）：
    ``agent.ts`` 只用 ``installed()`` / ``extension()``，但完整形状是**已知的**，
    截断会得到一个 ``registry.py`` 必然推翻的形状。
    """

    def installed(self) -> Any:
        """已安装的扩展。"""
        ...

    def extension(self, name: str) -> Any:
        """按名字取扩展。"""
        ...

    def tools(self) -> Any:
        """每个已安装工具及其扩展，**按安装序**。名字可以在不同扩展间重复。"""
        ...

    def sections(self) -> Any:
        """每个已安装 section 及其扩展。"""
        ...

    def tasks(self) -> Any:
        """内建与已安装的任务定义。"""
        ...

    def task(self, name: str) -> Any:
        """按名字取任务定义。"""
        ...


# --------------------------------------------------------------------------------------
# 批次 D：会话流与已解析设置（``types.ts:477-568``）
# --------------------------------------------------------------------------------------


class ConversationStreamOptions(TypedDict, total=False):
    """一次提供方请求的流式选项（``types.ts:477-489``）。

    ⚠️ ``transport`` 的取值是上游的**字面量联合**（``ai/src/types.ts:144``）：
    ``"sse" | "websocket" | "websocket-cached" | "auto"``。
    **pi_ai 目前没有导出 ``Transport`` 这个名字**，所以这里写内联字面量，
    而不是在别的包的范围里造一个同名类型——形状是精确的，只是没有名字。
    """

    transport: Literal["sse", "websocket", "websocket-cached", "auto"]
    timeoutMs: float
    #: 一次请求尝试**内部**的提供方/SDK 重试。
    maxRetries: int
    maxRetryDelayMs: float
    headers: dict[str, str]
    metadata: dict[str, Any]
    cacheRetention: Any  # pi_ai 的 CacheRetention
    deferred: Any  # bool | {"window": "15m" | "1h" | "24h"}


class HarnessSettings(TypedDict, total=False):
    """**宿主**给的设置：每一项都可以缺，缺了就用内建默认（``types.ts:530-553``）。

    ``retry`` / ``compaction`` / ``progress`` 是**部分**覆盖。
    """

    #: 默认扩展选择；缺省表示**每一个**已安装扩展，按安装序。
    extensions: list[Any]  # list[Extension]
    stream: ConversationStreamOptions
    retry: Any  # Partial[ConversationRetryPolicy]
    compaction: Any  # Partial[CompactionPolicy]
    progress: Any  # Partial[ProgressPolicy]
    toolExecution: Any  # ToolExecutionMode
    steeringMode: Any  # QueueMode
    followUpMode: Any  # QueueMode
    #: 一个空闲对话把**最后一次上下文读**在内存里留多久，好让下一次运行只读更新的条目。
    #: 忙碌的对话总是留着；``0`` 表示一空闲就丢。
    contextRetentionMs: float
    #: 一个**没有** watch / document state 观察的已加载文档，在最后一次读写后留多久。
    #: 观察中的文档总是留着。默认十分钟；``0`` 一用完就丢；``Infinity`` 一直留。
    #: 其它非毫秒数的值表示**用默认**。
    documentRetentionMs: float


class Settings(TypedDict, total=False):
    """**已解析**的设置：每一项都在内建默认之上，对象字段是**合并**过的（``types.ts:554-568``）。

    与 :class:`HarnessSettings` 的区别：这里除 ``extensions`` 外都是**必填**（已解析）。
    本包把它整体写成 ``total=False`` 是因为 ``extensions`` 仍然可选；
    字段的必填性由 ``resolve_settings()`` 保证，不由类型强制——**这是有意的**，
    与上游的 ``Settings``（必填字段 + 一个可选）在运行期等价。
    """

    #: 缺省表示**每一个**已安装扩展，按安装序。
    extensions: list[Any]  # list[Extension]
    stream: ConversationStreamOptions
    retry: ConversationRetryPolicy
    compaction: CompactionPolicy
    progress: ProgressPolicy
    toolExecution: Any  # ToolExecutionMode
    steeringMode: Any  # QueueMode
    followUpMode: Any  # QueueMode
    contextRetentionMs: float
    documentRetentionMs: float


# --------------------------------------------------------------------------------------
# 批次 D：钩子分发与对话句柄（``types.ts:158-166``、``harness/types.ts:127-135``）
# --------------------------------------------------------------------------------------


class HookRunner(Protocol):
    """把一个钩子分发给**每一个**匹配的已注册处理器，按阶段快照的注册表顺序
    （``types.ts:157-166``）。"""

    async def each(self, name: str, invoke: Any) -> None:
        """对每个名为 ``name`` 的匹配处理器调用 ``invoke``。

        ⚠️ **``invoke`` 普通抛出会被报告，然后继续跑下一个处理器**；
        一旦**调用本身**被取消，错误就**向上传播**。
        **组合发生在 ``invoke`` 内部**——所以多个处理器是"叠"起来的，
        每个都可能改 ``arguments`` 或决定 ``block``。

        本包的 ``ToolTask`` 正是靠这个语义实现「第一个 block 生效、其余不再跑」
        （``tool.ts:139-151`` 的 ``if (block !== undefined) return;``）。
        """
        ...


class ConversationHandle(Protocol):
    """一个会话句柄（``harness/types.ts:127-135``）：给**任务**用的会话入口。

    与 ``Conversation``（给宿主的）的区别：句柄是**无状态**的，按 ``id`` 比较。
    """

    @property
    def id(self) -> int:
        """对话 ID。"""
        ...

    async def submit(self, submission: InputSubmissionDraft, context: Any) -> Any:
        """提交一份输入，返回它的 ``Submission``。"""
        ...

    async def abort(self, context: Any, options: Any = None) -> None:
        """``Conversation.abort()``：撤回排队中的输入、中止普通归属作用域、**等到它空闲**。"""
        ...

    async def wait_for_idle(self, context: Any) -> None:
        """等到该会话的普通归属作用域里**没有活着的非后台任务**。"""
        ...


class RegistryReader(Protocol):
    """注册表的**只读**面（``harness/types.ts:389-394``）。

    :class:`Registry` 在它之上再加安装/卸载；调度器只需要读的那面——
    它拿的是「当前的快照」，以及一个「有发布了就叫我」的订阅。

    ⚠️ **按完整形状定义**（R28 情形二）：调度器与 ``TaskRuntime`` 都要它，
    只写其中一个方法会得到一个会被下一个消费者推翻的形状。
    """

    def snapshot(self) -> RegistrySnapshot:
        """**整个**当前注册表的不可变视图。"""
        ...

    def subscribe(self, listener: Any) -> Any:
        """每次发布**之后同步**调用；返回一个退订函数。

        ⚠️ **同步**是语义的一部分：调度器靠它被唤醒去重新考虑被阻塞的任务，
        异步通知会让「发布了但没人被叫醒」变成一个时序 bug。
        """
        ...

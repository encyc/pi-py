"""transcript 归一化与 system message 回放。

对应上游 ``utils/transcript.ts``（v1.3.0，237 行）。

**这是唯一**会构造 :class:`~pi_ai.types.TranscriptContext` 的模块。

上游 v0.86.0 Breaking 变更：provider 面向的入参从 ``Context`` 改为归一化的
``TranscriptContext``，``systemPrompt`` / ``tools`` 折进 transcript 的首条
``SystemMessage``。公开入参 ``Context`` 形状不变，因此调用方无感。

本端在 v1.3.0 同步中移植了**骨架**（归一化 + 折叠 + 回放读取），但未启用
"会话中途 system 消息"特性本身——即本端不会有代码去构造带 ``sections`` /
``tools_added`` 的**后续** system message。回放逻辑完整实现，是为了：
1. 与上游 provider 契约一致（第三方按 v1.3.0 契约构造的 transcript 可直接工作）；
2. 使 ``collapse_system_messages`` 对首条 system message 的处理与上游逐字等价。
"""

from __future__ import annotations

import json
from typing import Any

from .text import content_text, get_system_message_text
from .types import (
    AssistantMessage,
    Context,
    Message,
    Model,
    SystemMessage,
    Tool,
    ToolReference,
    TranscriptContext,
)

__all__ = [
    "create_initial_system_message",
    "normalize_context",
    "ensure_transcript",
    "get_initial_system_message",
    "without_initial_system_message",
    "get_current_tools",
    "get_current_system_message",
    "get_current_system_prompt",
    "collapse_system_messages",
    "resolve_transcript",
    "get_declared_tools",
    "resolve_transcript_tools",
    "get_tool_state_changes",
    "has_non_additive_tool_changes",
    "has_tool_redefinitions",
    "to_tool_declaration",
    "declarations_equal",
    "assistant_is_same_model",
    "TranscriptTools",
    "ToolStateChanges",
]


def _is_system_message(message: Any) -> bool:
    """回放辅助函数只看 ``role``，因此兼容携带自定义 role 的 agent transcript。"""
    return getattr(message, "role", None) == "system"


def create_initial_system_message(
    system_prompt: str | None,
    tools: list[Tool] | None,
) -> SystemMessage | None:
    """构造首条 system message。两者皆空时返回 None（空 transcript 保持为空）。

    对应上游 ``createInitialSystemMessage``。注意 ``timestamp=0``：上游同样硬编码 0，
    使归一化结果与请求时刻无关（便于缓存命中与测试稳定）。
    """
    has_system_prompt = system_prompt is not None and len(system_prompt) > 0
    has_tools = tools is not None and len(tools) > 0
    if not has_system_prompt and not has_tools:
        return None
    message = SystemMessage(content=system_prompt or "", timestamp=0)
    if tools:
        message.tools_added = list(tools)
    return message


def normalize_context(context: Context) -> TranscriptContext:
    """把 ``Context.systemPrompt`` / ``Context.tools`` 折进首条 system message。

    对应上游 ``normalizeContext``。**本函数是 TranscriptContext 的唯一合法来源**，
    所有面向 provider 的函数都期望它的结果。
    """
    initial = create_initial_system_message(context.system_prompt, context.tools)
    messages: list[Message] = (
        [initial, *context.messages] if initial is not None else list(context.messages)
    )
    return TranscriptContext(messages)


def ensure_transcript(context: Context | TranscriptContext) -> TranscriptContext:
    """接受已归一化的 transcript 或裸 ``Context``，统一返回 transcript。

    上游用 unique symbol brand 让裸 ``Context`` **无法**流入 provider 代码；Python
    没有等价机制，而 ``Context`` 与 ``TranscriptContext`` 都有 ``messages`` 属性，
    直接传给 provider 会**静默丢掉** ``system_prompt`` 与 ``tools``（它们不在
    ``context.messages`` 里）。

    因此所有 provider 入口都先经本函数：已是 transcript 则原样返回，否则归一化。
    这样"直接调用 provider"与"经 ``stream()`` 调用"行为一致。
    """
    if isinstance(context, TranscriptContext):
        return context
    return normalize_context(context)


def get_initial_system_message(messages: list[Message]) -> SystemMessage | None:
    """返回首条 system message（若 transcript 以它开头）。"""
    first = messages[0] if messages else None
    return first if isinstance(first, SystemMessage) else None


def without_initial_system_message(messages: list[Message]) -> list[Message]:
    """丢弃首条 system message（供把 prompt 放在消息列表之外的 API 使用）。

    对应上游 ``withoutInitialSystemMessage``。
    """
    return messages[1:] if get_initial_system_message(messages) is not None else messages


def get_current_tools(messages: list[Message]) -> list[Tool]:
    """按顺序应用每条 transcript 工具增量后，解析出当前可用工具集。

    对应上游 ``getCurrentTools``：先删 ``tools_removed``，再加 ``tools_added``
    （同名覆盖），保持首次插入顺序。
    """
    tools: dict[str, Tool] = {}
    for message in messages:
        if not _is_system_message(message):
            continue
        assert isinstance(message, SystemMessage)
        for removed in message.tools_removed or []:
            tools.pop(removed.name, None)
        for added in message.tools_added or []:
            tools[added.name] = added
    return list(tools.values())


def get_current_system_message(messages: list[Message]) -> SystemMessage | None:
    """把所有 system message 回放成**一条**首部 system message。

    对应上游 ``getCurrentSystemMessage``：后续 ``content`` 追加到基础 prompt，
    ``sections`` 按名字打补丁，工具集用 :func:`get_current_tools` 解析。
    """
    content: list[str] = []
    sections: dict[str, str] = {}
    timestamp: int | None = None
    for message in messages:
        if not _is_system_message(message):
            continue
        assert isinstance(message, SystemMessage)
        if timestamp is None:
            timestamp = message.timestamp
        text = content_text(message.content)
        if text:
            content.append(text)
        for name, value in (message.sections or {}).items():
            if value is None:
                sections.pop(name, None)
            else:
                sections[name] = value
    tools = get_current_tools(messages)
    if timestamp is None and not tools:
        return None
    result = SystemMessage(content="\n\n".join(content), timestamp=timestamp or 0)
    if sections:
        result.sections = dict(sections)
    if tools:
        result.tools_added = tools
    return result


def get_current_system_prompt(messages: list[Message]) -> str:
    """回放全部 system message 后渲染出当前系统提示词文本。"""
    message = get_current_system_message(messages)
    return get_system_message_text(message) if message is not None else ""


def collapse_system_messages(context: TranscriptContext) -> TranscriptContext:
    """为**不支持**会话中途 system 消息的 API 重建 transcript。

    对应上游 ``collapseSystemMessages``：回放后的 system message 领衔，
    其后所有 system message 丢弃。
    """
    head = get_current_system_message(context.messages)
    messages = [m for m in context.messages if not _is_system_message(m)]
    return TranscriptContext([head, *messages] if head is not None else messages)


def resolve_transcript(
    context: TranscriptContext,
    supports_mid_convo_system_messages: bool | None,
) -> TranscriptContext:
    """模型原生支持会话中途 system 消息时保留原位，否则折叠。"""
    return context if supports_mid_convo_system_messages else collapse_system_messages(context)


def get_declared_tools(messages: list[Message]) -> list[Tool]:
    """transcript 工具状态引用到的全部定义，按**首次声明顺序**。

    对应上游 ``getDeclaredTools``。与 :func:`get_current_tools` 的区别：
    本函数保留被后续 ``tools_removed`` 撤销、或被同名重定义覆盖掉的旧定义。
    """
    definitions: dict[str, Tool] = {}
    for message in messages:
        if not _is_system_message(message):
            continue
        assert isinstance(message, SystemMessage)
        for tool in message.tools_added or []:
            definitions[tool.name] = tool
    return list(definitions.values())


def to_tool_declaration(tool: Tool) -> Tool:
    """剥离工具的**可执行/仅显示**字段，供 transcript 比较或持久化。

    对应上游 ``toToolDeclaration``：只保留 name/description/parameters/
    constrainedSampling，``parameters`` 经 JSON 往返深拷贝。
    """
    declaration = Tool(
        name=tool.name,
        description=tool.description,
        parameters=json.loads(json.dumps(tool.parameters)),
    )
    if tool.constrained_sampling is not None:
        declaration.constrained_sampling = tool.constrained_sampling
    return declaration


def _declaration_json(tool: Tool) -> str:
    """工具声明的规范化 JSON 文本（键排序，保证可比）。

    Pydantic 模型不能直接进 ``json.dumps``，故先 ``model_dump(by_alias=True)``；
    再对键排序，使字段定义顺序不同的等价声明也判等（比上游"依赖固定键序"更稳）。
    """
    return json.dumps(to_tool_declaration(tool).model_dump(by_alias=True), sort_keys=True)


def declarations_equal(left: Tool, right: Tool) -> bool:
    """两个工具向模型声明的接口是否相同。

    对应上游 ``declarationsEqual``：两侧先经 :func:`to_tool_declaration`，
    其 JSON 往返丢弃 ``undefined`` 字段，故比较序列化结果即精确。
    """
    return _declaration_json(left) == _declaration_json(right)


class TranscriptTools:
    """工具声明在"顶层请求字段"与"原位增量"之间的划分。"""

    __slots__ = ("request_tools", "anchors_additions")

    def __init__(self, request_tools: list[Tool], anchors_additions: bool) -> None:
        #: 放在顶层请求字段里发送的工具。
        self.request_tools = request_tools
        #: 后续 system message 是否把自身 ``tools_added`` 作为原位增量发送。
        #: 为 False 时 ``request_tools`` 已含完整当前工具集。
        self.anchors_additions = anchors_additions

    def __repr__(self) -> str:
        return f"TranscriptTools(request_tools={len(self.request_tools)}, anchors_additions={self.anchors_additions})"


def has_non_additive_tool_changes(messages: list[Message]) -> bool:
    """是否存在**仅追加型**传输无法回放的删除或同名重声明。

    对应上游 ``hasNonAdditiveToolChanges``。
    """
    declared: set[str] = set()
    for message in messages:
        if not _is_system_message(message):
            continue
        assert isinstance(message, SystemMessage)
        if message.tools_removed:
            return True
        for tool in message.tools_added or []:
            if tool.name in declared:
                return True
            declared.add(tool.name)
    return False


def has_tool_redefinitions(messages: list[Message]) -> bool:
    """是否存在同名但**定义不同**的工具重声明。

    对应上游 ``hasToolRedefinitions``。

    .. deprecated::
       上游标注 deprecated，理由："No built-in transport needs this anymore:
       Anthropic expresses redefinitions with inline ``tool_definition`` blocks.
       Kept for API compatibility and will be removed in a future release."
       本端保留仅为与上游 transcript 模块的公开面一致。
    """
    declared: dict[str, Tool] = {}
    for message in messages:
        if not _is_system_message(message):
            continue
        assert isinstance(message, SystemMessage)
        for tool in message.tools_added or []:
            previous = declared.get(tool.name)
            if previous is not None and not declarations_equal(previous, tool):
                return True
            declared[tool.name] = tool
    return False


class ToolStateChanges:
    """两组完整工具状态之间的增量。对应上游 ``ToolStateChanges``。"""

    __slots__ = ("tools_added", "tools_removed")

    def __init__(self, tools_added: list[Tool], tools_removed: list[ToolReference]) -> None:
        self.tools_added = tools_added
        self.tools_removed = tools_removed

    def __repr__(self) -> str:
        return (
            f"ToolStateChanges(tools_added={[t.name for t in self.tools_added]}, "
            f"tools_removed={[t.name for t in self.tools_removed]})"
        )


def get_tool_state_changes(previous: list[Tool], current: list[Tool]) -> ToolStateChanges:
    """比较两组完整工具状态。

    对应上游 ``getToolStateChanges``。**定义变化**视作"先删除、再添加"，
    因此同名但描述/schema 不同的工具会同时出现在两个列表里。
    """
    previous_tools = {tool.name: tool for tool in previous}
    current_tools = {tool.name: tool for tool in current}
    return ToolStateChanges(
        tools_added=[
            to_tool_declaration(tool)
            for tool in current
            if tool.name not in previous_tools
            or not declarations_equal(previous_tools[tool.name], tool)
        ],
        tools_removed=[
            ToolReference(name=tool.name)
            for tool in previous
            if tool.name not in current_tools
            or not declarations_equal(tool, current_tools[tool.name])
        ],
    )


def resolve_transcript_tools(
    messages: list[Message], supports_tool_additions: bool
) -> TranscriptTools:
    """把工具声明拆分到顶层请求字段与原位增量。

    对应上游 ``resolveTranscriptTools``。只有当传输支持原位追加、且历史上没有
    删除/同名重声明时才原位锚定；其余情况直接发送当前完整工具集。
    """
    initial = get_initial_system_message(messages)
    anchors = supports_tool_additions and not has_non_additive_tool_changes(messages)
    request_tools = (
        list(initial.tools_added or [])
        if anchors and initial is not None
        else get_current_tools(messages)
    )
    return TranscriptTools(request_tools, anchors)


def assistant_is_same_model(message: AssistantMessage, model: Model) -> bool:
    """assistant 消息是否由**同一个模型**产生。

    对应上游 ``api/transform-messages.ts:122`` 的 ``isSameModel``：三元组
    ``provider`` / ``api`` / ``model`` 全部相等。用于 v1.3.0 的跨模型思考丢弃
    （见 :mod:`pi_ai.transform_messages`）。
    """
    return (
        message.provider == model.provider
        and message.api == model.api
        and message.model == model.id
    )

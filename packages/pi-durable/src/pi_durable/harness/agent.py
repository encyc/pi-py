"""一个对话的 agent 状态 —— 移植 ``harness/agent.ts``（314 行）。

「agent」= 一次请求解析出来的**模型 + 扩展 + 工具集 + 提示词 section**。它有三层：

1. **持久状态**（``AgentDoc``，``pi.agent`` 文档）：会话**自己选的**东西——模型、
   thinking level、扩展选择、工具过滤、instructions、cwd。
2. **宿主设置**（``HarnessSettings`` → :func:`resolve_settings` 解析成 ``Settings``）：
   整个宿主共享的默认值与策略。
3. **注册表快照**（``RegistrySnapshot``）：当前装了哪些扩展与工具。

:func:`resolve_agent` 把三者合起来算出这次请求的 agent。**状态里存的是名字，不是对象**——
所以换一个注册表快照重新解析，就会得到按新注册表解析的结果。
"""

from __future__ import annotations

from typing import Any, cast

from pi_chord.json import copy_json
from pi_durable.documents import define_doc
from pi_durable.harness.types import (
    Agent,
    AgentChange,
    AgentState,
    HarnessSettings,
    RegistrySnapshot,
    Settings,
)
from pi_durable.session.session import document_retention

__all__ = [
    "DEFAULT_COMPACTION_POLICY",
    "DEFAULT_CONTEXT_RETENTION_MS",
    "DEFAULT_PROGRESS_POLICY",
    "DEFAULT_RETRY_POLICY",
    "INSTRUCTIONS_KEY",
    "AgentDoc",
    "add_tools",
    "agent_hooks",
    "apply_change",
    "configure",
    "create_agent",
    "resolve_agent",
    "resolve_settings",
]

#: 默认重试策略（``agent.ts:20-25``）。
DEFAULT_RETRY_POLICY: dict[str, Any] = {
    "enabled": True,
    "maxRetries": 3,
    "baseDelayMs": 2000,
    "maxAgentDelayMs": 60000,
}

#: 默认压缩策略（``agent.ts:27-32``）。
DEFAULT_COMPACTION_POLICY: dict[str, Any] = {
    "enabled": True,
    "reserveTokens": 16384,
    "keepRecentTokens": 20000,
    "backgroundTokens": 32768,
}

#: 默认进度策略（``agent.ts:34-38``）。
DEFAULT_PROGRESS_POLICY: dict[str, Any] = {
    "partialIntervalMs": 100,
    "outputIntervalMs": 100,
}

#: agent ``instructions`` 的保留 section key（``agent.ts:40``）。
INSTRUCTIONS_KEY = "instructions"

#: 默认 ``settings.contextRetentionMs``：十分钟（``agent.ts:46``）。
DEFAULT_CONTEXT_RETENTION_MS = 600_000


#: 内建的 agent 文档（``agent.ts:42-51``）。
#:
#: ⚠️ **``history: "rewindable"`` + ``fork: "asOf"``——分叉从「分叉点的 agent 状态」开始**。
#: 这与同族的另外三个文档**恰好相反**：
#:
#: ==================  =========================  ==========================
#: 文档                 语义                        分叉时的行为
#: ==================  =========================  ==========================
#: ``pi.provider``      ``fork: "initial"``        **全新**身份（沿用父的会被认成同一会话）
#: ``pi.usage``         ``fork: "initial"``        账本**从零**（子会话不背父的账）
#: ``pi.inbox``         ``fork: "initial"``        队列**从空**（排队输入属于那个对话）
#: **``pi.agent``**     **``fork: "asOf"``**       **沿用**分叉点的 agent（模型/扩展/工具选择要继承）
#: ==================  =========================  ==========================
#:
#: **不要为了「统一」把它改成 ``"initial"``**：子会话本该继承父会话的 agent 配置，
#: 否则每次分叉都要重新选模型与工具。
AgentDoc = define_doc(
    {
        "kind": "pi.agent",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda seed=None: {},
        "checkpointWhen": lambda value, ops, info: True,
    }
)


def resolve_settings(settings: HarnessSettings | None) -> Settings:
    """解析宿主设置：每个字段都在内建默认之上，对象字段**合并**（``agent.ts:57-78``）。

    ⚠️ **三处合并的语义不同，上游有意如此**：

    * ``retry`` / ``compaction`` 用**展开**（``{...default, ...partial}``）——
      显式给出的键**会覆盖**，哪怕是 ``undefined``；
    * ``progress`` 用**逐字段 ``??``**——上游注释明说
      "so an explicitly undefined interval keeps its default"。
    """
    result: dict[str, Any] = {}
    extensions = settings.get("extensions") if settings is not None else None
    if extensions is not None:
        result["extensions"] = extensions
    result["stream"] = dict((settings or {}).get("stream") or {})
    # 展开：显式给出的键覆盖默认（含 None）
    result["retry"] = {**DEFAULT_RETRY_POLICY, **((settings or {}).get("retry") or {})}
    result["compaction"] = {
        **DEFAULT_COMPACTION_POLICY,
        **((settings or {}).get("compaction") or {}),
    }
    # 逐字段 `??`：显式给出的 None **不**覆盖默认
    progress = (settings or {}).get("progress") or {}
    result["progress"] = {
        "partialIntervalMs": progress.get("partialIntervalMs")
        if progress.get("partialIntervalMs") is not None
        else DEFAULT_PROGRESS_POLICY["partialIntervalMs"],
        "outputIntervalMs": progress.get("outputIntervalMs")
        if progress.get("outputIntervalMs") is not None
        else DEFAULT_PROGRESS_POLICY["outputIntervalMs"],
    }
    result["toolExecution"] = (settings or {}).get("toolExecution") or "parallel"
    result["steeringMode"] = (settings or {}).get("steeringMode") or "one-at-a-time"
    result["followUpMode"] = (settings or {}).get("followUpMode") or "one-at-a-time"
    result["contextRetentionMs"] = (
        (settings or {}).get("contextRetentionMs")
        if (settings or {}).get("contextRetentionMs") is not None
        else DEFAULT_CONTEXT_RETENTION_MS
    )
    # ⚠️ 同族的 `documentRetention()`：非法值（负数 / 非数字 / None）一律退回默认。
    result["documentRetentionMs"] = document_retention((settings or {}).get("documentRetentionMs"))
    return cast(Settings, result)


async def configure(tx: Any, conversation_id: int, change: AgentChange) -> None:
    """对 ``pi.agent`` 施加一次改动（``agent.ts:81-84``）。

    **给定字段替换已存的、``None`` 清除它、不设则什么都不改**——这是**三态**
    （见 PORTING.md **R29**）。
    """
    state = await tx.doc(AgentDoc, conversation_id)
    apply_change(state, change)


def apply_change(state: Any, change: AgentChange) -> None:
    """三态写入：不设 → 不动；``None`` → 删键；有值 → 写入（``agent.ts:109-152``）。"""

    draft = cast(Any, state)
    pending = cast(dict[str, Any], change)

    def put(key: str, value: Any) -> None:
        if key not in pending:
            return  # `undefined`：什么都不改
        if pending[key] is None:
            del draft[key]  # `null`：清除
        else:
            draft[key] = value

    model = change.get("model")
    put("model", None if model is None else dict(model))
    put("thinkingLevel", change.get("thinkingLevel"))
    put("extensions", _selection(change.get("extensions")))
    put("tools", _filter(change.get("tools")))
    put("modelTools", _filter(change.get("modelTools")))
    put("instructions", change.get("instructions"))
    put("cwd", change.get("cwd"))


def _selection(value: Any) -> Any:
    """扩展选择的三种形状：数组 → 名字列表；对象 → ``{add?, remove?}`` 的名字列表（``agent.ts:124-137``）。"""
    if value is None:
        return None
    if _is_list(value):
        return _names(value)
    edited: dict[str, Any] = {}
    if value.get("add") is not None:
        edited["add"] = _names(value["add"])
    if value.get("remove") is not None:
        edited["remove"] = _names(value["remove"])
    return edited


def _filter(value: Any) -> Any:
    """工具过滤的两种形状：数组 → 名字列表；对象 → ``{remove}`` / ``{remove?, add?}``
    （``agent.ts:138-151``）。"""
    if value is None:
        return None
    if _is_list(value):
        return _names(value)
    filtered: dict[str, Any] = {}
    if "remove" in value and value["remove"] is not None:
        filtered["remove"] = _names(value["remove"])
    if "add" in value and value["add"] is not None:
        filtered["add"] = _names(value["add"])
    return filtered


def _is_list(value: Any) -> bool:
    """``Array.isArray(value)`` 的对应物。

    ⚠️ **必须额外看 chord ``Draft`` 的 ``is_list``**：``Tx.doc()`` 返回的是 Draft，
    而 Draft **对 ``isinstance(list)`` 不可见**（它是个包装对象）。
    JS 侧没有这个问题——``Draft`` 是 Proxy，``Array.isArray`` 对它是**可见**的。

    漏掉这一步的后果：数组形状会被当成对象形状，走进 ``value.get("remove")`` 那条路，
    报 ``TypeError: list indices must be integers, not str``。
    """
    if isinstance(value, (list, tuple)):
        return True
    return getattr(value, "is_list", False) is True


def _names(items: Any) -> list[str]:
    return [item["name"] if isinstance(item, dict) else item.name for item in items]


async def add_tools(tx: Any, conversation_id: int, added: list[str]) -> None:
    """把一轮工具调用的 ``addTools`` 施加到 ``tools`` 与 ``modelTools`` 上
    （``agent.ts:90-107``）。

    数组形状：缺的名字**追加**；``{remove}`` 形状：**去掉**这些名字。
    未设的 ``tools`` 已经启用了每个工具，所以**不写**它。
    未设或对象形状的 ``modelTools`` 还会把名字追加到它的 ``add``——
    那会**加载延迟工具**，对其余工具没有效果。

    **按名字、不经注册表**：现在没注册的工具（比如正在重连的 MCP 服务器上的）
    等它再注册上时就会被提供。
    """
    state = await tx.doc(AgentDoc, conversation_id)

    # ⚠️ **不能用 `x in draft`**：chord 的 `Draft.__contains__` 对**列表**做的下标记检查
    # （`0 in arr` → True，`"read" in arr` → False），这是**忠实镜像 JS**——
    # JS 里 `in` 作用在数组上也是查下标，值查找要用 `Array.includes`。
    # 所以这里先把草稿**物化成普通 list** 再做成员判断。
    tools = state.get("tools")
    if _is_list(tools):
        present = list(tools)
        for name in added:
            if name not in present:
                present.append(name)
                tools.append(name)
    elif tools is not None:
        removed = list(tools.get("remove") or [])
        if any(name in added for name in removed):
            state["tools"] = {"remove": [name for name in removed if name not in added]}

    model_tools = state.get("modelTools")
    if _is_list(model_tools):
        present_models = list(model_tools)
        for name in added:
            if name not in present_models:
                present_models.append(name)
                model_tools.append(name)
        return
    # 整体赋值；等值的文档写入不会记录改动。
    kept = [name for name in list((model_tools or {}).get("remove") or []) if name not in added]
    appended = list((model_tools or {}).get("add") or [])
    for name in added:
        if name not in appended:
            appended.append(name)
    state["modelTools"] = {**({"remove": kept} if kept else {}), "add": appended}


async def create_agent(tx: Any, conversation: Any) -> None:
    """每个**新建或分叉**会话的提交里，内建的那一部分（``agent.ts:155-171``）。

    三种情形：

    * 分叉（有 ``parent``）→ **什么都不做**：``AgentDoc`` 的 ``fork: "asOf"``
      已经让存储层把分叉点的值复制过来了；
    * 新的**任务拥有**的会话 → 复制它**拥有者任务那个会话**已存的 agent；
    * 新的**无主**会话 → 从空开始。
    """
    if conversation.get("parent") is not None:
        return
    agent = await tx.doc(AgentDoc, conversation["id"])
    owner = conversation.get("owner")
    if owner is None:
        return
    owner_agent = await tx.doc(AgentDoc, owner["conversationId"])
    # 上游是 `Object.assign(agent, copyJson(owner))`：**整体覆盖**。
    # 这里逐个删掉现有键再写入，效果相同（chord 的草稿支持 `del`）。
    for key in list(agent.keys()):
        del agent[key]
    for key, value in cast(dict[str, Any], copy_json(owner_agent.to_json())).items():
        agent[key] = value


def agent_hooks(agent: Agent[Any], task_name: str) -> list[Any]:
    """选中扩展里匹配 ``task_name`` 的钩子处理器，**按扩展顺序**（``agent.ts:173-183``）。"""
    handlers: list[Any] = []
    for extension in agent.extensions:
        for hook in getattr(extension, "hooks", ()) or ():
            if hook["task"] == task_name:
                handlers.append(hook["handlers"])
    return handlers


def resolve_agent(
    state: AgentState | None,
    snapshot: RegistrySnapshot,
    settings: Settings,
    report: Any,
) -> Agent[Any]:
    """从**已存状态**、一个注册表快照与已解析设置解析出 agent（``agent.ts:185-238``）。

    抛出或改名的包装器会**丢掉它的目标**并被报告；没有目标的包装器什么都不做。
    """
    stored = state or {}
    extensions = _select_extensions(stored.get("extensions"), snapshot, settings)

    composed: dict[str, Any] = {}
    for extension in extensions:
        for tool in extension.tools or ():
            composed[tool.name] = tool
    sections: dict[str, Any] = {}
    for extension in extensions:
        for section in extension.sections or ():
            sections[section["key"]] = section
    for extension in extensions:
        for wrap in extension.wraps or ():
            if "tool" in wrap:
                _apply_wrap(
                    composed,
                    wrap["tool"],
                    lambda tool, w=wrap: w["wrap"](tool),
                    lambda tool: tool.name,
                    report,
                )
            else:
                _apply_wrap(
                    sections,
                    wrap["section"],
                    lambda section, w=wrap: w["wrap"](section),
                    lambda section: section["key"],
                    report,
                )

    enabled = _filter_tools(list(composed.values()), stored.get("tools"))
    model_callable = [tool for tool in enabled if tool.callers is None or "model" in tool.callers]
    tools = _offered_tools(model_callable, stored.get("modelTools"))
    # ⚠️ **按对象身份**，不按值：JS 的 `Set` 用引用相等，而 Python 的 `set(dataclass)`
    # 会去哈希字段值——`ToolRegistration.parameters` 是 dict，一哈希就
    # `TypeError: unhashable type: 'dict'`。用 `id()` 才与上游等价。
    offered = {id(tool) for tool in tools}
    deferred = [
        tool for tool in model_callable if tool.deferred is True and id(tool) not in offered
    ]
    callable_tools = [tool for tool in enabled if tool.callers is None or "tools" in tool.callers]

    instructions = stored.get("instructions")
    agent_sections = list(sections.values())
    if instructions is not None:
        agent_sections.append({"key": INSTRUCTIONS_KEY, "render": lambda *_: instructions})

    return Agent(
        model=stored.get("model"),
        thinking_level=stored.get("thinkingLevel") or "off",
        extensions=extensions,
        tools=tools,
        callable=callable_tools,
        deferred=deferred,
        sections=agent_sections,
        instructions=instructions,
        cwd=stored.get("cwd"),
    )


def _offered_tools(tools: list[Any], filter_: Any) -> list[Any]:
    """提供给模型的工具，如 ``AgentState.modelTools`` 所述（``agent.ts:241-254``）。"""
    if _is_list(filter_):
        return _filter_tools(tools, filter_)
    edit = filter_ or {}
    removed = set(edit.get("remove") or [])
    kept = [tool for tool in tools if tool.deferred is not True and tool.name not in removed]
    # 已加载的延迟工具**按加载序**跟随，所以加载一个就是往声明的工具后面追加。
    by_name = {tool.name: tool for tool in tools}
    loaded: list[Any] = []
    for name in dict.fromkeys(edit.get("add") or []):
        tool = by_name.get(name)
        if tool is not None and tool.deferred is True and name not in removed:
            loaded.append(tool)
    return [*kept, *loaded]


def _filter_tools(tools: list[Any], filter_: Any) -> list[Any]:
    """``tools`` 经过一个已存过滤：数组**就是**它的名字、按它的顺序；``{remove}`` 去掉名字
    （``agent.ts:257-266``）。"""
    if filter_ is None:
        return tools
    if _is_list(filter_):
        by_name = {tool.name: tool for tool in tools}
        result: list[Any] = []
        for name in dict.fromkeys(filter_):
            tool = by_name.get(name)
            if tool is not None:
                result.append(tool)
        return result
    removed = set(filter_.get("remove") or [])
    return [tool for tool in tools if tool.name not in removed]


def _select_extensions(stored: Any, snapshot: RegistrySnapshot, settings: Settings) -> list[Any]:
    """选中的已安装扩展：已存数组，或**在宿主默认选择上编辑**（``agent.ts:269-292``）。"""
    if _is_list(stored):
        selected = list(stored)
    else:
        settings_extensions = settings.get("extensions")
        base = (
            [extension.name for extension in settings_extensions]
            if settings_extensions
            else [extension.name for extension in snapshot.installed()]
        )
        edit = stored or {}
        removed = set(edit.get("remove") or [])
        selected = [name for name in [*base, *(edit.get("add") or [])] if name not in removed]

    extensions: list[Any] = []
    for name in dict.fromkeys(selected):
        extension = snapshot.extension(name)
        if extension is not None:
            extensions.append(extension)
    return extensions


def _apply_wrap(
    items: dict[str, Any],
    target: str,
    wrap: Any,
    name_of: Any,
    report: Any,
) -> None:
    """对一个目标套用包装器（``agent.ts:294-312``）。

    目标不存在 → 什么都不做；包装器抛出、或**改了名** → **丢掉目标**并报告。
    """
    item = items.get(target)
    if item is None:
        return
    try:
        wrapped = wrap(item)
        if name_of(wrapped) != target:
            raise RuntimeError(f"Wrapper renamed {target} to {name_of(wrapped)}")
        items[target] = wrapped
    except BaseException as error:
        del items[target]
        report(error)

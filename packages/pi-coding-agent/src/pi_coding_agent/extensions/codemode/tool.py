"""``codemode`` 工具的**纯函数部分**（Tier-B④-1a）。

对应上游 ``extensions/codemode/tool.ts``(411) 的**纯函数切片**：声明渲染、描述生成、
目录预算选择。**不含** loadout 与工具定义（那些是 ④-1b）。

上游文件头注释说明了这个工具做什么：模型写 JavaScript 调用其它工具；脚本用 ``tools`` /
``ALL_TOOLS`` / ``text()`` / ``image()`` / ``exit()`` / ``store()`` / ``load()`` / ``console.*`` /
``return``，可通过 ``models.*`` 到达模型目录、分类器与图像模型。

**有意偏离**

* **删掉 renderer 接线**（上游 ``tool.ts:37,391`` 的 ``...codemodeRenderers``）：``renderer.ts``(156)
  依赖 ``pi-tui``，本包无渲染宿主 → **判定不做**（见 ``PORTING.md`` 的偏离索引（丙））。
* ``CodemodeJsonSchema`` 直接用 pi-codemode 的类型；上游用 typebox 的 ``Type.Object`` ——
  本端工具参数本来就是普通 JSON Schema ``dict``（与 ``tools.py`` / MCP 一致）。
* ``describe_globals`` 的 ``models`` 那行指向 :data:`CODEMODE_DOCS_PATH` —— 该文档**已随包入库**
  （见 ``PORTING.md`` 的缺口一），有测试断言它真的存在且非空。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from pi_codemode import CODEMODE_SOURCE_GRAMMAR, CodemodeJsonSchema
from pi_codemode.declarations import (
    render_tool_output_type,
    render_tool_sample,
    to_codemode_identifier,
)

from ...config import get_docs_path
from ...extensions.types import ToolDefinition, ToolLoadout, ToolLoadoutChanges, ToolNamespace
from ...settings_defaults import SETTINGS_DEFAULTS
from ...settings_schema import CodemodeMode

#: 工具名（上游 ``CODEMODE_TOOL_NAME``）。
CODEMODE_TOOL_NAME = "codemode"

#: 保存一次脚本 ``store()`` 写入的自定义条目类型（上游 ``CODEMODE_STORE_ENTRY_TYPE``）。
CODEMODE_STORE_ENTRY_TYPE = "codemode-store"


class CodemodeStoreEntryData(TypedDict):
    """一条 ``codemode-store`` 条目的负载（对应上游 ``CodemodeStoreEntryData``）。"""

    set: dict[str, Any]
    delete: list[str]


#: 没有 ``outputSchema`` 的工具，其调用解析成字符串。
TEXT_OUTPUT_SCHEMA: CodemodeJsonSchema = {"type": "string"}

#: ``codemode`` 的输入 schema（上游 typebox ``Type.Object({ code: Type.String({...}) })``）。
#: typebox 对非可选属性会补 ``required``，这里保持一致。
CODEMODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"code": {"type": "string", "description": "Raw JavaScript source."}},
    "required": ["code"],
}

#: 嵌套调用的状态（上游 ``CodemodeNestedCallStatus``）。
CodemodeNestedCallStatus = Literal["running", "ok", "error", "cancelled"]


@dataclass
class CodemodeNestedCall:
    """脚本发起的一次嵌套工具调用（上游 ``CodemodeNestedCall``）。"""

    #: 嵌套调用的工具调用 id，形如 ``<codemode 调用 id>/<n>``。
    id: str
    name: str
    #: 参数的紧凑 JSON，为展示截断。
    args: str
    status: CodemodeNestedCallStatus
    duration_ms: float | None = None
    #: 错误文本，为展示截断。
    error: str | None = None
    #: 报告了用量的 ``models.*`` 调用的美元成本。
    cost: float | None = None


@dataclass
class CodemodeToolDetails:
    """``codemode`` 工具的结构化详情（上游 ``CodemodeToolDetails``）。"""

    calls: list[CodemodeNestedCall] = field(default_factory=list)
    #: 输出被截断时，存放完整文本输出的临时文件。
    full_output_path: str | None = None


#: 系统提示贡献（上游 ``codemodeToolSystemPromptContribution``，``as const``）。
CODEMODE_TOOL_SYSTEM_PROMPT_CONTRIBUTION: dict[str, Any] = {
    "snippet": "Run JavaScript that calls other tools",
    "guidelines": [
        "Use codemode to batch independent tool calls (Promise.allSettled), chain them, "
        "or filter large output, instead of many separate calls."
    ],
}

#: 脚本的参考手册路径（上游 ``join(getDocsPath(), "codemode.md")``）。
CODEMODE_DOCS_PATH = os.path.join(get_docs_path(), "codemode.md")

DESCRIPTION_INTRO = """Run JavaScript that calls other tools. The input is raw JavaScript (not JSON, no code fence), run as an async function body in a QuickJS sandbox: top-level `await` and `return` work. No Node, file system, network, or timers.
- `await tools.<name>({ ...args })` resolves to a string, or an object if the tool's declaration says so, and rejects with an Error on failure. A tool whose declaration returns an object resolves on failure too when it returned that object; check its fields. Calls still running when the script ends are cancelled.
- Optional first line: `// @options: {"max_output_tokens": 10000, "timeout_ms": 60000}``"""


def describe_globals(models: bool) -> str:
    """每个全局一行；细节在 :data:`CODEMODE_DOCS_PATH`（上游 ``describeGlobals``）。"""
    lines = [
        "Globals:",
        "- `text(value)`, `image(dataUrlOrImageBlock)`, `console.log(...)`, and top-level `return` "
        "add output; `exit()` ends the script. With several text items, each starts with a "
        "`==> text N/M <==` line, and `console` lines follow the other output in one "
        "`<console_output>` block. `image()` also saves the image to a temp file and the result "
        "names its path.",
        "- `store(key, value)` and `load(key)` keep JSON values across codemode calls.",
        "- `ALL_TOOLS`, `await searchTools(query, { limit?, namespace? })`, `await describeTool(name)`, "
        "`await describeNamespace(name)`: find unlisted tools, such as MCP tools.",
    ]
    if models:
        lines.append(
            f"- `models`: classifiers and image generation. Read {CODEMODE_DOCS_PATH} first."
        )
    return "\n".join(lines)


#: :attr:`CodemodeDescriptionOptions.inline_budget` 的默认值（估算 token 数）。
DEFAULT_CODEMODE_INLINE_BUDGET: int = SETTINGS_DEFAULTS["codemode"]["inlineBudget"]
#: 估算工具段落成本时，每个 token 折合多少字符。
CHARS_PER_TOKEN = 4


def to_codemode_declaration(
    tool: ToolDefinition,
    guidelines: Sequence[str] = (),
) -> dict[str, Any]:
    """脚本看到的一个工具：描述后跟它的 prompt guidelines。

    系统提示只为**已声明**的工具保留 guidelines。没有输出 schema 的工具解析成文本输出。

    返回 **snake_case** 键（``name`` / ``description`` / ``input_schema`` / ``output_schema``），
    因为 :func:`pi_codemode.declarations.render_tool_sample` 读的就是这个形状（上游是 camelCase 的
    ``Omit<CodemodeTool, "execute">``）。
    """
    bullets = [f"- {guideline.strip()}" for guideline in guidelines if guideline.strip()]
    description = tool.description.strip()
    if bullets:
        description = f"{description}\n\n" + "\n".join(bullets)
    return {
        "name": tool.name,
        "description": description,
        "input_schema": tool.parameters,
        "output_schema": tool.output_schema or TEXT_OUTPUT_SCHEMA,
    }


def get_codemode_callable_tools(tools: Iterable[ToolDefinition]) -> list[ToolDefinition]:
    """脚本可调用的工具：给定的全部工具，**除去 codemode 自己**。"""
    return [tool for tool in tools if tool.name != CODEMODE_TOOL_NAME]


@dataclass
class CodemodeDescriptionOptions:
    """``create_codemode_description`` 的选项（上游 ``CodemodeDescriptionOptions``）。"""

    #: 声明 ``models`` 命名空间；只给「带模型访问」创建的工具。
    models: bool = False
    #: 每个工具所属的 namespace，按工具名索引。同一 namespace 的工具列在一个标题下。
    namespaces: Mapping[str, ToolNamespace] | None = None
    #: 可调用但**永不列出**声明的工具（``deferred`` 曝光）。
    deferred: frozenset[str] | None = None
    #: 每个工具的 prompt guidelines，按工具名索引，列在它的描述之后。
    guidelines: Mapping[str, Sequence[str]] | None = None
    #: 工具段落可用的估算 token 数（字符 / 4）。放不下的工具被略去，同 ``deferred``。
    #: 未设置则列出每个非 deferred 工具。
    inline_budget: int | None = None


def _render_tool_section(declaration: dict[str, Any]) -> str:
    """``### \\`id\\` (\\`raw name\\`)`` 后跟工具的描述与声明。"""
    raw_name = str(declaration["name"])
    identifier = to_codemode_identifier(raw_name)
    heading = (
        f"### `{identifier}`" if identifier == raw_name else f"### `{identifier}` (`{raw_name}`)"
    )
    return f"{heading}\n{render_tool_sample(declaration).strip()}"


@dataclass
class _CatalogEntry:
    name: str
    section: str
    cost: int


@dataclass
class _CatalogGroup:
    namespace: ToolNamespace | None
    entries: list[_CatalogEntry] = field(default_factory=list)


def _select_catalog(groups: Sequence[_CatalogGroup], budget: int | None) -> set[str]:
    """挑选放得进预算的工具段落（上游 ``selectCatalog``，同 OpenCode 的目录算法）。

    每一轮里，每个分组（先是无 namespace 的，再按名字排 namespace）放上它**最便宜**的剩余工具；
    某个分组的下一个放不下时它退出，其它分组继续。任何 namespace 被**完整**列出之前，
    每个 namespace 都已经至少有一个代表。
    """
    if budget is None:
        return {entry.name for group in groups for entry in group.entries}
    queues = [sorted(group.entries, key=lambda entry: entry.cost) for group in groups]
    shown: set[str] = set()
    remaining = budget
    active = [queue for queue in queues if queue]
    while active:
        kept: list[list[_CatalogEntry]] = []
        for queue in active:
            next_entry = queue[0]
            if next_entry.cost > remaining:
                continue
            remaining -= next_entry.cost
            shown.add(next_entry.name)
            queue.pop(0)
            if queue:
                kept.append(queue)
        active = kept
    return shown


def create_codemode_description(
    tools: Sequence[ToolDefinition],
    options: CodemodeDescriptionOptions | None = None,
) -> str:
    """面向模型的描述：帮助列表、找未列出工具的指引、``models`` API，以及每个列出工具一段。

    按 namespace 分组。``deferred`` 工具**永不列出**、也**不影响**描述，因此后来注册的工具
    （例如 MCP server 连上时）不会改变它。工具段落受 ``inline_budget`` 限制。
    """
    opts = options or CodemodeDescriptionOptions()
    guidelines = opts.guidelines or {}
    declarations = [
        to_codemode_declaration(tool, guidelines.get(tool.name, ()))
        for tool in get_codemode_callable_tools(tools)
        if not (opts.deferred is not None and tool.name in opts.deferred)
    ]
    groups: dict[str, _CatalogGroup] = {"": _CatalogGroup(namespace=None)}
    for declaration in declarations:
        namespace = (opts.namespaces or {}).get(str(declaration["name"]))
        key = f"ns:{namespace.name}" if namespace is not None else ""
        group = groups.get(key)
        if group is None:
            group = _CatalogGroup(namespace=namespace)
            groups[key] = group
        section = _render_tool_section(declaration)
        group.entries.append(
            _CatalogEntry(
                name=str(declaration["name"]),
                section=section,
                cost=math.ceil(len(section) / CHARS_PER_TOKEN),
            )
        )

    # 无 namespace 的组排在最前，其余按 namespace 名排序。
    ordered = sorted(
        groups.values(),
        key=lambda group: (0, "") if group.namespace is None else (1, group.namespace.name),
    )
    shown = _select_catalog(ordered, opts.inline_budget)

    sections = [DESCRIPTION_INTRO, describe_globals(opts.models)]
    if not declarations:
        return "\n\n".join(sections)

    tool_sections = ["Nested tools:"]
    for group in ordered:
        visible = [entry for entry in group.entries if entry.name in shown]
        if group.namespace is not None:
            # 只有**放不进预算**的工具才算「未列出」。
            if len(visible) == len(group.entries):
                listing = ""
            elif not visible:
                listing = " (tools not listed)"
            else:
                listing = " (some tools not listed)"
            description = (group.namespace.description or "").strip()
            # 反斜杠不能出现在 f-string 的表达式里（Python 3.11），先算好。
            suffix = "\n" + description if description else ""
            tool_sections.append(f"## {group.namespace.name}{listing}{suffix}")
        tool_sections.extend(entry.section for entry in visible)
    sections.append("\n\n".join(tool_sections))
    return "\n\n".join(sections)


def _describe_fields(schema: object, depth: int) -> str | None:
    """对象 schema 的字段名，形如 ``{ output, exit_code, full_output_path? }``。

    ``depth`` 层深内的对象型字段会展开。非对象 schema 返回 ``None``。
    """
    if not isinstance(schema, dict):
        return None
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        return None
    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    fields = []
    for name, field_schema in schema["properties"].items():
        nested = _describe_fields(field_schema, depth - 1) if depth > 0 else None
        suffix = "" if name in required_names else "?"
        fields.append(f"{name}{suffix}{f': {nested}' if nested else ''}")
    return f"{{ {', '.join(fields)} }}"


def _describe_output(schema: CodemodeJsonSchema | None) -> str:
    """一次脚本调用解析成什么，一行说清（上游 ``describeOutput``）。"""
    rendered = render_tool_output_type(schema)
    if rendered == "string":
        return "a string"
    fields = _describe_fields(schema, 1)
    return f"`{fields if fields is not None else ' '.join(rendered.split())}`"


def describe_script_call(tool: ToolDefinition) -> str:
    """已声明工具的描述，后跟脚本怎么调用它、解析成什么（上游 ``describeScriptCall``）。

    参数就是工具已声明的参数，因此不重复。
    """
    output = _describe_output(to_codemode_declaration(tool)["output_schema"])
    identifier = to_codemode_identifier(tool.name)
    return (
        f"{tool.description.strip()}\n\nCodemode: `tools.{identifier}(args)` resolves to {output}."
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "CODEMODE_DOCS_PATH",
    "CODEMODE_SCHEMA",
    "CODEMODE_STORE_ENTRY_TYPE",
    "CODEMODE_TOOL_NAME",
    "CODEMODE_TOOL_SYSTEM_PROMPT_CONTRIBUTION",
    "DEFAULT_CODEMODE_INLINE_BUDGET",
    "DESCRIPTION_INTRO",
    "TEXT_OUTPUT_SCHEMA",
    "DISCOVERY_GLOBALS",
    "CodemodeDescriptionOptions",
    "CodemodeToolOptions",
    "CodemodeNestedCall",
    "CodemodeNestedCallStatus",
    "CodemodeStoreEntryData",
    "CodemodeToolDetails",
    "create_codemode_description",
    "create_codemode_tool",
    "create_codemode_tool_definition",
    "describe_globals",
    "describe_script_call",
    "get_codemode_callable_tools",
    "get_script_reach",
    "prepare_codemode_loadout",
    "to_codemode_declaration",
]


# ============================================================
# ④-1b-1：loadout 与其可达性判定
# ============================================================
#
# ⚠️ 上游同一切片的 ``createCodemodeToolDefinition`` / ``createCodemodeTool`` **尚未移植**：
# 它们的 ``execute`` 调 ``(await loadCodemodeExecutor()).executeCodemode(...)``，而
# ``loadCodemodeExecutor`` 就是 ``import("./execute.ts")`` —— 那是 **④-2**。
# 本包的 ``ToolDefinition`` 是 **ABC**（``execute`` 为 ``@abstractmethod``），
# 因此**没有 execute 就构造不出实例**：这不是"暂时留个桩"，是根本建不出来。
# 所以那两个函数留到 ④-2 之后（④-1b-2），此处**不导出**它们。


@dataclass
class CodemodeToolOptions:
    """``codemode`` 工具的选项（上游 ``CodemodeToolOptions``）。"""

    #: 某工具所属的 namespace，供 ``searchTools()`` 排序与 ``namespace`` 过滤。
    get_tool_namespace: Any = None
    #: 每个工具的 prompt guidelines，按工具名索引；``describeTool()`` 与 ``ALL_TOOLS`` 会一起展示。
    get_tool_guidelines: Any = None
    #: 把 ``models`` 命名空间暴露给脚本（由会话的 model registry 支撑）。
    #: 不给则 ``models`` 不会被声明。
    models: bool = False
    #: 把 ``store()`` 写入持久化成会话自定义条目。不给则只在当前脚本内有效；
    #: ``load()`` 仍能读到分支上已有的条目。
    append_entry: Any = None
    #: 工具活跃时如何呈现 loadout（``codemode.mode`` 设置）。默认 ``"on"``。
    get_mode: Any = None
    #: 描述里工具声明的 token 预算。默认 :data:`DEFAULT_CODEMODE_INLINE_BUDGET`。
    get_inline_budget: Any = None


#: 脚本里会「列出或查找工具」的全局名 —— 用到它们就说明脚本可能触达**任意**工具。
#:
#: 上游用 ``/\b(searchTools|describeNamespace|describeTool|ALL_TOOLS)\b/``。这里显式加
#: ``re.ASCII``：JS 的 ``\w`` 只含 ``[A-Za-z0-9_]``，而 Python 的 ``\w`` 默认是 Unicode 的，
#: 不加会把中文标识符也算进去，得到与上游不同的可达集。
DISCOVERY_GLOBALS = re.compile(r"\b(searchTools|describeNamespace|describeTool|ALL_TOOLS)\b")


def get_script_reach(code: object) -> Literal["all"] | list[str]:
    """脚本可调用的工具（对应 ``ToolDefinition.reachesTools``）。

    用了上面那几个发现类全局名 → ``"all"``；否则返回源码里的**词**，
    于是 ``tools.mcp__docs__search`` 与 ``tools["mcp__docs__" + name]`` 都能点名 ``mcp__docs``
    这个 namespace。非字符串输入返回空列表。
    """
    if not isinstance(code, str):
        return []
    if DISCOVERY_GLOBALS.search(code):
        return "all"
    return re.findall(r"\w+", code, re.ASCII)


def prepare_codemode_loadout(
    loadout: ToolLoadout,
    options: CodemodeToolOptions | None = None,
) -> ToolLoadoutChanges:
    """``codemode`` 工具如何呈现「既已声明、又能被脚本调用」的工具（上游 ``prepareCodemodeLoadout``）。

    * ``on``：这些工具的描述里说明脚本怎么调用它们，而 codemode 的描述**只列**没有 ``direct``
      曝光的可调用工具。
    * ``only``：codemode 的描述列出**每个**可调用工具，且请求里**略去**活跃 ``direct`` 工具的声明。

    被列出的工具带上它们的 prompt guidelines（系统提示只为已声明工具保留这些）。

    **按曝光而不是按活跃集合来列**，这样 ``tool_search`` 载入一个工具时 codemode 的描述不变，
    载入也就不会重新声明 codemode。

    **失效类偏离（直接报错）**：上游 ``ToolLoadout`` 是**方法**
    （``getExposure`` / ``getNamespace`` / ``getPromptGuidelines``），本包是**映射**
    （``exposures`` / ``namespaces`` / ``prompt_guidelines``）——照抄会 ``AttributeError``。
    """
    opts = options or CodemodeToolOptions()
    mode: CodemodeMode = (
        opts.get_mode() if opts.get_mode is not None else SETTINGS_DEFAULTS["codemode"]["mode"]
    )

    def is_direct(tool: ToolDefinition) -> bool:
        return loadout.exposures.get(tool.name) == "direct"

    callable_tools = get_codemode_callable_tools(loadout.callable)
    callable_names = {tool.name for tool in callable_tools}
    descriptions: dict[str, str] = {}
    if mode == "on":
        for tool in loadout.declared:
            if tool.name in callable_names:
                descriptions[tool.name] = describe_script_call(tool)

    listed = (
        callable_tools
        if mode == "only"
        else [tool for tool in callable_tools if not is_direct(tool)]
    )
    namespaces = {
        tool.name: loadout.namespaces[tool.name]
        for tool in listed
        if tool.name in loadout.namespaces
    }
    guidelines = {tool.name: loadout.prompt_guidelines.get(tool.name, ()) for tool in listed}
    inline_budget = opts.get_inline_budget() if opts.get_inline_budget is not None else None
    descriptions[CODEMODE_TOOL_NAME] = create_codemode_description(
        listed,
        CodemodeDescriptionOptions(
            models=opts.models is True,
            namespaces=namespaces,
            guidelines=guidelines,
            deferred=frozenset(
                tool.name for tool in listed if loadout.exposures.get(tool.name) == "deferred"
            ),
            inline_budget=inline_budget
            if inline_budget is not None
            else DEFAULT_CODEMODE_INLINE_BUDGET,
        ),
    )
    declared_names = {tool.name for tool in loadout.declared}
    hidden = (
        [tool.name for tool in callable_tools if is_direct(tool) and tool.name in declared_names]
        if mode == "only"
        else []
    )
    return ToolLoadoutChanges(descriptions=descriptions, hidden_declarations=hidden)


# ============================================================
# ④-1b-2：工具定义与 AgentTool
# ============================================================
#
# 形状照 **MCP 同目录先例**（``extensions/mcp/tools.py`` 的函数内局部 ``ToolDefinition`` 子类）：
# 本包的 ``ToolDefinition`` 是 ABC，``execute`` 必须实现，所以用局部具体子类而不是返回字面量。
# **不包层**：上游 ``createCodemodeTool`` 里那次 ``wrapToolDefinition`` 已判**不做**
# （纯转换，pi-py 的工具本就「直接作为 AgentTool 使用而不再包一层」，见 PORTING 偏离索引（丙））。


def create_codemode_tool_definition(options: CodemodeToolOptions | None = None) -> ToolDefinition:
    """``codemode`` 工具的 ``ToolDefinition``（上游 ``createCodemodeToolDefinition``，``tool.ts:371``）。

    ``description`` 用**空工具列表**生成；工具真正活跃时由 ``prepare_loadout`` 换成带声明的版本。
    """
    opts = options or CodemodeToolOptions()

    class _CodemodeTool(ToolDefinition):
        #: 有能力的模型把脚本当**原文**写，而不是 JSON 转义后的字符串。
        #: （本仓 ``ToolDefinition`` 不声明该字段 —— 见其「有意偏离」，这里就地补上。）
        constrained_sampling: dict[str, Any]

        async def execute(
            self,
            tool_call_id: str,
            params: dict[str, Any],
            cancel_event: Any = None,
            on_update: Any = None,
            ctx: Any = None,
        ) -> Any:
            # 沙箱（worker + QuickJS wasm）在**首次调用**时才加载，不是启动时（上游经
            # `execute.lazy.ts` 懒加载；本端在 execute 里才 import `.execute`）。
            from .execute import execute_codemode

            return await execute_codemode(tool_call_id, params, cancel_event, on_update, ctx, opts)

    definition = _CodemodeTool(
        name=CODEMODE_TOOL_NAME,
        label=CODEMODE_TOOL_NAME,
        description=create_codemode_description(
            [], CodemodeDescriptionOptions(models=opts.models is True)
        ),
        parameters=CODEMODE_SCHEMA,
        prompt_snippet=CODEMODE_TOOL_SYSTEM_PROMPT_CONTRIBUTION["snippet"],
        prompt_guidelines=list(CODEMODE_TOOL_SYSTEM_PROMPT_CONTRIBUTION["guidelines"]),
        # 脚本**不能**启动别的脚本，所以只向模型曝光。
        exposure="model-only",
        reaches_tools=lambda payload: get_script_reach(_code_of(payload)),
        prepare_loadout=lambda loadout: prepare_codemode_loadout(loadout, opts),
    )
    definition.constrained_sampling = {
        "type": "grammar",
        "variants": {"openai_lark": CODEMODE_SOURCE_GRAMMAR},
    }
    return definition


def create_codemode_tool(
    tools: Sequence[ToolDefinition] = (),
    options: CodemodeToolOptions | None = None,
) -> ToolDefinition:
    """``codemode`` 工具，描述里列出给定的工具（上游 ``createCodemodeTool``，``tool.ts:399``）。

    脚本能调用的是**执行时** agent 循环提供的工具，所以这里只换 ``description``
    （上游另外 ``Object.assign`` 回 ``promptSnippet`` / ``promptGuidelines`` —— 那两个本来就一样）。
    """
    opts = options or CodemodeToolOptions()
    definition = create_codemode_tool_definition(opts)
    description = create_codemode_description(
        list(tools), CodemodeDescriptionOptions(models=opts.models is True)
    )
    # 不包层：直接把 description 写到同一个定义上（上游是 `Object.assign(tool, {...})`）。
    definition.description = description
    return definition


def _code_of(payload: Any) -> Any:
    """从工具入参里取 ``code``（``reachesTools`` 的入参是脚本入参）。"""
    if isinstance(payload, Mapping):
        return payload.get("code")
    return getattr(payload, "code", None)

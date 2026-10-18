"""system 提示 section 的渲染与规划 —— 移植 ``harness/prompt.ts``（158 行）。

两件事：

1. **重放**：把 transcript 里已提交的 system 消息按序叠起来，得到「当前生效」的 section 集合
   （:func:`replay_sections`）——因为提供方只认**打头的** system 消息，
   后续变更要靠 ``sections`` 这种可重放的**差量**来表达。
2. **规划**：算出要写哪些 ``pi.system`` 条目，使重放出来的 section 与工具
   **在值与顺序上都**等于期望（:func:`plan_system_entries`）。
"""

from __future__ import annotations

from typing import Any, cast

from pi_ai.transcript import declarations_equal, get_current_tools, to_tool_declaration
from pi_chord.context import Context
from pi_durable.entries import SystemEntry
from pi_durable.harness.types import ContextView, PromptInput, PromptSection

__all__ = [
    "plan_system_entries",
    "replay_sections",
    "render_sections",
]


def _field(message: Any, name: str, default: Any = None) -> Any:
    """取一条消息的字段，**同时**接受 JSON 记录（dict）与 pydantic 模型——见 PORTING.md **R26**。"""
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def replay_sections(messages: list[Any]) -> dict[str, str]:
    """按序重放 system 消息之后**生效**的 section（``prompt.ts:9-19``）。

    规则：**就地设置**、``None`` **删除**、**再添加则追加到末尾**（顺序变了）。
    """
    shown: dict[str, str] = {}
    for message in messages:
        if _field(message, "role") != "system":
            continue
        sections = _field(message, "sections")
        if sections is None:
            continue
        for key, value in cast(dict[str, Any], sections).items():
            if value is None:
                shown.pop(key, None)
            else:
                # ⚠️ **已存在的键保持原位**：JS 的 `Map.set` 更新值但不改位置，
                # 而 Python 的 `d[k] = v` 对已存在的键同样保持插入顺序——两者一致。
                # 这里**不能**先 pop 再赋值（那会把它挪到末尾，改变顺序），
                # 只有「先删后加」才真的追加到末尾。
                shown[key] = value
    return shown


async def render_sections(
    sections: list[PromptSection],
    input: PromptInput,
    shown: Any,
    report: Any,
    context: Context,
) -> dict[str, str]:
    """按序渲染 agent 的 section（``prompt.ts:26-51``）。

    * ``None`` → **省略**这个 section；
    * 带标签的文本包成 ``<key>\\n...\\n</key>``（``tag`` 默认 ``True``）；
    * **抛出的 section 保留它已显示的文本**（如果有）并被报告；
    * ``context`` 已被取消之后**再**抛出的错误**向上传播**（不再吞掉）。
    """
    desired: dict[str, str] = {}
    for section in sections:
        key = section["key"]
        try:
            text = section["render"](input, context)
            if hasattr(text, "__await__"):
                text = await text
        except BaseException as error:
            # 调用方**已经取消**：不再吞，直接往上抛。
            if context.abort_signal is not None and context.abort_signal.aborted:
                raise
            report(error)
            kept = shown.get(key) if hasattr(shown, "get") else None
            if kept is not None:
                desired[key] = kept
            continue
        if text is None:
            continue
        desired[key] = text if section.get("tag") is False else f"<{key}>\n{text}\n</{key}>"
    return desired


def plan_system_entries(
    view: ContextView,
    desired: dict[str, str],
    tools: list[Any],
    timestamp: float,
) -> list[dict[str, Any]]:
    """规划使 ``view`` 重放出来的 section 与工具等于 ``desired`` / ``tools`` 的 ``pi.system`` 条目
    （``prompt.ts:64-95``）。

    三种情形：

    1. **有 head 标记、且它之后没有 ``pi.system`` 条目** → 写**一个完整基线**，
       并 ``omit`` 掉所有被保留的更早 ``pi.system`` 条目（**即使它只是重述**重放值也要写，
       因为此刻需要的是「一个干净的起点」）；
    2. 否则，若**最小 patch 会留下不同的顺序** → **删掉全部已显示、再按序重加全部期望**；
    3. 否则就是**最小 patch**（只写改过的值与 ``null`` 删除），或者什么都不写。

    工具的变更**搭在最后一条规划出来的条目上**，或自己占一条。
    """
    head = view["head"]
    if head is not None and not any(
        SystemEntry.is_(entry) and entry["id"] > head["id"] for entry in view["entries"]
    ):
        edits = [
            {"target": entry["id"], "action": "omit"}
            for entry in view["entries"]
            if SystemEntry.is_(entry)
        ]
        baseline = _system_entry(
            dict(desired),
            {"toolsRemoved": [], "toolsAdded": [to_tool_declaration(tool) for tool in tools]},
            timestamp,
        )
        if not edits:
            return [baseline]
        return [{**baseline, "edits": edits}]

    sections = _plan_sections(replay_sections(view["messages"]), desired)
    changes = _plan_tools(get_current_tools(view["messages"]), tools)
    if not changes["toolsRemoved"] and not changes["toolsAdded"]:
        return [_system_entry(patch, None, timestamp) for patch in sections]
    if not sections:
        return [_system_entry(None, changes, timestamp)]
    return [
        _system_entry(patch, changes if index == len(sections) - 1 else None, timestamp)
        for index, patch in enumerate(sections)
    ]


def _plan_tools(offered: list[Any], desired: list[Any]) -> dict[str, list[Any]]:
    """从 ``offered`` 到 ``desired`` 的工具变更（``prompt.ts:103-125``）。

    **声明变了的工具会被移除再添加**。重放会把保留的工具留在原位、把新增的追加在后面；
    当那得不到期望的顺序时，**移除全部已提供的、再按序重加全部期望的**。
    """
    wanted = {tool.name: tool for tool in desired}
    kept = [
        tool
        for tool in offered
        if tool.name in wanted and declarations_equal(tool, wanted[tool.name])
    ]
    kept_names = {tool.name for tool in kept}
    added = [tool for tool in desired if tool.name not in kept_names]
    replayed = [*kept, *added]
    if any(tool.name != desired[index].name for index, tool in enumerate(replayed)) or len(
        replayed
    ) != len(desired):
        return {
            "toolsRemoved": [{"name": tool.name} for tool in offered],
            "toolsAdded": [to_tool_declaration(tool) for tool in desired],
        }
    return {
        "toolsRemoved": [{"name": tool.name} for tool in offered if tool.name not in kept_names],
        "toolsAdded": [to_tool_declaration(tool) for tool in added],
    }


def _plan_sections(shown: dict[str, str], desired: dict[str, str]) -> list[dict[str, Any]]:
    """section 的 patch：没有、最小 patch、或「全删 + 按序重加」（``prompt.ts:128-148``）。"""
    patched_order = [
        *[key for key in shown if key in desired],
        *[key for key in desired if key not in shown],
    ]
    desired_order = list(desired)
    if patched_order != desired_order:
        return [
            {key: None for key in shown},
            dict(desired),
        ]

    patch: dict[str, Any] = {}
    for key, value in shown.items():
        next_value = desired.get(key)
        if next_value != value:
            patch[key] = next_value  # `None` 表示删除
    for key, value in desired.items():
        if key not in shown:
            patch[key] = value
    return [] if not patch else [patch]


def _system_entry(
    sections: dict[str, Any] | None,
    tools: dict[str, list[Any]] | None,
    timestamp: float,
) -> dict[str, Any]:
    """一条 ``pi.system`` 条目的草稿（``prompt.ts:150-165``）。"""
    message: dict[str, Any] = {"role": "system", "content": "", "timestamp": timestamp}
    if sections is not None:
        message["sections"] = sections
    if tools is not None and tools["toolsRemoved"]:
        message["toolsRemoved"] = tools["toolsRemoved"]
    if tools is not None and tools["toolsAdded"]:
        message["toolsAdded"] = tools["toolsAdded"]
    return {"model": [message]}

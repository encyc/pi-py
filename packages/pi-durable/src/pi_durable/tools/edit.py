"""``edit`` 工具 —— 移植 ``durable/src/tools/edit.ts``（144 行）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pi_ai import TextContent
from pi_chord.context import Context
from pi_durable.env import FileError
from pi_durable.harness import ToolExecutionApi, ToolExecutionResult, ToolRegistration, define_tool
from pi_durable.tools.args import json_schema_of
from pi_durable.tools.edit_diff import (
    Edit,
    apply_edits_to_normalized_content,
    detect_line_ending,
    generate_diff_string,
    generate_unified_patch,
    normalize_to_lf,
    prepare_edit_arguments,
    restore_line_endings,
    strip_bom,
)
from pi_durable.tools.env import require_env
from pi_durable.tools.file_mutation_queue import with_file_mutation_queue
from pi_durable.tools.path_utils import resolve_tool_path

__all__ = [
    "EDIT_SCHEMA",
    "EditToolDetails",
    "EditToolInput",
    "ReplaceEditInput",
    "create_edit_tool",
    "prepare_edit_arguments",
]


class ReplaceEditInput(BaseModel):
    """一次定向替换（``edit.ts:19-25`` 的 ``replaceEditSchema``）。"""

    old_text: str = Field(
        alias="oldText",
        description=(
            "Exact text for one targeted replacement. It must be unique in the original file and "
            "must not overlap with any other edits[].oldText in the same call."
        ),
    )
    new_text: str = Field(alias="newText", description="Replacement text for this targeted edit.")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class EditToolInput(BaseModel):
    """``edit.ts:27-33`` 的 ``editSchema``。"""

    path: str = Field(description="Path to the file to edit (relative or absolute)")
    edits: list[ReplaceEditInput] = Field(
        description=(
            "One or more targeted replacements. Each edit is matched against the original file, "
            "not incrementally. Do not include overlapping or nested edits. If two changes touch "
            "the same block or nearby lines, merge them into one edit instead."
        )
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


EDIT_SCHEMA: dict[str, Any] = json_schema_of(EditToolInput)


class EditToolDetails(BaseModel):
    """``edit.ts:73-77`` 的 ``EditToolDetails``。"""

    model_config = {"populate_by_name": True}

    diff: str
    patch: str
    first_changed_line: int | None = Field(default=None, alias="firstChangedLine")


def _validate_edit_input(raw: EditToolInput | dict[str, Any]) -> tuple[str, list[Edit]]:
    """``edit.ts:79-84`` 的 ``validateEditInput``。"""
    edits = raw.edits if isinstance(raw, EditToolInput) else raw.get("edits")
    path = raw.path if isinstance(raw, EditToolInput) else raw.get("path")
    if not isinstance(edits, list) or len(edits) == 0:
        raise RuntimeError(
            "Edit tool input is invalid. edits must contain at least one replacement."
        )
    converted: list[Edit] = []
    for edit in edits:
        if isinstance(edit, Edit):
            converted.append(edit)
        elif isinstance(edit, dict):
            converted.append(Edit(old_text=str(edit["oldText"]), new_text=str(edit["newText"])))
        else:  # pydantic 模型
            converted.append(Edit(old_text=edit.old_text, new_text=edit.new_text))
    return str(path), converted


def _edit_access_error(path: str, error: FileError) -> RuntimeError:
    """``edit.ts:86-88``。"""
    return RuntimeError(f"Could not edit file: {path}. Error code: {error.code}.")


async def _execute(
    raw: Any,
    api: ToolExecutionApi,
    context: Context,
) -> ToolExecutionResult[Any]:
    path, edits = _validate_edit_input(raw)
    env = require_env(api)
    absolute_path = await resolve_tool_path(env, path, context)

    async def edit_file() -> ToolExecutionResult[Any]:
        if context.abort_signal is not None and context.abort_signal.aborted:
            raise RuntimeError("Operation aborted")

        info = await env.file_info(absolute_path, context)
        if not info.ok:
            raise _edit_access_error(path, info.error)
        if info.value.kind not in ("file", "symlink"):
            raise RuntimeError(f"Could not edit file: {path}. Path is not a file.")

        read_result = await env.read_text_file(absolute_path, context)
        if not read_result.ok:
            raise _edit_access_error(path, read_result.error)
        if context.abort_signal is not None and context.abort_signal.aborted:
            raise RuntimeError("Operation aborted")

        original = strip_bom(read_result.value)
        original_ending = detect_line_ending(original.text)
        normalized = normalize_to_lf(original.text)
        applied = apply_edits_to_normalized_content(normalized, edits, path)
        if context.abort_signal is not None and context.abort_signal.aborted:
            raise RuntimeError("Operation aborted")

        final_content = original.bom + restore_line_endings(applied.new_content, original_ending)
        write_result = await env.write_file(absolute_path, final_content, context)
        if not write_result.ok:
            raise _edit_access_error(path, write_result.error)
        if context.abort_signal is not None and context.abort_signal.aborted:
            raise RuntimeError("Operation aborted")

        diff_result = generate_diff_string(applied.base_content, applied.new_content)
        details = EditToolDetails(
            diff=diff_result.diff,
            patch=generate_unified_patch(path, applied.base_content, applied.new_content),
            firstChangedLine=diff_result.first_changed_line,
        )
        return ToolExecutionResult(
            output=[TextContent(text=f"Successfully replaced {len(edits)} block(s) in {path}.")],
            details=details,
        )

    return await with_file_mutation_queue(env, absolute_path, edit_file, context)


def create_edit_tool() -> ToolRegistration[Any]:
    """``createEditTool()``（``edit.ts:90-144``）。

    注意 ``prepare_arguments``：模型常把 ``edits`` 写成 JSON 字符串、写成单个对象，
    或在顶层直接给 ``oldText``/``newText``——这些都在这条纯函数里修好
    （``edit.ts:45-71``，由 ``test/tools.test.ts:375`` 直接测）。
    """
    return define_tool(
        name="edit",
        description=(
            "Edit a single file using exact text replacement. Every edits[].oldText must match a "
            "unique, non-overlapping region of the original file. If two changes affect the same "
            "block or nearby lines, merge them into one edit instead of emitting overlapping edits. "
            "Do not include large unchanged regions just to connect distant changes."
        ),
        parameters=EDIT_SCHEMA,
        prepare_arguments=prepare_edit_arguments,
        execute=_execute,
    )

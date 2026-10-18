"""``write`` 工具 —— 移植 ``durable/src/tools/write.ts``（39 行）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pi_ai import TextContent
from pi_chord.context import Context
from pi_durable.env import get_or_throw
from pi_durable.harness import ToolExecutionApi, ToolExecutionResult, ToolRegistration, define_tool
from pi_durable.tools.args import json_schema_of
from pi_durable.tools.env import require_env
from pi_durable.tools.file_mutation_queue import with_file_mutation_queue
from pi_durable.tools.path_utils import resolve_tool_path

__all__ = ["WRITE_SCHEMA", "WriteToolInput", "create_write_tool"]


# ``write.ts:9-14`` 的 ``writeSchema`` + ``Static<typeof writeSchema>``。
# 不给模型写 docstring：pydantic 会把它变成 schema 的 description，而 typebox 不会。
class WriteToolInput(BaseModel):
    path: str = Field(description="Path to the file to write (relative or absolute)")
    content: str = Field(description="Content to write to the file")


WRITE_SCHEMA: dict[str, Any] = json_schema_of(WriteToolInput)


async def _execute(
    args: WriteToolInput,
    api: ToolExecutionApi,
    context: Context,
) -> ToolExecutionResult[Any]:
    env = require_env(api)
    absolute_path = await resolve_tool_path(env, args.path, context)

    async def write() -> ToolExecutionResult[Any]:
        if context.abort_signal is not None and context.abort_signal.aborted:
            raise RuntimeError("Operation aborted")
        get_or_throw(await env.write_file(absolute_path, args.content, context))
        if context.abort_signal is not None and context.abort_signal.aborted:
            raise RuntimeError("Operation aborted")
        return ToolExecutionResult(output=[TextContent(text=f"Successfully wrote to {args.path}")])

    return await with_file_mutation_queue(env, absolute_path, write, context)


def create_write_tool() -> ToolRegistration[WriteToolInput]:
    """``createWriteTool()``（``write.ts:16-38``）。

    写入会被文件的改动队列串行化（``withFileMutationQueue``），并自动建父目录
    （由 ``env.write_file`` 保证，与上游一致）。
    """
    return define_tool(
        name="write",
        description=(
            "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. "
            "Automatically creates parent directories."
        ),
        parameters=WRITE_SCHEMA,
        execute=_execute,
    )

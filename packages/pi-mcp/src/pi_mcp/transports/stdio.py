"""stdio 传输：以换行分帧的 JSON-RPC 驱动一个子进程 MCP 服务端。

对应上游 ``packages/mcp/src/transports/stdio.ts``（v1.3.0，216 行）。

移植要点（也是上游注释里点明的坑）：

- **分帧**：服务端把每条 JSON-RPC 消息写成一行（``\\n`` 分隔）；解析时容忍 ``\\r\\n``，
  空行跳过。缓冲区超过 ``max_message_bytes`` 即报错并丢弃。
- **进程组**：POSIX 下子进程放进自己的进程组（``start_new_session``），关闭时按**负 pid**
  杀整组，否则 ``npx`` / ``uvx`` 这类包装器会留下真正的服务端进程。
- **退出钩子**：进程退出时兜底 SIGTERM 所有还活着进程组。
- **关闭时序**：先关 stdin 让服务端自己退出 → 宽限期后 SIGTERM → 再等 ``close_timeout_ms``
  后 SIGKILL。
- **stderr**：保留最近 ``max_stderr_bytes``（默认 64 KiB）供诊断，并逐块回调 ``on_stderr``。

偏离（见 ``PORTING.md``）：``cross-spawn`` → ``asyncio.create_subprocess_exec``；
``process.kill(-pid)`` → ``os.killpg``；``process.once("exit")`` → ``atexit``。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..protocol.jsonrpc import (
    JsonRpcMessage,
    McpConnectionClosedError,
    parse_json_rpc_message,
)
from .transport import (
    DEFAULT_MAX_MESSAGE_BYTES,
    McpTransportSendOptions,
    TransportEvents,
)

DEFAULT_MAX_STDERR_BYTES = 64 * 1024
DEFAULT_CLOSE_TIMEOUT_MS = 2_000
#: stdin 关闭后，服务端有多少时间自行退出，之后才发 SIGTERM。
STDIN_CLOSE_GRACE_MS = 500
_USE_PROCESS_GROUPS = sys.platform != "win32"

#: 还在运行的进程组；宿主未关闭就退出时兜底清理。
_live_process_groups: set[int] = set()
_exit_hook_installed = False


def _kill_process_tree(pid: int | None, sig: int) -> None:
    """杀整个进程树（对齐上游 ``killProcessTree``）。"""
    if sys.platform == "win32" and pid is not None:
        # Windows 没有优雅信号，且包装器（cmd.exe 的 .cmd shim）只杀直接子进程会留下服务端。
        from subprocess import DEVNULL, Popen  # noqa: S404 - 与上游 taskkill 等价

        with contextlib.suppress(OSError):
            Popen(  # noqa: S603
                ["taskkill", "/pid", str(pid), "/T", "/F"],
                stdout=DEVNULL,
                stderr=DEVNULL,
                stdin=DEVNULL,
            )
        return
    if _USE_PROCESS_GROUPS and pid is not None:
        try:
            # 负 pid：整组，包装器不会把服务端留下。
            os.killpg(pid, sig)
            return
        except OSError:
            pass  # 组不存在或从未创建；回退到直接子进程
    if pid is not None:
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


def _install_exit_hook() -> None:
    global _exit_hook_installed
    if _exit_hook_installed:
        return
    _exit_hook_installed = True

    def _cleanup() -> None:
        for pid in list(_live_process_groups):
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)

    atexit.register(_cleanup)


@dataclass
class StdioTransportOptions:
    """``StdioTransport`` 的配置。"""

    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    #: 默认继承宿主环境并与 ``env`` 合并；置 ``False`` 则只用 ``env``。
    inherit_env: bool = True
    #: ``"pipe"``（默认）把 stderr 收进缓冲区；``"inherit"`` 直接接到本进程。
    stderr: str = "pipe"
    on_stderr: Callable[[str], None] | None = None
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES
    #: 发 SIGTERM 后等子进程退出的时长，超时再 SIGKILL。默认 2000ms。
    close_timeout_ms: int = DEFAULT_CLOSE_TIMEOUT_MS


class StdioTransport(TransportEvents):
    """以子进程 stdin/stdout 承载换行分帧 JSON-RPC 的传输。"""

    def __init__(self, options: StdioTransportOptions) -> None:
        super().__init__()
        self.options = options
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._started = False
        self._closed = False
        self._exited = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    # ---- 只读属性 ----

    @property
    def pid(self) -> int | None:
        """子进程 pid（未启动为 ``None``）。"""
        return self._process.pid if self._process is not None else None

    @property
    def stderr(self) -> str:
        """至今为止保留的 stderr 尾部。"""
        return self._stderr_buffer.decode("utf-8", errors="replace")

    @property
    def started(self) -> bool:
        """是否已 ``start()``。"""
        return self._started

    @property
    def closed(self) -> bool:
        """是否已 ``close()``。"""
        return self._closed

    # ---- 生命周期 ----

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("MCP stdio transport already started")
        if self._closed:
            raise McpConnectionClosedError()
        self._started = True

        env = dict(os.environ) if self.options.inherit_env else {}
        env.update(self.options.env)

        stderr_target: Any = asyncio.subprocess.PIPE if self.options.stderr != "inherit" else None
        process = await asyncio.create_subprocess_exec(
            self.options.command,
            *self.options.args,
            cwd=self.options.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_target,
            # 自己的进程组，关闭时才能连子进程一起终止。
            start_new_session=_USE_PROCESS_GROUPS,
        )
        self._process = process
        pid = process.pid
        if _USE_PROCESS_GROUPS and pid is not None:
            _install_exit_hook()
            _live_process_groups.add(pid)

        self._tasks.append(asyncio.create_task(self._read_stdout()))
        if process.stderr is not None:
            self._tasks.append(asyncio.create_task(self._read_stderr()))
        self._tasks.append(asyncio.create_task(self._watch_exit()))

    async def send(
        self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None
    ) -> None:
        process = self._process
        stdin = process.stdin if process is not None else None
        if not self._started or self._closed or stdin is None or stdin.is_closing():
            raise McpConnectionClosedError()
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        stdin.write(payload)
        # 背压：写到管道缓冲满时等待排水（对应上游 stdin.write 的回调等待）。
        await stdin.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            self._emit_close()
            return
        if process.returncode is not None:
            # 已经退出：``_watch_exit`` 会收尾。
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            await self._exited.wait()
            return

        # 按规范关机：关 stdin 让服务端自行退出，然后 SIGTERM，再 SIGKILL。
        close_timeout_ms = self.options.close_timeout_ms
        grace = min(STDIN_CLOSE_GRACE_MS, close_timeout_ms)

        async def _escalate() -> None:
            await asyncio.sleep(grace / 1000)
            _kill_process_tree(process.pid, signal.SIGTERM)
            await asyncio.sleep(close_timeout_ms / 1000)
            _kill_process_tree(process.pid, signal.SIGKILL)

        escalator = asyncio.create_task(_escalate())
        try:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            await self._exited.wait()
        finally:
            escalator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await escalator
        # 忽略 stdin 关闭、留在组里的子进程，不能让它活得比服务端久。
        _kill_process_tree(process.pid, signal.SIGTERM)

    # ---- 内部：读取与收尾 ----

    async def _read_stdout(self) -> None:
        process = self._process
        stream = process.stdout if process is not None else None
        if stream is None:
            return
        while True:
            try:
                chunk = await stream.read(65_536)
            except (asyncio.CancelledError, OSError):
                return
            if not chunk:
                return
            self._handle_stdout(chunk)

    async def _read_stderr(self) -> None:
        process = self._process
        stream = process.stderr if process is not None else None
        if stream is None:
            return
        while True:
            try:
                chunk = await stream.read(65_536)
            except (asyncio.CancelledError, OSError):
                return
            if not chunk:
                return
            self._handle_stderr(chunk)

    async def _watch_exit(self) -> None:
        process = self._process
        if process is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await process.wait()
        if process.pid is not None:
            _live_process_groups.discard(process.pid)
        self._process = None
        if self._stdout_buffer.decode("utf-8", errors="replace").strip():
            self.emit_error(
                Exception("MCP stdio server closed with an incomplete JSON-RPC message")
            )
        self._stdout_buffer.clear()
        self._exited.set()
        self._emit_close()

    def _handle_stdout(self, chunk: bytes) -> None:
        self._stdout_buffer.extend(chunk)
        max_message_bytes = self.options.max_message_bytes
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline < 0:
                if len(self._stdout_buffer) > max_message_bytes:
                    self._stdout_buffer.clear()
                    self.emit_error(
                        Exception(f"MCP stdio message exceeds {max_message_bytes} bytes")
                    )
                return
            line = bytes(self._stdout_buffer[:newline])
            del self._stdout_buffer[: newline + 1]
            if len(line) > max_message_bytes:
                self.emit_error(Exception(f"MCP stdio message exceeds {max_message_bytes} bytes"))
                continue
            text = line.decode("utf-8", errors="replace")
            if text.endswith("\r"):
                text = text[:-1]
            if not text.strip():
                continue
            try:
                self._emit_message(parse_json_rpc_message(json.loads(text)))
            except Exception as error:  # noqa: BLE001 - 与上游一致：坏帧只上报
                self.emit_error(error)

    def _handle_stderr(self, chunk: bytes) -> None:
        max_stderr_bytes = self.options.max_stderr_bytes
        self._stderr_buffer.extend(chunk)
        if len(self._stderr_buffer) > max_stderr_bytes:
            del self._stderr_buffer[: len(self._stderr_buffer) - max_stderr_bytes]
        if self.options.on_stderr is not None:
            self.options.on_stderr(chunk.decode("utf-8", errors="replace"))


__all__ = [
    "DEFAULT_CLOSE_TIMEOUT_MS",
    "DEFAULT_MAX_STDERR_BYTES",
    "STDIN_CLOSE_GRACE_MS",
    "StdioTransport",
    "StdioTransportOptions",
]

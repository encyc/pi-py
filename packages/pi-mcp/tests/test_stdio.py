"""``StdioTransport`` 的行为规格 —— **真实子进程**，不 mock。

逐条对齐上游 ``packages/mcp/test/stdio.test.ts``（v1.3.0，64 行）+ 两个 fixtures
（``stdio-server.mjs`` / ``stubborn-server.mjs`` 的 Python 等价物）。

为什么必须跑真进程：stdio 传输最容易踩的坑全在进程边界上 —— 换行分帧、``\\r\\n``、
部分行、stderr 与 stdout 的分离、EOF 与信号、以及「包装器进程死了但服务端还活着」。
mock 一个 stdout 完全测不到这些。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from pi_mcp import (
    McpClient,
    McpClientOptions,
    McpConnectionClosedError,
    StdioTransport,
    StdioTransportOptions,
)

FIXTURES = Path(__file__).parent / "fixtures"
STDIO_SERVER = str(FIXTURES / "stdio_server.py")
STUBBORN_SERVER = str(FIXTURES / "stubborn_server.py")


def _transport(script: str, **kwargs: object) -> StdioTransport:
    return StdioTransport(
        StdioTransportOptions(command=sys.executable, args=(script,), **kwargs)  # type: ignore[arg-type]
    )


# ============================================================
# stdio.test.ts:9 「connects to a newline-delimited MCP server and captures stderr」
# ============================================================


async def test_connects_to_a_newline_delimited_mcp_server_and_captures_stderr() -> None:
    stderr: list[str] = []
    transport = _transport(STDIO_SERVER, on_stderr=stderr.append)
    client = McpClient(McpClientOptions(name="stdio-test", version="1.0.0"))

    await client.connect(transport)
    assert await client.list_tools() == [{"name": "echo", "inputSchema": {"type": "object"}}]
    assert await client.call_tool("echo", {"text": "hello"}) == {
        "content": [{"type": "text", "text": "hello"}]
    }
    assert isinstance(transport.pid, int)
    await asyncio.sleep(0.05)
    assert "stdio fixture ready" in "".join(stderr)
    assert "stdio fixture ready" in transport.stderr

    await client.close()
    assert client.connection_state == "closed"


# ============================================================
# stdio.test.ts:30 「kills a server that ignores shutdown, including its children」
# ============================================================


@pytest.mark.skipif(sys.platform == "win32", reason="上游同样跳过 Windows（无优雅信号）")
async def test_kills_a_server_that_ignores_shutdown_including_its_children() -> None:
    transport = _transport(STUBBORN_SERVER, close_timeout_ms=100)
    client = McpClient(McpClientOptions(name="stdio-test", version="1.0.0"))
    await client.connect(transport)

    grandchild: int | None = None
    for _ in range(100):
        match = None
        for line in transport.stderr.splitlines():
            if line.startswith("grandchild "):
                match = line
        if match is not None:
            grandchild = int(match.split()[1])
            break
        await asyncio.sleep(0.01)
    assert isinstance(grandchild, int)

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await client.close()
    # SIGTERM 无效 → 必须靠 SIGKILL 收场，但整体要快。
    assert loop.time() - started_at < 5.0

    alive = True
    for _ in range(100):
        if not alive:
            break
        try:
            os.kill(grandchild, 0)
            await asyncio.sleep(0.02)
        except OSError:
            alive = False
    assert alive is False


# ============================================================
# 本端补充：分帧、stderr 上限、生命周期
# ============================================================


async def test_crlf_and_blank_lines_are_tolerated() -> None:
    """分帧容错：``\\r\\n`` 当作 ``\\n``，空行跳过（上游 ``stdio.ts:199``）。"""
    script = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' not in msg: continue\n"
        "    out = json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{}})\n"
        "    sys.stdout.write('\\r\\n' + out + '\\r\\n\\n\\n')\n"
        "    sys.stdout.flush()\n"
    )
    path = FIXTURES / "_crlf_server.py"
    path.write_text(script, encoding="utf-8")
    try:
        transport = _transport(str(path))
        client = McpClient(McpClientOptions(name="t", version="1"))
        # initialize 由这个最小服务端回 {}，因此校验会失败；只验证帧被解析出来。
        errors: list[Exception] = []
        transport.on_error(errors.append)
        await transport.start()
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        received: list[object] = []
        transport.on_message(received.append)
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
        assert errors == []
        await client.close()
        await transport.close()
    finally:
        path.unlink(missing_ok=True)


async def test_malformed_line_is_reported_without_killing_the_transport() -> None:
    """坏帧只上报（上游 ``stdio.ts:203``），后续消息照常解析。

    两类坏帧都要覆盖：非 JSON（``JSON.parse`` 抛）与「是 JSON 但不是 JSON-RPC 消息」
    （``parseJsonRpcMessage`` 抛 ``McpError``）。
    """
    script = (
        "import json,sys\n"
        "sys.stdout.write('this is not json\\n')\n"
        "sys.stdout.write('{\"hello\":1}\\n')\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' not in msg: continue\n"
        "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{}})+'\\n')\n"
        "    sys.stdout.flush()\n"
    )
    path = FIXTURES / "_bad_frame_server.py"
    path.write_text(script, encoding="utf-8")
    try:
        transport = _transport(str(path))
        errors: list[Exception] = []
        received: list[object] = []
        transport.on_error(errors.append)
        transport.on_message(received.append)
        await transport.start()
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert len(errors) == 2
        assert "Expecting value" in str(errors[0])  # 非 JSON
        assert "Invalid JSON-RPC message" in str(errors[1])  # 是 JSON，但不是 JSON-RPC
        assert received == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
        await transport.close()
    finally:
        path.unlink(missing_ok=True)


async def test_oversized_message_is_rejected_and_the_buffer_dropped() -> None:
    """超过 ``max_message_bytes`` 的行报错并清空缓冲（上游 ``stdio.ts:187``）。"""
    script = "import sys,time\nsys.stdout.write('x' * 4096)\nsys.stdout.flush()\ntime.sleep(5)\n"
    path = FIXTURES / "_big_server.py"
    path.write_text(script, encoding="utf-8")
    try:
        transport = _transport(str(path), max_message_bytes=64)
        errors: list[Exception] = []
        transport.on_error(errors.append)
        await transport.start()
        for _ in range(100):
            if errors:
                break
            await asyncio.sleep(0.01)
        assert len(errors) == 1
        assert "exceeds 64 bytes" in str(errors[0])
        await transport.close()
    finally:
        path.unlink(missing_ok=True)


async def test_incomplete_trailing_message_is_reported_on_close() -> None:
    """关闭时 stdout 里还剩半条消息 → 上报（上游 ``stdio.ts:119``）。"""
    script = (
        "import sys,time\n"
        "sys.stdin.readline()\n"
        'sys.stdout.write(\'{"jsonrpc":"2.0",\')\n'  # 半条，无换行
        "sys.stdout.flush()\n"
        "time.sleep(5)\n"
    )
    path = FIXTURES / "_half_server.py"
    path.write_text(script, encoding="utf-8")
    try:
        transport = _transport(str(path), close_timeout_ms=100)
        errors: list[Exception] = []
        transport.on_error(errors.append)
        await transport.start()
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        for _ in range(100):
            if transport.stderr is not None and errors:
                break
            await asyncio.sleep(0.01)
        await transport.close()
        assert any("incomplete JSON-RPC message" in str(error) for error in errors)
    finally:
        path.unlink(missing_ok=True)


async def test_send_before_start_and_after_close_are_rejected() -> None:
    transport = _transport(STDIO_SERVER)
    with pytest.raises(McpConnectionClosedError):
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    await transport.start()
    with pytest.raises(RuntimeError, match="already started"):
        await transport.start()
    await transport.close()
    with pytest.raises(McpConnectionClosedError):
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


async def test_close_emits_close_once_and_is_idempotent() -> None:
    transport = _transport(STDIO_SERVER)
    closed: list[int] = []
    transport.on_close(lambda: closed.append(1))
    await transport.start()
    await transport.close()
    await transport.close()
    assert closed == [1]


async def test_stderr_buffer_keeps_only_the_tail() -> None:
    """stderr 只保留最近 ``max_stderr_bytes``（上游 ``stdio.ts:213``）。"""
    script = (
        "import sys\n"
        "sys.stderr.write('A' * 100 + 'B' * 100)\n"
        "sys.stderr.flush()\n"
        "sys.stdin.readline()\n"
    )
    path = FIXTURES / "_stderr_server.py"
    path.write_text(script, encoding="utf-8")
    try:
        transport = _transport(str(path), max_stderr_bytes=50)
        await transport.start()
        for _ in range(100):
            if transport.stderr:
                break
            await asyncio.sleep(0.01)
        assert transport.stderr == "B" * 50
        await transport.close()
    finally:
        path.unlink(missing_ok=True)

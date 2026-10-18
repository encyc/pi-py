"""C3b-2b-α 测试：MCP 连接生命周期（``index.ts`` 543–645）。用假 server/连接，不起真实 server。"""

from __future__ import annotations

import asyncio
import json

from pi_coding_agent.auth_storage import FileAuthStorageBackend
from pi_coding_agent.extensions.mcp.config import McpServerEntry
from pi_coding_agent.extensions.mcp.index import McpServer
from pi_coding_agent.extensions.mcp.lifecycle import McpConnectionLifecycle
from pi_coding_agent.extensions.mcp.oauth import McpOAuthCredentialStore
from pi_coding_agent.mcp_servers import validate_mcp_server_config


def _entry(name: str, raw: dict) -> McpServerEntry:  # type: ignore[type-arg]
    config = validate_mcp_server_config(name, raw)
    assert not isinstance(config, str), config
    return McpServerEntry(name=name, config=config, source="test")


class _Conn:
    def __init__(self, name: str, state: str = "connected") -> None:
        self.name = name
        self.state = state
        self.oauth_url: str | None = f"https://{name}/mcp"
        self.reconnects = 0
        self.get_client_calls = 0
        self.tools: list[dict[str, object]] = []

    async def reconnect(self) -> None:
        self.reconnects += 1
        self.state = "connected"

    async def get_client(self) -> object:
        self.get_client_calls += 1
        return object()


def _lifecycle(
    servers: list[McpServer],
    *,
    store: McpOAuthCredentialStore | None = None,
    aborted: bool = False,
    on_tools: object = None,
    discovery_calls: list[int] | None = None,
) -> tuple[McpConnectionLifecycle, list[int]]:
    changes: list[int] = []
    made: list[McpServer] = []

    async def create(server: McpServer, is_current: object) -> object:
        connection = _Conn(server.entry.name)
        made.append(server)
        return connection

    lifecycle = McpConnectionLifecycle(
        servers=lambda: servers,
        session_cwd=lambda: "/tmp",
        create_connection=create,  # type: ignore[arg-type]
        credentials=lambda: store,
        session_aborted=lambda: aborted,
        emit_change=lambda: changes.append(1),
        on_tools=on_tools or (lambda _c: None),  # type: ignore[arg-type]
        ensure_discovery_active=lambda: (
            discovery_calls if discovery_calls is not None else []
        ).append(1),
    )
    return lifecycle, changes


# ============================================================
# 令牌快照
# ============================================================


def test_stored_tokens_null_without_credentials_or_url(tmp_path):
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    connection = _Conn("docs")
    lifecycle, _ = _lifecycle([server])
    assert lifecycle.stored_tokens(connection) == "null"  # type: ignore[arg-type]

    store = McpOAuthCredentialStore(
        backend=FileAuthStorageBackend(str(tmp_path / "a.json")), lock_dir=str(tmp_path)
    )
    lifecycle2, _ = _lifecycle([server], store=store)
    assert lifecycle2.stored_tokens(connection) == "null"  # type: ignore[arg-type]


def test_stored_tokens_reads_store(tmp_path):
    store = McpOAuthCredentialStore(
        backend=FileAuthStorageBackend(str(tmp_path / "a.json")), lock_dir=str(tmp_path)
    )
    server_conn = store.for_server("docs", "https://docs/mcp")
    asyncio.run(server_conn.save({"tokens": {"access_token": "t1"}}))

    lifecycle, _ = _lifecycle(
        [McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))], store=store
    )
    assert json.loads(lifecycle.stored_tokens(_Conn("docs")))["access_token"] == "t1"  # type: ignore[arg-type]


def test_on_connection_change_tracks_needs_auth(tmp_path):
    store = McpOAuthCredentialStore(
        backend=FileAuthStorageBackend(str(tmp_path / "a.json")), lock_dir=str(tmp_path)
    )
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, changes = _lifecycle([server], store=store)
    connection = _Conn("docs", state="needs-auth")

    lifecycle.on_connection_change(connection)  # type: ignore[arg-type]
    assert id(connection) in lifecycle.tokens_at_sign_in
    assert changes == [1]

    # 已记录时不覆盖快照
    snapshot = lifecycle.tokens_at_sign_in[id(connection)]
    lifecycle.on_connection_change(connection)  # type: ignore[arg-type]
    assert lifecycle.tokens_at_sign_in[id(connection)] == snapshot

    # 状态离开 needs-auth → 删除
    connection.state = "connected"
    lifecycle.on_connection_change(connection)  # type: ignore[arg-type]
    assert id(connection) not in lifecycle.tokens_at_sign_in


# ============================================================
# reconnectSignedIn
# ============================================================


async def test_reconnect_signed_in_only_for_changed_credentials(tmp_path):
    store = McpOAuthCredentialStore(
        backend=FileAuthStorageBackend(str(tmp_path / "a.json")), lock_dir=str(tmp_path)
    )
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    connection = _Conn("docs", state="needs-auth")
    server.connection = connection  # type: ignore[assignment]
    discovery: list[int] = []
    lifecycle, _ = _lifecycle([server], store=store, discovery_calls=discovery)

    # 记下「需要登录时」的快照（此时无令牌 → "null"）
    lifecycle.on_connection_change(connection)  # type: ignore[arg-type]
    await lifecycle.reconnect_signed_in()
    assert connection.reconnects == 0  # 凭据未变 → 不重连
    assert discovery == []

    # 另一个进程写入了令牌
    await store.for_server("docs", "https://docs/mcp").save({"tokens": {"access_token": "new"}})
    await lifecycle.reconnect_signed_in()
    assert connection.reconnects == 1
    assert discovery == [1]  # 重连后重新激活发现工具
    assert id(connection) not in lifecycle.tokens_at_sign_in


async def test_reconnect_signed_in_noop_without_pending():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])
    await lifecycle.reconnect_signed_in()  # 不抛错


# ============================================================
# create / start
# ============================================================


async def test_create_sets_connection_and_emits_change():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, changes = _lifecycle([server])
    connection = await lifecycle.create(server, lambda: True)
    assert connection is not None
    assert server.connection is connection
    assert changes == [1]


async def test_create_returns_none_when_not_current():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])

    async def create(server: McpServer, is_current: object) -> None:
        return None

    lifecycle.create_connection = create  # type: ignore[assignment]
    assert await lifecycle.create(server, lambda: False) is None
    assert server.connection is None


async def test_start_connects_and_settles_ready():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])
    await lifecycle.start(server)
    await server.ready
    assert server.connection is not None
    assert server.connection.get_client_calls == 1


async def test_start_waits_for_after():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])
    gate = asyncio.Event()

    async def after() -> None:
        await gate.wait()

    lifecycle.start(server, after())  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert server.connection is None  # 还在等 after
    gate.set()
    await server.ready
    assert server.connection is not None


async def test_start_skips_when_attempt_replaced_or_disabled():
    entry = _entry("docs", {"url": "https://docs/mcp"})
    server = McpServer(entry=entry)
    lifecycle, _ = _lifecycle([server])
    # 启动后立刻把 attempt 换掉（模拟被禁用/替换）
    lifecycle.start(server)
    server.attempt = object()
    await server.ready
    assert server.connection is None


async def test_start_skips_disabled_server():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp", "enabled": False}))
    lifecycle, _ = _lifecycle([server])
    await lifecycle.start(server)
    await server.ready
    assert server.connection is None


async def test_start_swallows_connection_failure():
    """连不上时 ready 仍落定（失败体现在 server 状态里）。"""
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])

    async def failing(server: McpServer, is_current: object) -> object:
        raise RuntimeError("boom")

    lifecycle.create_connection = failing  # type: ignore[assignment]
    lifecycle.start(server)
    await server.ready  # 不抛
    assert server.connection is None


# ============================================================
# waitForServers
# ============================================================


async def test_wait_for_servers_returns_when_no_ready():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])
    await lifecycle.wait_for_servers([server])  # 无 ready → 立刻返回


async def test_wait_for_servers_waits_for_all():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])
    done: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(0.02)
        done.append("ready")

    server.ready = asyncio.ensure_future(slow())
    await lifecycle.wait_for_servers([server])
    assert done == ["ready"]


async def test_wait_for_servers_returns_on_abort():
    server = McpServer(entry=_entry("docs", {"url": "https://docs/mcp"}))
    lifecycle, _ = _lifecycle([server])
    never = asyncio.Event()
    server.ready = asyncio.ensure_future(never.wait())

    signal = asyncio.Event()

    async def abort_soon() -> None:
        await asyncio.sleep(0.01)
        signal.set()

    aborter = asyncio.ensure_future(abort_soon())
    await lifecycle.wait_for_servers([server], signal)
    await aborter
    assert not server.ready.done()  # 提前返回，未等 ready


async def test_wait_for_servers_skips_disabled_and_removed():
    served = McpServer(entry=_entry("kept", {"url": "https://kept/mcp"}))
    disabled = McpServer(entry=_entry("off", {"url": "https://off/mcp", "enabled": False}))
    removed = McpServer(entry=_entry("gone", {"url": "https://gone/mcp"}))
    never = asyncio.Event()
    served.ready = asyncio.ensure_future(asyncio.sleep(0))
    disabled.ready = asyncio.ensure_future(never.wait())
    removed.ready = asyncio.ensure_future(never.wait())
    lifecycle, _ = _lifecycle([served, disabled])  # removed 不在 servers 里

    signal = asyncio.Event()
    await lifecycle.wait_for_servers([served, disabled, removed], signal)
    assert disabled.ready is not None and not disabled.ready.done()


# ============================================================
# 防线
# ============================================================


def test_create_mcp_extension_still_not_exported():
    """C3b-2b 后半（6 个钩子）未做 → 工厂名不存在。"""
    import pi_coding_agent.extensions.mcp.lifecycle as module

    assert not hasattr(module, "create_mcp_extension")

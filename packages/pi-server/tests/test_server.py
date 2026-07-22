"""pi-server 测试：supervisor 实例管理 + IPC 协议。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_server import (
    InstanceSummary,
    Supervisor,
    encode_message,
    handle_request,
    parse_line,
    send_request,
)
from pi_server.ipc import serve

# ============================================================
# 协议编解码
# ============================================================


def test_encode_decode_roundtrip():
    msg = {"type": "spawn", "cwd": "/tmp", "label": "test"}
    encoded = encode_message(msg)
    assert encoded.endswith(b"\n")
    decoded = parse_line(encoded)
    assert decoded == msg


def test_parse_line_bytes():
    line = b'{"type": "list"}\n'
    result = parse_line(line)
    assert result["type"] == "list"


def test_instance_summary_to_dict():
    s = InstanceSummary(id="abc", status="online", cwd="/proj", label="my-agent")
    d = s.to_dict()
    assert d["id"] == "abc"
    assert d["status"] == "online"
    assert d["cwd"] == "/proj"
    assert d["label"] == "my-agent"


# ============================================================
# Supervisor（用 mock agent 避免 LLM 调用）
# ============================================================


def _make_mock_model():
    """构造一个最小 Model 对象（不用于真实调用）。"""
    from pi_ai import Model

    return Model(
        id="mock",
        name="mock",
        api="faux",
        provider="faux",
        base_url="",
        reasoning=False,
        input=["text"],
        context_window=1000,
        max_tokens=100,
    )


async def test_supervisor_spawn_and_list():
    """spawn → list → 找到实例。"""
    sup = Supervisor()
    model = _make_mock_model()
    with tempfile.TemporaryDirectory() as cwd:
        with patch("pi_server.supervisor.CodingAgent") as mock_agent_cls:
            mock_agent_cls.return_value = MagicMock()
            summary = await sup.spawn_instance(cwd=cwd, label="test", model=model)

        assert summary.status == "online"
        assert summary.cwd == cwd
        assert summary.label == "test"

        instances = sup.list_instances()
        assert len(instances) == 1
        assert instances[0].id == summary.id


async def test_supervisor_stop():
    """spawn → stop → 实例被移除。"""
    sup = Supervisor()
    model = _make_mock_model()
    with tempfile.TemporaryDirectory() as cwd:
        with patch("pi_server.supervisor.CodingAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.wait_for_idle = AsyncMock()
            mock_agent_cls.return_value = mock_agent
            summary = await sup.spawn_instance(cwd=cwd, model=model)

        instance_id = await sup.stop_instance(summary.id)
        assert instance_id == summary.id
        assert sup.get_instance(summary.id) is None


async def test_supervisor_stop_not_found():
    sup = Supervisor()
    with pytest.raises(KeyError):
        await sup.stop_instance("nonexistent")


async def test_supervisor_get_instance():
    sup = Supervisor()
    model = _make_mock_model()
    with tempfile.TemporaryDirectory() as cwd:
        with patch("pi_server.supervisor.CodingAgent"):
            summary = await sup.spawn_instance(cwd=cwd, model=model)
        inst = sup.get_instance(summary.id)
        assert inst is not None
        assert inst.status == "online"


async def test_supervisor_handle_rpc_prompt():
    """RPC prompt 命令转发给 agent。"""
    sup = Supervisor()
    model = _make_mock_model()
    with tempfile.TemporaryDirectory() as cwd:
        with patch("pi_server.supervisor.CodingAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.prompt = AsyncMock()
            mock_agent.subscribe = MagicMock()
            mock_agent_cls.return_value = mock_agent
            await sup.spawn_instance(cwd=cwd, model=model)

        # 找到实例 id
        inst_id = sup.list_instances()[0].id
        response = await sup.handle_rpc(inst_id, {"type": "prompt", "text": "hello"})
        assert response["success"] is True
        mock_agent.prompt.assert_called_once_with("hello")


async def test_supervisor_handle_rpc_unknown_instance():
    sup = Supervisor()
    response = await sup.handle_rpc("nonexistent", {"type": "prompt", "text": "x"})
    assert response["success"] is False


async def test_supervisor_handle_rpc_get_state():
    sup = Supervisor()
    model = _make_mock_model()
    with tempfile.TemporaryDirectory() as cwd:
        with patch("pi_server.supervisor.CodingAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.subscribe = MagicMock()
            mock_agent.state = MagicMock()
            mock_agent.state.messages = []
            mock_agent_cls.return_value = mock_agent
            await sup.spawn_instance(cwd=cwd, model=model)

        inst_id = sup.list_instances()[0].id
        response = await sup.handle_rpc(inst_id, {"type": "get_state"})
        assert response["success"] is True
        assert response["data"]["messageCount"] == 0


async def test_supervisor_open_close_stream():
    """打开/关闭事件流订阅。"""
    sup = Supervisor()
    model = _make_mock_model()
    with tempfile.TemporaryDirectory() as cwd:
        with patch("pi_server.supervisor.CodingAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.subscribe = MagicMock()
            mock_agent_cls.return_value = mock_agent
            await sup.spawn_instance(cwd=cwd, model=model)

        inst_id = sup.list_instances()[0].id
        queue = sup.open_rpc_stream(inst_id)
        assert queue is not None

        inst = sup.get_instance(inst_id)
        assert queue in inst._subscribers

        sup.close_rpc_stream(inst_id, queue)
        assert queue not in inst._subscribers


def test_supervisor_shutdown():
    from pi_server.supervisor import AgentInstance

    sup = Supervisor()
    inst = AgentInstance(id="x", status="online", cwd="/tmp")
    inst.agent = None
    sup._instances["x"] = inst
    sup.shutdown()
    assert inst.status == "stopped"


# ============================================================
# handle_request（上层请求分发）
# ============================================================


async def test_handle_request_list():
    response = await handle_request({"type": "list"})
    assert response["type"] == "list_result"
    assert response["ok"] is True
    assert "instances" in response


async def test_handle_request_unknown_type():
    response = await handle_request({"type": "unknown_xyz"})
    assert response["ok"] is False
    assert "Unknown" in response["error"]


async def test_handle_request_status_not_found():
    response = await handle_request({"type": "status", "instanceId": "nonexistent"})
    assert response["ok"] is False


# ============================================================
# IPC server（端到端：启动 server → 客户端请求）
# ============================================================


async def test_ipc_server_list():
    """启动 IPC server → send_request('list') → 收到响应。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # 用环境变量覆盖 socket 路径
        os.environ["PI_SERVER_DIR"] = tmp
        server_task = asyncio.create_task(serve())
        await asyncio.sleep(0.3)  # 等 server 启动

        try:
            response = await send_request({"type": "list"})
            assert response["type"] == "list_result"
            assert response["ok"] is True
            assert isinstance(response["instances"], list)
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
            os.environ.pop("PI_SERVER_DIR", None)

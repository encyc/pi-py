"""IPC 服务端：Unix socket + JSONL 协议。

对应上游 ``packages/server/src/ipc/server.ts`` + ``handler.ts`` + ``serve.ts``。

两条路径：
- **普通请求**（spawn/list/stop/status/rpc）：一连接一请求一响应。
- **rpc_stream**：握手后升级为双向流（事件推送）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .config import ensure_server_dir, get_socket_path
from .protocol import encode_message, error_response, ok_response, parse_line
from .supervisor import supervisor

logger = logging.getLogger(__name__)


async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """处理上层请求。返回响应 dict。"""
    req_type = request.get("type", "")

    try:
        if req_type == "spawn":
            summary = await supervisor.spawn_instance(
                cwd=request.get("cwd", "."),
                label=request.get("label"),
                model=request.get("model_obj"),  # 调用方需预先构造 Model
                api_key=request.get("api_key"),
            )
            return ok_response("spawn_result", instance=summary.to_dict())

        elif req_type == "list":
            instances = [s.to_dict() for s in supervisor.list_instances()]
            return ok_response("list_result", instances=instances)

        elif req_type == "status":
            inst = supervisor.get_instance(request["instanceId"])
            if inst is None:
                return error_response(f"Instance not found: {request['instanceId']}")
            return ok_response("status_result", instance=inst.to_summary().to_dict())

        elif req_type == "stop":
            instance_id = await supervisor.stop_instance(request["instanceId"])
            return ok_response("stop_result", instanceId=instance_id)

        elif req_type == "rpc":
            response = await supervisor.handle_rpc(request["instanceId"], request["command"])
            return ok_response("rpc_result", response=response)

        elif req_type == "rpc_stream":
            inst = supervisor.get_instance(request["instanceId"])
            if inst is None:
                return error_response(f"Instance not found: {request['instanceId']}")
            return ok_response("rpc_ready", instance=inst.to_summary().to_dict())

        else:
            return error_response(f"Unknown request type: {req_type}")

    except KeyError as e:
        return error_response(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Request handling error")
        return error_response(str(e))


async def _handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """处理单个 Unix socket 连接。"""
    try:
        line = await reader.readline()
        if not line:
            return

        request = parse_line(line)
        req_type = request.get("type", "")

        # rpc_stream：升级为流式连接
        if req_type == "rpc_stream":
            await _handle_rpc_stream(request, reader, writer)
            return

        # 普通请求：一请求一响应
        response = await handle_request(request)
        writer.write(encode_message(response))
        await writer.drain()

    except (json.JSONDecodeError, KeyError) as e:
        writer.write(encode_message(error_response(f"Bad request: {e}")))
        await writer.drain()
    except Exception:  # noqa: BLE001
        logger.exception("Connection error")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _handle_rpc_stream(
    request: dict[str, Any],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """处理 rpc_stream 连接（双向 JSONL）。"""
    instance_id = request["instanceId"]

    # 握手
    inst = supervisor.get_instance(instance_id)
    if inst is None or inst.agent is None:
        writer.write(encode_message(error_response(f"Instance not online: {instance_id}")))
        await writer.drain()
        return

    ready = ok_response("rpc_ready", instance=inst.to_summary().to_dict())
    writer.write(encode_message(ready))
    await writer.drain()

    # 订阅事件流
    event_queue = supervisor.open_rpc_stream(instance_id)
    if event_queue is None:
        writer.write(encode_message(error_response("Failed to open stream")))
        await writer.drain()
        return

    async def _forward_events() -> None:
        """从事件队列读取，写入 socket。"""
        try:
            while True:
                event = await event_queue.get()
                if event is None:  # 关闭信号
                    break
                writer.write(encode_message({"type": "event", "event": _serialize_event(event)}))
                await writer.drain()
        except Exception:  # noqa: BLE001
            pass

    async def _read_commands() -> None:
        """从 socket 读取命令，转发给 agent。"""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                command = parse_line(line)
                response = await supervisor.handle_rpc(instance_id, command)
                writer.write(encode_message({"type": "rpc_result", "response": response}))
                await writer.drain()
        except Exception:  # noqa: BLE001
            pass

    # 并发：读命令 + 转发事件
    tasks = [
        asyncio.create_task(_forward_events()),
        asyncio.create_task(_read_commands()),
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for t in tasks:
            t.cancel()
        supervisor.close_rpc_stream(instance_id, event_queue)


def _serialize_event(event: Any) -> Any:
    """序列化 agent 事件。"""
    if hasattr(event, "type"):
        return {"type": event.type}
    return str(event)


async def serve(host_task: bool = True) -> asyncio.base_events.Server | None:
    """启动 IPC 服务，监听 Unix socket。

    对应上游 ``serve()``。
    """
    import os

    socket_path = get_socket_path()
    ensure_server_dir()

    # 清理 stale socket
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    server = await asyncio.start_unix_server(_handle_connection, path=socket_path)
    logger.info("server listening on %s", socket_path)

    try:
        if host_task:
            # 永久运行（直到被取消）
            async with server:
                await server.serve_forever()
        else:
            return server
    finally:
        supervisor.shutdown()
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    return None


async def send_request(request: dict[str, Any]) -> dict[str, Any]:
    """客户端：发送一次性请求。

    对应上游 ``sendIpcRequest``。
    """
    socket_path = get_socket_path()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write(encode_message(request))
        await writer.drain()
        line = await reader.readline()
        if not line:
            return error_response("No response from server")
        return parse_line(line)
    finally:
        writer.close()
        await writer.wait_closed()


__all__ = ["serve", "send_request", "handle_request"]

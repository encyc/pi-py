"""实例管理器（Supervisor）。

对应上游 ``packages/server/src/supervisor.ts`` + ``rpc-process.ts``。

关键偏离：上游 spawn 子进程（``pi --mode rpc``），通过 JSONL over stdio 通信。
Python 版直接在进程内管理 ``CodingAgent`` 实例，通过事件订阅实现流式。
这样更轻量、更 Pythonic，适合 SDK 场景。

状态机：``starting → online → stopping → stopped``，异常时 ``error``。
持久化：``instances.json`` 镜像（用于跨重启可见性 + CLI list）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from pi_ai import Model
from pi_coding_agent import CodingAgent

from .config import ensure_server_dir, get_instances_path
from .protocol import InstanceStatus, InstanceSummary


class AgentInstance:
    """一个 agent 实例（进程内）。"""

    def __init__(
        self,
        id: str,
        status: InstanceStatus,
        cwd: str,
        label: str | None = None,
    ) -> None:
        self.id = id
        self.status = status
        self.cwd = cwd
        self.label = label
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.agent: CodingAgent | None = None
        self._subscribers: list[Any] = []  # asyncio.Queue（事件流）

    def to_summary(self) -> InstanceSummary:
        return InstanceSummary(
            id=self.id,
            status=self.status,
            cwd=self.cwd,
            label=self.label,
            created_at=self.created_at,
        )


class Supervisor:
    """管理多个 agent 实例。"""

    def __init__(self) -> None:
        self._instances: dict[str, AgentInstance] = {}

    # ---- 查询 ----

    def get_instance(self, instance_id: str) -> AgentInstance | None:
        return self._instances.get(instance_id)

    def list_instances(self) -> list[InstanceSummary]:
        return [inst.to_summary() for inst in self._instances.values()]

    def list_live(self) -> list[AgentInstance]:
        return [i for i in self._instances.values() if i.status in ("online", "starting")]

    # ---- 生命周期 ----

    async def spawn_instance(
        self,
        cwd: str = ".",
        label: str | None = None,
        model: Model | None = None,
        api_key: str | None = None,
    ) -> InstanceSummary:
        """创建新实例。"""
        instance_id = uuid.uuid4().hex[:16]
        inst = AgentInstance(id=instance_id, status="starting", cwd=cwd, label=label)
        self._instances[instance_id] = inst
        self._persist()

        try:
            if model is None:
                raise ValueError("model is required to spawn an agent instance")
            inst.agent = CodingAgent(model=model, api_key=api_key, cwd=cwd)

            # 订阅 agent 事件，转发给订阅者
            def _on_event(event: Any, signal: Any) -> None:
                for queue in list(inst._subscribers):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass

            inst.agent.subscribe(_on_event)
            inst.status = "online"
            self._persist()
            return inst.to_summary()
        except Exception:  # noqa: BLE001
            inst.status = "error"
            self._persist()
            raise

    async def stop_instance(self, instance_id: str) -> str:
        """停止实例。"""
        inst = self._instances.get(instance_id)
        if inst is None:
            raise KeyError(f"Instance not found: {instance_id}")
        inst.status = "stopping"
        if inst.agent is not None:
            inst.agent.abort()
            try:
                await asyncio.wait_for(inst.agent.wait_for_idle(), timeout=5)
            except TimeoutError:
                pass
        inst.status = "stopped"
        inst.agent = None
        inst._subscribers.clear()
        self._instances.pop(instance_id, None)
        self._persist()
        return instance_id

    # ---- RPC ----

    async def handle_rpc(self, instance_id: str, command: dict[str, Any]) -> dict[str, Any]:
        """转发 RPC 命令给 agent 实例。"""
        inst = self._instances.get(instance_id)
        if inst is None or inst.agent is None:
            return {"success": False, "error": f"Instance not found or not online: {instance_id}"}

        cmd_type = command.get("type", "")
        try:
            if cmd_type == "prompt":
                text = command.get("text", command.get("message", ""))
                await inst.agent.prompt(text)
                return {"success": True, "command": cmd_type}
            elif cmd_type == "get_state":
                msgs = inst.agent.state.messages
                return {
                    "success": True,
                    "command": cmd_type,
                    "data": {"messageCount": len(msgs)},
                }
            elif cmd_type == "abort":
                inst.agent.abort()
                return {"success": True, "command": cmd_type}
            else:
                return {"success": False, "error": f"Unknown command: {cmd_type}"}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": str(e)}

    def open_rpc_stream(self, instance_id: str) -> asyncio.Queue[Any] | None:
        """打开事件流订阅。返回一个 asyncio.Queue 供消费者读取。"""
        inst = self._instances.get(instance_id)
        if inst is None or inst.agent is None:
            return None
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
        inst._subscribers.append(queue)
        return queue

    def close_rpc_stream(self, instance_id: str, queue: asyncio.Queue[Any]) -> None:
        """关闭事件流订阅。"""
        inst = self._instances.get(instance_id)
        if inst and queue in inst._subscribers:
            inst._subscribers.remove(queue)

    # ---- 持久化 ----

    def _persist(self) -> None:
        """同步状态到 instances.json。"""
        ensure_server_dir()
        data = {
            "instances": [
                {
                    "id": inst.id,
                    "status": inst.status,
                    "cwd": inst.cwd,
                    "label": inst.label,
                    "createdAt": inst.created_at,
                }
                for inst in self._instances.values()
            ]
        }
        Path(get_instances_path()).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def recover_after_restart(self) -> None:
        """server 重启后恢复：所有 online/starting → stopped。"""
        changed = False
        for inst in self._instances.values():
            if inst.status in ("online", "starting"):
                inst.status = "stopped"
                inst.agent = None
                changed = True
        if changed:
            self._persist()

    def shutdown(self) -> None:
        """关闭所有实例。"""
        for inst in list(self._instances.values()):
            inst.status = "stopped"
            inst.agent = None
            inst._subscribers.clear()
        self._persist()


#: 全局单例。
supervisor = Supervisor()


__all__ = ["AgentInstance", "Supervisor", "supervisor"]

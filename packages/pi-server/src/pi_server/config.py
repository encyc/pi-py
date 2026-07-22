"""路径与配置。

对应上游 ``packages/server/src/config.ts``。

socket path 优先级：``$PI_SERVER_DIR`` > ``($PI_CONFIG_DIR 或 ~/.pi)/server``。
默认 socket：``~/.pi/server/server.sock``。
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_NAME = ".pi"
ENV_SERVER_DIR = "PI_SERVER_DIR"
ENV_CONFIG_DIR = "PI_CONFIG_DIR"


def get_server_dir() -> str:
    """获取 server 目录。"""
    env_dir = os.environ.get(ENV_SERVER_DIR)
    if env_dir:
        return os.path.expanduser(env_dir)
    config_dir = os.environ.get(ENV_CONFIG_DIR) or str(Path.home() / CONFIG_DIR_NAME)
    return os.path.join(config_dir, "server")


def get_socket_path() -> str:
    """获取 Unix socket 文件路径。"""
    return os.path.join(get_server_dir(), "server.sock")


def get_instances_path() -> str:
    """获取 instances.json 持久化路径。"""
    return os.path.join(get_server_dir(), "instances.json")


def ensure_server_dir() -> None:
    """确保 server 目录存在。"""
    Path(get_server_dir()).mkdir(parents=True, exist_ok=True)


__all__ = [
    "CONFIG_DIR_NAME",
    "get_server_dir",
    "get_socket_path",
    "get_instances_path",
    "ensure_server_dir",
]

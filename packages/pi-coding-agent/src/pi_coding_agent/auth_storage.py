"""auth 文件的加锁读改写后端（**窄接口**）。

对应上游 ``core/auth-storage.ts``（507 行）中 MCP 扩展真正使用的那部分：
``AuthStorageBackend``（2 方法）+ ``FileAuthStorageBackend``。

**窄接口说明（经 Lead 批准）**：上游 507 行里其余约 300 行是 pi-ai 的凭证管理
（``AuthStorage`` / ``ReadOnlyAuthStorage`` / ``InMemoryAuthStorageBackend`` /
``readStoredCredential``，依赖 pi-ai 的 ``Credential`` / ``CredentialStore`` / ``CredentialInfo``），
本包及可见的后续任务都没有消费者，因此**不移植、不导出**（见 ``PORTING.md``）。

MCP 扩展（``extensions/mcp/oauth.ts``）只把它当作**「带锁的 JSON 文件读改写原语」**用::

    backend.withLock((current: string|undefined) => ({ result, next? })): T

即「读出当前内容 → 返回结果 + 可选的新内容 → 写回」，与凭证模型无关。

**有意偏离（锁实现）**
上游用 npm 的 ``proper-lockfile``（``<path>.lock`` 目录 + mtime 心跳 + ``update``/``compromised``
机制）。本端**零新依赖**，用 ``os.mkdir`` 的原子性手工实现排他锁，并加**过期锁回收**：

* 锁目录内写 ``owner.json``（pid / hostname / 建立时间）
* 冲突时：若同主机且该 pid 已不存在 → 判定为**崩溃残留**，回收后重试
* 兜底：锁目录 mtime 超过 :data:`STALE_LOCK_SECONDS` 秒 → 判定失效（覆盖跨主机 / 无法验活的情形）
* 上游的 ``compromised`` 回调（心跳失败）**未实现** —— 本端无心跳线程，锁在临界区内不会被
  主动判定失效，只有**冲突方**才会回收过期锁。临界区是「读一个小 JSON + 写回」，毫秒级完成，
  因此 :data:`STALE_LOCK_SECONDS` 取 10 秒仍是安全的。

**未移植的 shell 细节**：上游 ``ensureParentDir`` 用 ``0o700``、写文件用 ``0o600``（仅创建时生效，
不动管理员已设的模式）—— 这两条本端照做。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

__all__ = [
    "STALE_LOCK_SECONDS",
    "AuthOperationOptions",
    "AuthStorageBackend",
    "FileAuthStorageBackend",
    "LockResult",
    "default_auth_path",
]

T = TypeVar("T")

#: 锁目录 mtime 超过该秒数即视为崩溃残留（无从验活时的兜底）。
STALE_LOCK_SECONDS = 10.0

#: 同步获取锁的重试次数与间隔（对齐上游 ``acquireLockSyncWithRetry``）。
_MAX_LOCK_ATTEMPTS = 10
_LOCK_RETRY_DELAY_MS = 20

#: 异步获取锁的总期限与指数退避上限（对齐上游 ``withLockAsync`` 的 30s 期限）。
_ASYNC_LOCK_DEADLINE_MS = 30_000
_ASYNC_LOCK_MAX_DELAY_MS = 1_000

#: ``EPERM`` 重试上限：Windows 上刚删除的锁目录可能短暂处于 "delete pending"。
_MAX_EPERM_RETRIES = 5

_FILE_MODE = 0o600
_DIR_MODE = 0o700


def default_auth_path() -> str:
    """默认 auth 文件路径。

    对齐上游 ``config.ts: getAgentDir()``：``$<APP>_CODING_AGENT_DIR`` 优先，否则
    ``~/<CONFIG_DIR>/agent``。这里按 pi 的默认值取 ``~/.pi/agent/auth.json`` 并支持
    ``PI_CODING_AGENT_DIR`` 覆盖（上游 ``config.ts`` 未移植，仅实现这两个分支）。
    """
    override = os.environ.get("PI_CODING_AGENT_DIR")
    base = os.path.expanduser(override) if override else os.path.join(Path.home(), ".pi", "agent")
    return os.path.join(base, "auth.json")


@dataclass
class LockResult(Generic[T]):
    """临界区的返回值：``result`` 交给调用方，``next`` 非 None 时写回文件。"""

    result: T
    next: str | None = None


@dataclass
class AuthOperationOptions:
    """异步操作的选项（对齐上游 ``AuthOperationOptions``）。"""

    signal: Any = None


@runtime_checkable
class AuthStorageBackend(Protocol):
    """加锁的读改写后端。"""

    def with_lock(self, fn: Callable[[str | None], LockResult[T]]) -> T:
        """同步执行 ``fn``；期间持有排他锁。"""
        ...

    async def with_lock_async(
        self,
        fn: Callable[[str | None], Awaitable[LockResult[T]]],
        options: AuthOperationOptions | None = None,
    ) -> T:
        """异步执行 ``fn``；期间持有排他锁。"""
        ...


@dataclass
class FileAuthStorageBackend:
    """基于一个 JSON 文件的 :class:`AuthStorageBackend`。

    上游对应类名 ``FileAuthStorageBackend``（此处用 dataclass 以便 ``field`` 默认值）。
    """

    auth_path: str = field(default_factory=default_auth_path)
    _lock_dir: str = field(init=False, repr=False, default="")

    def __post_init__(self) -> None:
        self.auth_path = os.path.normpath(self.auth_path)
        self._lock_dir = f"{self.auth_path}.lock"

    # ---- 准备 ----

    def _ensure_parent_dir(self) -> None:
        parent = os.path.dirname(self.auth_path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, mode=_DIR_MODE, exist_ok=True)

    def _ensure_file_exists(self) -> None:
        if not os.path.exists(self.auth_path):
            _write_file(self.auth_path, "{}")

    # ---- 锁 ----

    def _lock_is_stale(self) -> bool:
        """过期锁判定：同主机且 pid 已死 → 立刻回收；否则按 mtime 兜底。"""
        owner_path = os.path.join(self._lock_dir, "owner.json")
        try:
            with open(owner_path, encoding="utf-8") as handle:
                owner = json.load(handle)
        except (OSError, ValueError):
            owner = None

        if isinstance(owner, dict) and owner.get("hostname") == socket.gethostname():
            pid = owner.get("pid")
            if isinstance(pid, int) and not _pid_alive(pid):
                return True

        try:
            age = time.time() - os.path.getmtime(self._lock_dir)
        except OSError:
            return False
        return age > STALE_LOCK_SECONDS

    def _try_acquire(self) -> bool:
        """尝试创建锁目录；成功返回 True，被占用返回 False。"""
        try:
            os.mkdir(self._lock_dir, _DIR_MODE)
        except FileExistsError:
            if self._lock_is_stale():
                # 回收崩溃残留后重试一次；仍失败就让上层按重试策略继续。
                _remove_lock_dir(self._lock_dir)
                try:
                    os.mkdir(self._lock_dir, _DIR_MODE)
                except FileExistsError:
                    return False
                except OSError:
                    return False
            else:
                return False
        except OSError as error:
            if error.errno == errno.EPERM:
                # 交由调用方的 EPERM 重试上限处理。
                raise
            return False
        _write_owner(self._lock_dir)
        return True

    def _acquire_sync(self) -> None:
        for attempt in range(1, _MAX_LOCK_ATTEMPTS + 1):
            try:
                if self._try_acquire():
                    return
            except OSError as error:
                if error.errno != errno.EPERM:
                    raise
            if attempt == _MAX_LOCK_ATTEMPTS:
                raise TimeoutError(f"Could not acquire auth storage lock: {self._lock_dir}")
            _sleep_ms(_LOCK_RETRY_DELAY_MS)
        raise TimeoutError(f"Could not acquire auth storage lock: {self._lock_dir}")

    async def _acquire_async(self, signal: Any) -> None:
        deadline = time.monotonic() + _ASYNC_LOCK_DEADLINE_MS / 1000
        retry = 0
        eperm_retries = 0
        while True:
            _throw_if_aborted(signal)
            error: OSError | None = None
            try:
                if self._try_acquire():
                    return
            except OSError as caught:
                error = caught
                if caught.errno != errno.EPERM:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if error is not None:
                    raise error
                raise TimeoutError(f"Could not acquire auth storage lock: {self._lock_dir}")
            if error is not None:
                eperm_retries += 1
                if eperm_retries > _MAX_EPERM_RETRIES:
                    raise error
            base_delay_ms = min(10 * 2**retry, _ASYNC_LOCK_MAX_DELAY_MS / 2)
            retry += 1
            delay_ms = min(base_delay_ms * (1 + 0.5), remaining * 1000)
            await asyncio.sleep(delay_ms / 1000)

    def _release(self) -> None:
        _remove_lock_dir(self._lock_dir)

    # ---- 公开 API ----

    def with_lock(self, fn: Callable[[str | None], LockResult[T]]) -> T:
        self._ensure_parent_dir()
        self._ensure_file_exists()
        self._acquire_sync()
        try:
            current = _read_file(self.auth_path)
            outcome = fn(current)
            if outcome.next is not None:
                _write_file(self.auth_path, outcome.next)
            return outcome.result
        finally:
            self._release()

    async def with_lock_async(
        self,
        fn: Callable[[str | None], Awaitable[LockResult[T]]],
        options: AuthOperationOptions | None = None,
    ) -> T:
        signal = options.signal if options is not None else None
        _throw_if_aborted(signal)
        self._ensure_parent_dir()
        self._ensure_file_exists()
        await self._acquire_async(signal)
        try:
            _throw_if_aborted(signal)
            current = _read_file(self.auth_path)
            outcome = await fn(current)
            _throw_if_aborted(signal)
            if outcome.next is not None:
                _write_file(self.auth_path, outcome.next)
            return outcome.result
        finally:
            with contextlib.suppress(OSError):
                self._release()


def _pid_alive(pid: int) -> bool:
    """该 pid 是否仍存在（同主机）。权限不足视为存活，避免误抢活锁。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _write_owner(lock_dir: str) -> None:
    try:
        with open(os.path.join(lock_dir, "owner.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "hostname": socket.gethostname(), "createdAt": time.time()},
                handle,
            )
    except OSError:
        # 写不了属主信息不致命：过期判定会退回 mtime 兜底。
        pass


def _remove_lock_dir(lock_dir: str) -> None:
    owner_path = os.path.join(lock_dir, "owner.json")
    with contextlib.suppress(OSError):
        os.unlink(owner_path)
    with contextlib.suppress(OSError):
        os.rmdir(lock_dir)


def _read_file(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    data = Path(path).read_bytes()
    # 对齐上游 stripBom。
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8")


def _write_file(path: str, content: str) -> None:
    # 上游 AUTH_FILE_WRITE_OPTIONS 只给 mode 0o600，且仅在创建时生效，
    # 以保留管理员设置的权限；os.open 的 O_CREAT 语义一致。
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)


def _sleep_ms(milliseconds: int) -> None:
    time.sleep(milliseconds / 1000)


def _throw_if_aborted(signal: Any) -> None:
    if signal is not None and getattr(signal, "aborted", False):
        raise asyncio.CancelledError("auth storage operation aborted")

"""durable 的错误类型 —— 移植 ``durable/src/errors.ts``（57 行）。"""

from __future__ import annotations

from pi_durable.types import ConversationId

__all__ = [
    "ConversationBusy",
    "ReadAfterWrite",
    "SessionFailed",
    "StorageRequestError",
    "error_message",
]


def error_message(error: object) -> str:
    """取一个抛出值的消息，**且自身不抛**（``errors.ts:3-14``）。

    上游用 ``try { String(error) } catch { Object.prototype.toString.call(error) }`` 兜底。
    Python 的 ``str()`` 对绝大多数对象都不会抛，但 ``__str__`` 可以被实现成抛异常，
    所以保留同样的兜底。
    """
    if isinstance(error, BaseException):
        return str(error)
    try:
        return str(error)
    except Exception:
        return type(error).__name__


class ReadAfterWrite(Exception):  # noqa: N818 - 上游类名如此，忠实镜像
    """事务在第一次表写入之后又读了表（``errors.ts:16-22``）。

    要求：在写之前把需要的行都读完。
    """

    def __init__(self, method: str) -> None:
        super().__init__(f"Tx.{method}() cannot read tables after the first table write")
        self.name = "ReadAfterWrite"


class StorageRequestError(Exception):
    """Storage 读操作拒绝了无效请求（``errors.ts:24-34``）。

    例如未知会话、来自另一次扫描的游标。**没有持久副作用**。与 Storage 抛出的其它错误不同，
    它只让那一次读失败，不会让 Session 失败；但**由 ``commit()`` 抛出时同样致命**。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "StorageRequestError"


class SessionFailed(Exception):  # noqa: N818 - 上游类名如此，忠实镜像
    """Session 已失败，不再运行也不提交（``errors.ts:36-46``）。

    触发条件：某个 Storage 方法抛错、提交无法被采纳、或 Harness 自己的簿记抛错。
    ``__cause__`` 是那第一个错误。重新打开 Session 即可从已提交状态恢复。
    """

    def __init__(self, cause: object) -> None:
        super().__init__("Session failed after a storage error; close and reopen it")
        self.name = "SessionFailed"
        self.__cause__ = cause if isinstance(cause, BaseException) else None


class ConversationBusy(Exception):  # noqa: N818 - 上游类名如此，忠实镜像
    """一次提交到达了忙碌的会话，未被受理（``errors.ts:48-56``）。"""

    def __init__(self, conversation_id: ConversationId) -> None:
        super().__init__(f"Conversation {conversation_id} is busy")
        self.name = "ConversationBusy"
        self.conversation_id = conversation_id

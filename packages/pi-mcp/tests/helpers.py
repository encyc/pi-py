"""HTTP 测试辅助：起一个**真实回环 HTTP 服务端**（对应上游 ``test/helpers.ts``）。

上游用 ``node:http`` + ``server.listen(0, "127.0.0.1")``；Python 侧用
``http.server.ThreadingHTTPServer``（每连接一线程），既能做普通响应，也能做
**分块传输的 SSE 长连接** —— 这是 ``StreamableHttpTransport`` 的主要验收面。

用法：

```python
origin = listen(handler)          # handler(request, response, requests)
...
close_servers()                   # afterEach
```
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class RecordedRequest:
    """服务端收到的一次请求。"""

    method: str
    path: str
    headers: dict[str, str]
    body: str


@dataclass
class Response:
    """供 handler 使用的响应写入器。

    ``send()`` 写一次性响应；``start_stream()`` + ``write_chunk()`` + ``end_stream()``
    写 HTTP/1.1 分块流（SSE 需要它来「边算边发」且不结束连接）。
    """

    _handler: BaseHTTPRequestHandler
    _streaming: bool = False
    _ended: bool = False

    def send(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        """写一个带 ``Content-Length`` 的普通响应。"""
        payload = body.encode("utf-8")
        self._handler.send_response(status)
        for name, value in (headers or {}).items():
            self._handler.send_header(name, value)
        self._handler.send_header("Content-Length", str(len(payload)))
        self._handler.end_headers()
        self._handler.wfile.write(payload)
        self._handler.wfile.flush()
        self._ended = True

    def start_stream(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        *,
        content_length: int | None = None,
    ) -> None:
        """开始一个响应。

        ``content_length`` 给定则不分组；否则用 ``Transfer-Encoding: chunked`` 以便持续推送。
        """
        self._handler.send_response(status)
        for name, value in (headers or {}).items():
            self._handler.send_header(name, value)
        if content_length is None:
            self._handler.send_header("Transfer-Encoding", "chunked")
            self._streaming = True
        else:
            self._handler.send_header("Content-Length", str(content_length))
        self._handler.end_headers()
        self._handler.wfile.flush()

    def write_chunk(self, text: str) -> None:
        """写一个分块（或裸写，若用了 ``content_length``）。客户端已断开时静默忽略。"""
        payload = text.encode("utf-8")
        with contextlib.suppress(OSError, ValueError):
            if self._streaming:
                self._handler.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
                self._handler.wfile.write(payload)
                self._handler.wfile.write(b"\r\n")
            else:
                self._handler.wfile.write(payload)
            self._handler.wfile.flush()

    def end_stream(self) -> None:
        """结束流（写终止分块）。"""
        if self._ended:
            return
        self._ended = True
        if self._streaming:
            with contextlib.suppress(OSError, ValueError):
                self._handler.wfile.write(b"0\r\n\r\n")
                self._handler.wfile.flush()

    def abort(self) -> None:
        """**不写终止分块**直接断开连接，模拟流在中途断掉。"""
        if self._ended:
            return
        self._ended = True
        with contextlib.suppress(OSError):
            self._handler.connection.close()

    def sleep(self, seconds: float) -> None:
        """在流中间等待（让客户端先读到已写出的部分）。"""
        time.sleep(seconds)


#: handler 签名：(request, response, 目前为止的全部请求)
HttpHandler = Callable[[RecordedRequest, Response, list[RecordedRequest]], None]

_servers: list[ThreadingHTTPServer] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pi-mcp-test"

    def _handle(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        record = RecordedRequest(
            method=self.command,
            path=self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=raw.decode("utf-8"),
        )
        requests: list[RecordedRequest] = self.server.requests  # type: ignore[attr-defined]
        requests.append(record)
        response = Response(self)
        try:
            self.server.handler(record, response, requests)  # type: ignore[attr-defined]
        finally:
            response.end_stream()

    do_GET = _handle
    do_POST = _handle
    do_DELETE = _handle

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 对齐基类签名
        """静音访问日志。"""


def listen(handler: HttpHandler) -> str:
    """起一个回环 HTTP 服务端，返回 ``http://127.0.0.1:<port>``。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.handler = handler  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _servers.append(server)
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}"


def close_servers() -> None:
    """关闭全部测试服务端。"""
    for server in _servers:
        server.shutdown()
        server.server_close()
    _servers.clear()


def sse(*events: str) -> str:
    """把若干 SSE 文本片段拼成一个响应体。"""
    return "".join(events)


def sse_message(payload: dict[str, Any], event_id: str | None = None) -> str:
    """一条 ``message`` 事件的 SSE 文本。"""
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: message\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


__all__ = [
    "HttpHandler",
    "RecordedRequest",
    "Response",
    "close_servers",
    "listen",
    "sse",
    "sse_message",
]

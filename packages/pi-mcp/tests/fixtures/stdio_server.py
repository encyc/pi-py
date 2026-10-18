"""stdio 测试夹具：换行分帧的 MCP 服务端（对应上游 ``test/fixtures/stdio-server.mjs``）。

行为逐条对齐上游：

- 启动即往 stderr 写 ``stdio fixture ready``（供测试断言 stderr 捕获）；
- 逐行读 stdin，忽略通知（无 ``id``）；
- 支持 ``initialize`` / ``tools/list`` / ``tools/call`` / ``ping``；其余方法回 ``-32601``；
- ``initialize`` 回协议版本 ``2025-06-18``、``capabilities.tools = {}``；
- 每条答复写成一行 JSON 到 stdout。
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    sys.stderr.write("stdio fixture ready\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stdio-fixture", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            text = str(message["params"]["arguments"]["text"])
            result = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "not found"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()

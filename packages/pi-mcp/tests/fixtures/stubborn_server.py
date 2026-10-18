"""顽固 stdio 测试夹具（对应上游 ``test/fixtures/stubborn-server.mjs``）。

行为逐条对齐上游：

- 应答 ``initialize``；
- 起一个**孙进程**，它忽略 SIGTERM 且比 stdin 活得久；把 ``grandchild <pid>`` 写到 stderr；
- 自己忽略 stdin EOF 与 SIGTERM，因此必须靠 SIGKILL 收场。
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

_GRANDCHILD_CODE = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, lambda *a: None)\n"
    "while True:\n"
    "    time.sleep(1)\n"
)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *args: None)

    grandchild = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _GRANDCHILD_CODE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sys.stderr.write(f"grandchild {grandchild.pid}\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("method") != "initialize":
            continue
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "stubborn-fixture", "version": "1.0.0"},
        }
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n"
        )
        sys.stdout.flush()

    # stdin 已 EOF：像上游那样继续活着（忽略 EOF），等 SIGKILL。
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()

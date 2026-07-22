# pi-server

Python port of [@earendil-works/pi-server](https://github.com/earendil-works/pi) — Agent server (Unix socket + JSONL).

## Install

```bash
pip install pi-server
```

## Quick Start

```python
import asyncio
from pi_server import serve, send_request

# Start server in background
asyncio.create_task(serve())

# Client: list instances
response = await send_request({"type": "list"})
print(response["instances"])
```

## Commands

- `spawn` — create new agent instance
- `list` — list all instances
- `status` — get instance status
- `stop` — stop an instance
- `rpc` — forward command to agent
- `rpc_stream` — open bidirectional event stream

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream

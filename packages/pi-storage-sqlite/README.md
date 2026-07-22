# pi-storage-sqlite

Python port of [@earendil-works/pi-storage-sqlite-node](https://github.com/earendil-works/pi) — SQLite session storage backend.

## Install

```bash
pip install pi-storage-sqlite
```

## Quick Start

```python
from pi_storage_sqlite import SqliteSessionRepo
from pi_agent_core import Session

repo = SqliteSessionRepo("sessions.db")
sid, storage = repo.create(cwd="/project")
session = Session(storage)
session.append_message(UserMessage(content="hello"))
# ... agent interaction ...
repo.close()
```

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream

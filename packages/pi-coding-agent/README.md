# pi-coding-agent

Python port of [@earendil-works/pi-coding-agent](https://github.com/earendil-works/pi) — Coding agent SDK (core only, no TUI).

## Install

```bash
pip install pi-coding-agent
```

## Quick Start

```python
from pi_coding_agent import CodingAgent
from pi_ai import Model

agent = CodingAgent(
    model=Model(id="deepseek-chat", api="openai-completions", provider="deepseek",
                base_url="https://api.deepseek.com/v1", input=["text"],
                context_window=64000, max_tokens=8192),
    api_key="sk-...",
    cwd=".",  # working directory
)
await agent.prompt("Read README.md and summarize it")
```

## Tools

- **bash** — execute shell commands
- **read** — read files (text + images, offset/limit)
- **edit** — exact text replacement with uniqueness check
- **write** — create/overwrite files
- **grep** — search file contents (pure Python, no ripgrep needed)
- **find** — glob-based file search
- **ls** — list directory contents

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream (TUI intentionally omitted)

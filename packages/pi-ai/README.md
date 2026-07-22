# pi-ai

Python port of [@earendil-works/pi-ai](https://github.com/earendil-works/pi) — Unified LLM API.

## Install

```bash
pip install pi-ai
```

## Quick Start

```python
from pi_ai import stream, Context, UserMessage, Model, StreamOptions

model = Model(
    id="deepseek-chat", api="openai-completions", provider="deepseek",
    base_url="https://api.deepseek.com/v1", input=["text"],
    context_window=64000, max_tokens=8192,
)
ctx = Context(messages=[UserMessage(content="Hello")])
es = stream(model, ctx, StreamOptions(api_key="sk-..."))
async for event in es:
    if event.type == "text_delta": print(event.delta, end="")
```

## Providers

- **OpenAI** — Chat Completions (also works with DeepSeek, OpenRouter, etc.)
- **Anthropic** — Messages API with thinking (extended reasoning) support
- **Faux** — Test mock

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream

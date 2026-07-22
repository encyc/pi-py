"""模型注册表与 provider 分发。

对应上游 ``models.ts``（模型注册）+ ``api-registry.ts``（API 实现注册）+
``providers/register-builtins.ts``（内置 provider 注册）。

上游把三者拆在多文件，Python 版合并为单文件 ``models.py``，职责：

1. **模型注册表**：``provider -> id -> Model`` 二级表。
2. **API provider 注册表**：``api 名 -> ProviderStreams``（实现 stream 的对象）。
3. 内置 provider 注册：通过 ``register_builtins()`` 注入（副作用，import 时触发）。

Provider 契约（``ProviderStreams``）用 ``Protocol`` 表达。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .event_stream import EventStream
from .events import AssistantMessageEvent
from .types import AssistantMessage, Context, Model, SimpleStreamOptions, StreamOptions


@runtime_checkable
class ProviderStreams(Protocol):
    """provider 必须实现的流式契约。对应上游 ``ProviderStreams``。"""

    def stream(
        self,
        model: Model,
        context: Context,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]: ...

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]: ...


# ============================================================
# 模型注册表
# ============================================================

#: provider -> id -> Model
_model_registry: dict[str, dict[str, Model]] = {}


def register_model(model: Model) -> Model:
    """注册一个模型。返回该模型。"""
    by_id = _model_registry.setdefault(model.provider, {})
    by_id[model.id] = model
    return model


def get_model(provider: str, id: str) -> Model | None:
    """按 provider + id 取已注册模型。"""
    return _model_registry.get(provider, {}).get(id)


def list_models(provider: str | None = None) -> list[Model]:
    """列出某 provider 下（或全部）的模型。"""
    if provider is not None:
        return list(_model_registry.get(provider, {}).values())
    return [m for by_id in _model_registry.values() for m in by_id.values()]


def clear_models() -> None:
    """清空模型注册表（测试用）。"""
    _model_registry.clear()


# ============================================================
# API provider 注册表
# ============================================================

#: api 名 -> ProviderStreams
_api_registry: dict[str, ProviderStreams] = {}


def register_api_provider(api: str, impl: ProviderStreams) -> None:
    """注册一个 api 实现。"""
    _api_registry[api] = impl


def get_api_provider(api: str) -> ProviderStreams:
    """按 api 名取实现。找不到时抛 KeyError。"""
    if api not in _api_registry:
        raise KeyError(
            f"未注册的 api: {api!r}。请确保已通过 register_builtins() 注册内置 provider，"
            f"或用 register_api_provider() 注册自定义 provider。"
        )
    return _api_registry[api]


def clear_api_providers() -> None:
    """清空 api provider 注册表（测试用）。"""
    _api_registry.clear()


# ============================================================
# 内置 provider 注册（副作用）
# ============================================================

_builtins_registered = False


def register_builtins() -> None:
    """注册内置 provider 与模型（幂等）。

    顶层 ``pi_ai`` 包 import 时调用一次。测试可通过 ``_reset_for_testing`` 重置后重注册。
    """
    global _builtins_registered
    if _builtins_registered:
        return

    # OpenAI provider（stream 实现）—— 延迟导入避免循环
    from .providers.openai_provider import OPENAI_MODELS, openai_api_provider

    register_api_provider("openai-completions", openai_api_provider)
    for m in OPENAI_MODELS:
        register_model(m)

    # Anthropic provider
    from .providers.anthropic_provider import ANTHROPIC_MODELS, anthropic_api_provider

    register_api_provider("anthropic-messages", anthropic_api_provider)
    for m in ANTHROPIC_MODELS:
        register_model(m)

    # Faux（测试用 mock provider）
    from .providers.faux import FAUX_MODEL, faux_api_provider

    register_api_provider("faux", faux_api_provider)
    register_model(FAUX_MODEL)

    _builtins_registered = True


def _reset_for_testing() -> None:
    """测试专用：重置全部注册表与幂等标记。"""
    global _builtins_registered
    clear_models()
    clear_api_providers()
    _builtins_registered = False


__all__ = [
    "ProviderStreams",
    "register_model",
    "get_model",
    "list_models",
    "clear_models",
    "register_api_provider",
    "get_api_provider",
    "clear_api_providers",
    "register_builtins",
]

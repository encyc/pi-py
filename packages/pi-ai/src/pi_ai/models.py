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

from typing import Any, Literal, Protocol, overload, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .event_stream import EventStream
from .events import AssistantMessageEvent
from .types import (
    AnyModel,
    AssistantMessage,
    ClassifierModel,
    Context,
    ImageModel,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    TranscriptContext,
)


@runtime_checkable
class ProviderStreams(Protocol):
    """provider 必须实现的流式契约。对应上游 ``ProviderStreams``。

    v1.3.0（上游 v0.86.0 Breaking）：``stream`` / ``stream_simple`` 的第二个参数
    由 ``Context`` 改为归一化的 :class:`~pi_ai.types.TranscriptContext`——系统提示词
    与工具声明由 transcript 内的 system message 承载，不再挂在 context 上。
    provider 实现请用 :func:`pi_ai.transcript.get_current_system_prompt` 与
    :func:`pi_ai.transcript.get_current_tools` 读取它们。

    公开入口 :func:`pi_ai.stream.stream` / :func:`~pi_ai.stream.stream_simple`
    负责把调用方的 ``Context`` 归一化后再分发，故调用方无需感知该变更。
    """

    def stream(
        self,
        model: Model,
        context: TranscriptContext,
        options: StreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]: ...

    def stream_simple(
        self,
        model: Model,
        context: TranscriptContext,
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


# ============================================================
# 「Models 门面」的类型面（**仅类型，无实现**）
# ============================================================
#
# 对应上游 ``models.ts``。上游 ``Models`` 是 pi-ai 的运行时门面**接口**，配一个
# ``createModels()`` 实现与 ``Provider`` 对象注册表。
#
# **本端只补类型面，未移植任何实现。** 这是刻意的「类型专用切分」：
# - 不实现 ``createModels()``、不实现 ``Provider`` 运行时、不实现鉴权/目录刷新；
# - 只声明契约，供下游（如 ``pi-durable`` 的 ``TaskRuntime.models``）做类型标注。
#
# 本端现有的 ``_model_registry`` / ``get_model`` / ``list_models`` 是**扁平注册表**，
# 与上游「``Models`` 门面 + ``Provider`` 对象」是两套架构。这个错位**本批不解**——
# 它是 ``recon/01-upstream-ai-delta.md`` §0.2 指出的排期关键路径、§8 的 Q2 架构决策，
# 需要用户拍板。详见 ``PORTING.md``。
#
# ⚠️ **本 Protocol 目前只覆盖上游 24 个成员中的 9 个**（见 :data:`MODELS_OMITTED_MEMBERS`
# 与 ``PORTING.md``）。未覆盖的成员依赖**尚未移植的类型**，其中三类还与既有决策冲突：
# ``Provider``（Q2 架构面）、鉴权 8 件套（OAuth 越界面）、
# ``ApiStreamOptions<TApi>``（依赖未移植 api 模块选项的泛型映射，Python 无法等价表达）。
# 补全需用户先拍板，故此处**不猜测形状**。

#: provider 请求头。对应上游 ``ProviderHeaders``（types.ts:148）。
#: 与 ``StreamOptions.headers`` 同形（pi-py 既有用法）。
ProviderHeaders = dict[str, str | None]


class ProviderRequestOptions(BaseModel):
    """provider 请求级选项。对应上游 ``ProviderRequestOptions``（types.ts:157-...）。

    字段名遵循上游（便于逐一对照），值类型按 pi-py 既有约定映射：

    - ``signal``：上游是 ``AbortSignal``；本端沿用既有约定放 ``asyncio.Event``
      （与 :attr:`~pi_ai.types.StreamOptions.cancel_event` 同物，见 PORTING「技术备忘」）。
    - ``fetch``：上游是 ``globalThis.fetch``；本端对应 ``httpx`` 客户端
      （与 ``StreamOptions.http_client`` 同物）。
    - ``onPayload`` / ``onResponse``：函数类型，本端用 ``Any`` 承载（``exclude=True``）。

    .. note::
       上游另有 ``telemetryContext``（telemetry 未移植），此处保留同名占位字段以保持
       形状完整，值为 ``Any``。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    signal: Any = Field(default=None, exclude=True)
    telemetry_context: Any = Field(default=None, alias="telemetryContext", exclude=True)
    api_key: str | None = Field(default=None, alias="apiKey")
    fetch: Any = Field(default=None, exclude=True)
    env: dict[str, str] | None = None
    on_payload: Any = Field(default=None, alias="onPayload", exclude=True)
    on_response: Any = Field(default=None, alias="onResponse", exclude=True)
    headers: ProviderHeaders | None = None
    timeout_ms: int | None = Field(default=None, alias="timeoutMs")
    max_retries: int | None = Field(default=None, alias="maxRetries")
    max_retry_delay_ms: int | None = Field(default=None, alias="maxRetryDelayMs")


class DeferredHandle(BaseModel):
    """延迟响应的句柄。对应上游 ``DeferredHandle``（types.ts:218-229）。

    **本端只提供形状**：延迟响应特性（``StopReason="deferred"``、``fetchDeferred`` /
    ``cancelDeferred`` 的 provider 实现）未移植。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    provider: str
    model_id: str = Field(alias="modelId")
    api: str
    #: provider 私有令牌，如 response id / batch id + row id。
    id: str
    expires_at: int | None = Field(default=None, alias="expiresAt")
    poll_after_ms: int | None = Field(default=None, alias="pollAfterMs")
    #: 重建最终 assistant 消息所需的 provider 转换数据。
    data: Any = None


class DeferredFetchOptions(ProviderRequestOptions):
    """取回延迟响应的选项。对应上游 ``DeferredFetchOptions``（types.ts:266-273）。"""

    #: provider 长轮询上限（毫秒）。默认 0 = 只做一次状态检查。
    wait: int | None = None


#: 取消延迟响应的选项。上游是纯别名（types.ts:275）。
DeferredCancelOptions = ProviderRequestOptions


class ModelsRequestTransforms(BaseModel):
    """门面层请求变换。对应上游 ``ModelsRequestTransforms``（models.ts:117-121）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    #: 在 provider 分发**之前**变换组装好的 model/auth/请求头。
    transform_headers: Any = Field(default=None, alias="transformHeaders", exclude=True)


class ModelsSimpleStreamOptions(SimpleStreamOptions):
    """``SimpleStreamOptions`` 与 :class:`ModelsRequestTransforms` 的合并。

    对应上游 ``ModelsSimpleStreamOptions``（models.ts:123）。
    上游是类型交叉（``&``）；Python 用**子类**表达，语义等价且类型可用。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    transform_headers: Any = Field(default=None, alias="transformHeaders", exclude=True)


class ModelsDeferredFetchOptions(DeferredFetchOptions):
    """对应上游 ``ModelsDeferredFetchOptions``（models.ts:124）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    transform_headers: Any = Field(default=None, alias="transformHeaders", exclude=True)


class ModelsDeferredCancelOptions(ProviderRequestOptions):
    """对应上游 ``ModelsDeferredCancelOptions``（models.ts:125）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True, arbitrary_types_allowed=True)

    transform_headers: Any = Field(default=None, alias="transformHeaders", exclude=True)


#: 上游 ``Models`` 中**本批未声明**的成员（15 个），**键用上游 camelCase 名**，
#: 便于与上游 ``interface Models`` 逐项对账。
#: 以常量形式暴露，使"部分覆盖"是**显式且可测**的，而不是静默缺失。
MODELS_OMITTED_MEMBERS: dict[str, str] = {
    # --- 依赖 Provider（Q2 架构面，需拍板） ---
    "getProviders": "依赖 Provider 类型",
    "getProvider": "依赖 Provider 类型",
    # --- 依赖鉴权类型（OAuth 越界面，需拍板） ---
    "checkAuth": "依赖 AuthOperationOptions / AuthCheck",
    "getAvailable": "依赖 AuthOperationOptions",
    "getAvailableOfType": "依赖 AuthOperationOptions",
    "getAllAvailable": "依赖 AuthOperationOptions",
    "getAuth": "依赖 AuthResolutionOverrides / AuthResult",
    "login": "依赖 AuthType / AuthInteraction / LoginOptions / Credential",
    "logout": "依赖 AuthOperationOptions",
    # --- 依赖 ModelsRefreshOptions / Result + AbortSignal 映射决策 ---
    "refresh": "依赖 ModelsRefreshOptions / ModelsRefreshResult",
    # --- 依赖泛型 API 选项映射（未移植的 api 模块选项，Python 无法等价表达） ---
    "stream": "依赖 ApiStreamOptions<TApi> → ApiOptionsMap（泛型映射，需拍板）",
    "complete": "同 stream",
    # --- 依赖 capability 选项（task-33 明确延后的能力面） ---
    "generateImages": "依赖 ImagesOptions / ModelsImagesOptions",
    "classify": "依赖 ClassifierOptions / ModelsClassifierOptions",
    # --- 依赖上游 ScriptedStream/CombinedStream 等更远的类型 ---
    "streamDeferred": "依赖 ModelsDeferredFetchOptions（已备）但上游另有组合语义，待核",
}


@runtime_checkable
class Models(Protocol):
    """pi-ai 门面契约（**类型专用切分：只声明类型，未移植实现**）。

    对应上游 ``interface Models``（``models.ts:255-365``，24 个成员）。
    ``pi-durable`` 的 ``TaskRuntime.models`` 正是这个类型。

    本 Protocol **只声明**上游 24 个成员中本端已具备其依赖类型的 **9 个**；未声明的 **15 个**见
    :data:`MODELS_OMITTED_MEMBERS`（每个都附阻塞原因）。

    .. important::
       **不可实例化**（``TypeError: Protocols cannot be instantiated``）——这是
       如实声明「契约存在、实现待补」，不是桩。要判断某个对象是否满足契约，用
       ``isinstance(obj, Models)``（已 ``runtime_checkable``，按方法存在性判定）。

    与 ``pi-durable`` 实际调用面对应的成员（上游 ``harness/**`` 里的 ``models.`` 调用点）：
    :meth:`get_model`、:meth:`stream_simple`、:meth:`complete_simple`、
    :meth:`fetch_deferred`、:meth:`cancel_deferred`。
    """

    # ---- 同步读取 / 查找 ----

    def get_models(self, provider: str | None = None) -> list[Model]:
        """同步读取最近一次已知的 chat 模型。上游 ``getModels``。"""
        ...

    def get_model(self, provider: str, id: str) -> Model | None:
        """按 provider + id 同步查找 chat 模型。上游 ``getModel``。

        .. note::
           与模块级 :func:`get_model` 同名但不同物：后者是本端**扁平注册表**的
           查询函数，前者是契约方法。
        """
        ...

    @overload
    def get_models_of_type(
        self, type: Literal["chat"], provider: str | None = None
    ) -> list[Model]: ...

    @overload
    def get_models_of_type(
        self, type: Literal["image"], provider: str | None = None
    ) -> list[ImageModel]: ...

    @overload
    def get_models_of_type(
        self, type: Literal["classifier"], provider: str | None = None
    ) -> list[ClassifierModel]: ...

    def get_models_of_type(self, type: str, provider: str | None = None) -> list[Any]:
        """按模型类型同步读取。上游 ``getModelsOfType``。

        上游是 ``ModelTypeMap[TType][]``；Python 的 ``TypedDict`` 不支持 ``TypeVar``
        下标，故改用**按字面量重载**——调用方传字面量即得到精确返回类型。
        """
        ...

    @overload
    def get_model_of_type(self, type: Literal["chat"], provider: str, id: str) -> Model | None: ...

    @overload
    def get_model_of_type(
        self, type: Literal["image"], provider: str, id: str
    ) -> ImageModel | None: ...

    @overload
    def get_model_of_type(
        self, type: Literal["classifier"], provider: str, id: str
    ) -> ClassifierModel | None: ...

    def get_model_of_type(self, type: str, provider: str, id: str) -> Any:
        """按模型类型同步查找。上游 ``getModelOfType``。"""
        ...

    def get_all_models(self, provider: str | None = None) -> list[AnyModel]:
        """同步读取所有类型的模型。上游 ``getAllModels``。"""
        ...

    # ---- 流式 / 非流式请求 ----

    def stream_simple(
        self,
        model: Model,
        context: Context,
        options: ModelsSimpleStreamOptions | None = None,
    ) -> EventStream[AssistantMessageEvent, AssistantMessage]:
        """流式调用（简化版）。上游 ``streamSimple``。

        **入参是公开的** :class:`~pi_ai.types.Context`（不是 ``TranscriptContext``）——
        门面负责在分发前归一化（上游 ``model-runtime.ts`` 里就是
        ``const transcript = normalizeContext(context)``）。provider 面向的才是
        ``TranscriptContext``。
        """
        ...

    async def complete_simple(
        self,
        model: Model,
        context: Context,
        options: ModelsSimpleStreamOptions | None = None,
    ) -> AssistantMessage:
        """非流式调用（简化版）。上游 ``completeSimple``。"""
        ...

    async def fetch_deferred(
        self,
        model: Model,
        handle: DeferredHandle,
        options: ModelsDeferredFetchOptions | None = None,
    ) -> AssistantMessage:
        """取回延迟响应。上游 ``fetchDeferred``。"""
        ...

    async def cancel_deferred(
        self,
        model: Model,
        handle: DeferredHandle,
        options: ModelsDeferredCancelOptions | None = None,
    ) -> None:
        """尽力取消延迟响应。上游 ``cancelDeferred``。"""
        ...

"""``Models`` 门面类型面测试（task-42）。

**本批只补类型、不补实现**，故测试聚焦于：
1. ``Models`` 是 **Protocol**、**不可实例化**、**可 isinstance 判定**；
2. 声明了**哪些**成员、**未声明哪些**——"部分覆盖"必须是显式且可测的；
3. 前置形状类型（``ProviderHeaders`` / ``ProviderRequestOptions`` / ``DeferredHandle`` /
   ``Models*Options``）的字段与别名对齐上游；
4. 与上游 24 个成员的清单逐项对账，防止"静默缺失"或"悄悄少写"。
"""

from __future__ import annotations

import inspect
from typing import get_overloads

import pytest
from pydantic import ValidationError

from pi_ai.models import (
    MODELS_OMITTED_MEMBERS,
    DeferredCancelOptions,
    DeferredFetchOptions,
    DeferredHandle,
    Models,
    ModelsDeferredCancelOptions,
    ModelsDeferredFetchOptions,
    ModelsRequestTransforms,
    ModelsSimpleStreamOptions,
    ProviderHeaders,
    ProviderRequestOptions,
)
from pi_ai.types import SimpleStreamOptions

#: 上游 ``models.ts:255-365`` 的 ``interface Models`` 全部成员（24 个）。
#: 用于与"本端声明了什么"逐项对账。
UPSTREAM_MODELS_MEMBERS = {
    "getProviders",
    "getProvider",
    "getModels",
    "getModel",
    "getModelsOfType",
    "getModelOfType",
    "getAllModels",
    "refresh",
    "checkAuth",
    "getAvailable",
    "getAvailableOfType",
    "getAllAvailable",
    "getAuth",
    "login",
    "logout",
    "stream",
    "complete",
    "streamSimple",
    "completeSimple",
    "streamDeferred",
    "fetchDeferred",
    "cancelDeferred",
    "generateImages",
    "classify",
}

#: 本端**已声明**的成员：上游 camelCase 名 -> 本端 Python 属性名。
DECLARED_MEMBERS: dict[str, str] = {
    "getModels": "get_models",
    "getModel": "get_model",
    "getModelsOfType": "get_models_of_type",
    "getModelOfType": "get_model_of_type",
    "getAllModels": "get_all_models",
    "streamSimple": "stream_simple",
    "completeSimple": "complete_simple",
    "fetchDeferred": "fetch_deferred",
    "cancelDeferred": "cancel_deferred",
}

#: ``pi-durable`` 的 ``harness/**`` 里 ``models.`` 的**实际调用**（5 个方法）。
#: 来源：``durable/src/harness/{compaction,generation}.ts`` 的 grep 结果。
DURABLE_USED_METHODS = {
    "getModel",  # compaction.ts:120,158 / generation.ts:134,205,238,268
    "streamSimple",  # generation.ts:431
    "completeSimple",  # compaction.ts:181
    "fetchDeferred",  # generation.ts:241
    "cancelDeferred",  # generation.ts:271
}


# ============================================================
# Protocol 语义
# ============================================================


def test_models_is_a_protocol() -> None:
    assert getattr(Models, "_is_protocol", False) is True


def test_models_cannot_be_instantiated() -> None:
    """「契约存在、实现待补」——不可实例化，构造抛 TypeError。"""
    with pytest.raises(TypeError, match="Protocols cannot be instantiated"):
        Models()  # type: ignore[misc]


def test_models_is_runtime_checkable() -> None:
    """``runtime_checkable``：下游可对该契约做 isinstance 判定（按方法存在性）。"""

    class _Full:
        def get_models(self, provider=None): ...
        def get_model(self, provider, id): ...
        def get_models_of_type(self, type, provider=None): ...
        def get_model_of_type(self, type, provider, id): ...
        def get_all_models(self, provider=None): ...
        def stream_simple(self, model, context, options=None): ...
        async def complete_simple(self, model, context, options=None): ...
        async def fetch_deferred(self, model, handle, options=None): ...
        async def cancel_deferred(self, model, handle, options=None): ...

    class _Empty:
        pass

    assert isinstance(_Full(), Models) is True
    assert isinstance(_Empty(), Models) is False


def test_models_declares_exactly_the_expected_members() -> None:
    missing = {up for up, py in DECLARED_MEMBERS.items() if not hasattr(Models, py)}
    assert not missing, f"声明清单与实际不符，缺：{missing}"


def test_every_upstream_member_is_either_declared_or_documented_as_omitted() -> None:
    """核心对账：24 个上游成员 = 已声明 9 个 + 已登记省略 15 个，无遗漏、无多余。"""
    assert len(UPSTREAM_MODELS_MEMBERS) == 24
    assert set(DECLARED_MEMBERS) | set(MODELS_OMITTED_MEMBERS) == UPSTREAM_MODELS_MEMBERS
    assert set(DECLARED_MEMBERS) & set(MODELS_OMITTED_MEMBERS) == set()


def test_every_omitted_member_documents_a_reason() -> None:
    assert MODELS_OMITTED_MEMBERS, "省略清单不得为空（否则是静默缺失）"
    for member, reason in MODELS_OMITTED_MEMBERS.items():
        assert reason and len(reason) > 4, f"{member} 的阻塞原因太笼统：{reason!r}"


def test_all_methods_used_by_pi_durable_are_declared() -> None:
    """**本任务的核心目的**：``TaskRuntime.models`` 实际用到的 5 个方法必须已声明。"""
    missing = DURABLE_USED_METHODS - set(DECLARED_MEMBERS)
    assert not missing, f"pi-durable 用到但未声明的成员：{missing}"


def test_generic_members_use_literal_overloads() -> None:
    """上游 ``ModelTypeMap[TType]`` 在 Python 不可表达，故改用按字面量重载。"""
    assert len(get_overloads(Models.get_models_of_type)) == 3
    assert len(get_overloads(Models.get_model_of_type)) == 3


def test_stream_simple_takes_the_public_context_not_the_transcript() -> None:
    """门面入参是公开 ``Context``；归一化在门面内部完成（上游 model-runtime 亦然）。"""
    from pi_ai.types import Context, TranscriptContext

    hints = inspect.get_annotations(Models.stream_simple, eval_str=True)
    assert hints["context"] is Context
    assert hints["context"] is not TranscriptContext
    assert hints["options"] == ModelsSimpleStreamOptions | None


def test_deferred_methods_are_async_and_streaming_is_sync() -> None:
    """``streamSimple`` 同步返回流；``completeSimple``/``fetchDeferred``/``cancelDeferred`` 异步。"""
    assert not inspect.iscoroutinefunction(Models.stream_simple)
    assert inspect.iscoroutinefunction(Models.complete_simple)
    assert inspect.iscoroutinefunction(Models.fetch_deferred)
    assert inspect.iscoroutinefunction(Models.cancel_deferred)


# ============================================================
# 前置形状类型
# ============================================================


def test_provider_headers_shape() -> None:
    """上游 ``ProviderHeaders = Record<string, string | null>``（types.ts:148）。"""
    assert ProviderHeaders == dict[str, str | None]


def test_provider_request_options_aliases_and_conventions() -> None:
    options = ProviderRequestOptions(
        signal=None,
        apiKey="k",
        env={"A": "B"},
        headers={"X": "1"},
        timeoutMs=10,
        maxRetries=2,
        maxRetryDelayMs=100,
    )
    dumped = options.model_dump(by_alias=True)
    assert dumped["apiKey"] == "k"
    assert dumped["env"] == {"A": "B"}
    assert dumped["headers"] == {"X": "1"}
    assert dumped["timeoutMs"] == 10
    assert dumped["maxRetries"] == 2
    assert dumped["maxRetryDelayMs"] == 100
    # 函数/句柄类字段不参与序列化
    assert "onPayload" not in dumped
    assert "onResponse" not in dumped
    assert "signal" not in dumped
    # telemetry 未移植，保留同名占位字段
    assert "telemetryContext" not in dumped


def test_deferred_handle_shape_and_aliases() -> None:
    handle = DeferredHandle(
        provider="openai",
        modelId="gpt-4o",
        api="openai-responses",
        id="resp-1",
        expiresAt=1,
        pollAfterMs=2,
        data={"a": 1},
    )
    dumped = handle.model_dump(by_alias=True)
    assert dumped["modelId"] == "gpt-4o"
    assert dumped["expiresAt"] == 1
    assert dumped["pollAfterMs"] == 2
    assert dumped["data"] == {"a": 1}
    # 必填字段
    with pytest.raises(ValidationError):
        DeferredHandle(provider="p", api="a", id="i")  # 缺 modelId


def test_deferred_fetch_options_adds_wait() -> None:
    assert DeferredFetchOptions(wait=500).wait == 500
    assert DeferredFetchOptions().wait is None
    # 继承 ProviderRequestOptions
    assert isinstance(DeferredFetchOptions(apiKey="k"), ProviderRequestOptions)


def test_deferred_cancel_options_is_a_provider_request_options_alias() -> None:
    """上游是纯别名（types.ts:275）。"""
    assert DeferredCancelOptions is ProviderRequestOptions


def test_models_request_transforms_shape() -> None:
    transforms = ModelsRequestTransforms(transformHeaders=lambda h: h)
    assert callable(transforms.transform_headers)
    assert "transformHeaders" not in transforms.model_dump(by_alias=True)


@pytest.mark.parametrize(
    ("cls", "base"),
    [
        (ModelsSimpleStreamOptions, SimpleStreamOptions),
        (ModelsDeferredFetchOptions, DeferredFetchOptions),
        (ModelsDeferredCancelOptions, ProviderRequestOptions),
    ],
)
def test_models_option_types_intersect_their_base(cls: type, base: type) -> None:
    """上游是类型交叉（``&``）；Python 用子类表达，故须真是其子类。"""
    assert issubclass(cls, base)
    instance = cls()
    assert instance.transform_headers is None
    assert "transformHeaders" not in instance.model_dump(by_alias=True)


def test_models_simple_stream_options_keeps_simple_options_fields() -> None:
    options = ModelsSimpleStreamOptions(reasoning="high", toolChoice="auto")
    assert options.reasoning == "high"
    assert options.tool_choice == "auto"


# ============================================================
# 类型专用切分：不得顺手带实现
# ============================================================


def test_no_models_implementation_was_added() -> None:
    """本批**只补类型**：不得出现 ``createModels`` / 门面实现类。"""
    import pi_ai
    import pi_ai.models as models_mod

    for name in ("createModels", "create_models", "MutableModels", "InMemoryModels"):
        assert not hasattr(models_mod, name), f"不该出现实现符号 {name}"
        assert not hasattr(pi_ai, name), f"不该出现实现符号 {name}"


def test_existing_flat_registry_is_untouched() -> None:
    """既有扁平注册表保持原样（架构错位本批不解）。"""
    from pi_ai import clear_models, get_model, list_models, register_model

    assert callable(get_model) and callable(list_models)
    assert callable(register_model) and callable(clear_models)
    # 模块级 get_model 是注册表查询，不是 Models 协议方法
    assert get_model is not Models.get_model


def test_models_is_exported_from_the_package_root() -> None:
    import pi_ai

    assert pi_ai.Models is Models
    assert "Models" in pi_ai.__all__

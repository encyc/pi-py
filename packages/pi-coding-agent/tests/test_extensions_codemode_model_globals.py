"""Tier-B④-2b-2b：`createModelGlobals` —— 脚本的 `models.*`（上游 `execute.ts:664-772`）。

字段确认结果（**分支 1：有**）：pi-ai 的 `ClassifierResult` / `AssistantImages` 都有
`stop_reason`（alias `stopReason`）与 `error_message`（alias `errorMessage`），
且 `ClassifierStopReason` / `ImagesStopReason` 都是 `Literal["stop","error","aborted"]`
—— **三态齐备**，故照上游写，不需要偏离，也不需要 pi-ai 补字段。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from pi_coding_agent.extensions.codemode.execute import create_model_globals
from pi_coding_agent.extensions.codemode.tool import CodemodeNestedCall

CLASSIFIER = {"provider": "p", "id": "clf", "headers": {"X": "secret"}}
IMAGE = {"provider": "p", "id": "img", "headers": {"X": "secret"}}
CONTEXT = {
    "state": {"a": 1},
    "questions": {
        "q": {"type": "bool", "instructions": "?", "criteria": {"true": "y", "false": "n"}}
    },
}
IMAGE_CONTEXT = {"input": [{"type": "text", "text": "a cat"}]}


@dataclass
class _Result:
    stop_reason: str = "stop"
    error_message: str | None = None
    usage: Any = None
    output: list[Any] = field(default_factory=list)


class _Registry:
    """最小 `ModelRegistry`：只实现 `create_model_globals` 会调用的方法。"""

    def __init__(self, result: _Result | None = None) -> None:
        self.result = result or _Result()
        self.calls: list[tuple[str, Any]] = []

    def get_model_of_type(self, model_type: str, provider: str, model_id: str) -> Any:
        if provider != "p":
            return None
        expected = {"classifier": "clf", "image": "img"}.get(model_type)
        if model_id != expected:
            return None
        return {"provider": provider, "id": model_id, "headers": {"X": "secret"}}

    async def get_models_of_type(self, model_type: str, provider: str | None = None) -> Any:
        self.calls.append(("get_models_of_type", (model_type, provider)))
        return [dict(CLASSIFIER, model_type=model_type)]

    async def get_available_of_type(
        self, model_type: str, provider: Any = None, options: Any = None
    ) -> Any:
        self.calls.append(("get_available_of_type", (model_type, provider, options)))
        return [dict(IMAGE, model_type=model_type)]

    async def classify(self, model: Any, context: Any, options: Any = None) -> Any:
        self.calls.append(("classify", options))
        return self.result

    async def generate_images(self, model: Any, context: Any, options: Any = None) -> Any:
        self.calls.append(("generate_images", options))
        return self.result


def _globals(registry: _Registry | None = None, result: _Result | None = None):
    registry = registry or _Registry(result)
    calls: list[CodemodeNestedCall] = []
    usage: list[Any] = []
    images: list[int] = []
    published: list[int] = []
    tools = create_model_globals(
        registry, "tc", calls, lambda: published.append(1), usage.append, images.append
    )
    return {tool.name: tool for tool in tools}, calls, usage, images, registry


# ============================================================
# 形状
# ============================================================


def test_all_model_globals_are_spread():
    """`models.classify(model, context)` 靠 spread 才能拿到第二个实参。"""
    tools, *_ = _globals()
    assert list(tools) == [
        "models.getModelsOfType",
        "models.getAvailableOfType",
        "models.getModelOfType",
        "models.classify",
        "models.generateImages",
    ]
    assert all(tool.spread for tool in tools.values())


def test_to_model_info_drops_headers_for_every_catalog_method():
    """脚本看到目录条目，但**看不到 headers**（可能含凭据）。"""
    tools, *_ = _globals()
    assert "headers" not in asyncio.run(tools["models.getModelsOfType"].execute(["chat"]))[0]
    assert "headers" not in asyncio.run(tools["models.getAvailableOfType"].execute(["image"]))[0]
    assert "headers" not in tools["models.getModelOfType"].execute(["classifier", "p", "clf"])


def test_get_models_of_type_awaits_the_registry_method():
    """本仓 `get_models_of_type` 是 async（上游同步）—— 实现必须 await 它。"""
    tools, _, _, _, registry = _globals()
    asyncio.run(tools["models.getModelsOfType"].execute(["chat", "p"]))
    assert registry.calls == [("get_models_of_type", ("chat", "p"))]


def test_get_model_of_type_requires_three_strings():
    tools, *_ = _globals()
    assert tools["models.getModelOfType"].execute(["classifier", "p", "clf"])["id"] == "clf"
    assert tools["models.getModelOfType"].execute(["classifier", "nope", "clf"]) is None
    with pytest.raises(ValueError, match="expects three strings"):
        tools["models.getModelOfType"].execute(["classifier", "p"])


# ============================================================
# 三态：stop / aborted / error **不合并**
# ============================================================


def test_classify_maps_stop_to_ok_and_records_a_row():
    tools, calls, _, _, _ = _globals(result=_Result(stop_reason="stop"))
    asyncio.run(tools["models.classify"].execute([CLASSIFIER, CONTEXT]))
    assert len(calls) == 1
    assert calls[0].status == "ok"
    assert calls[0].name == "models.classify"
    assert calls[0].id == "tc/models.classify/1"  # 行 id 含调用方 id 与序号
    assert calls[0].args == "p/clf"  # 行里只显示模型，不含 prompt


def test_aborted_is_cancelled_not_error():
    """**取消与错误在 UI 上是不同提示** —— 合并掉就是语义丢失。"""
    tools, calls, *_ = _globals(result=_Result(stop_reason="aborted"))
    asyncio.run(tools["models.classify"].execute([CLASSIFIER, CONTEXT]))
    assert calls[0].status == "cancelled"


def test_error_is_error_and_carries_the_message():
    tools, calls, *_ = _globals(result=_Result(stop_reason="error", error_message="boom"))
    asyncio.run(tools["models.classify"].execute([CLASSIFIER, CONTEXT]))
    assert calls[0].status == "error"
    assert calls[0].error == "boom"


def test_usage_goes_to_add_usage_and_row_cost():
    usage = {"cost": {"total": 0.25}, "input": 1}
    tools, calls, seen, _, _ = _globals(result=_Result(usage=usage))
    asyncio.run(tools["models.classify"].execute([CLASSIFIER, CONTEXT]))
    assert seen == [usage]
    assert calls[0].cost == 0.25


# ============================================================
# 错误文案与安全
# ============================================================


def test_missing_model_mentions_the_lookup_helper():
    tools, *_ = _globals()
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(tools["models.classify"].execute([None, CONTEXT]))
    message = str(excinfo.value)
    assert "expects a classifier model as its first argument" in message
    assert "models.getModelOfType() returns undefined" in message
    assert 'models.getAvailableOfType("classifier")' in message


def test_wrong_model_type_says_what_it_actually_is():
    tools, *_ = _globals()
    with pytest.raises(ValueError, match="is a classifier model, not an image model"):
        asyncio.run(tools["models.generateImages"].execute([CLASSIFIER, IMAGE_CONTEXT]))


def test_unknown_model_is_reported_as_unknown():
    tools, *_ = _globals()
    with pytest.raises(ValueError, match='Unknown classifier model "nope/x"'):
        asyncio.run(tools["models.classify"].execute([{"provider": "nope", "id": "x"}, CONTEXT]))


def test_context_is_validated_before_the_call():
    """上下文先校验，笔误以**期望的形状**报错，而不是留到 provider 那边。"""
    tools, _, _, _, registry = _globals()
    with pytest.raises(ValueError, match="models.classify\\(\\)"):
        asyncio.run(tools["models.classify"].execute([CLASSIFIER, {"prompt": "x"}]))
    assert not [call for call in registry.calls if call[0] == "classify"], "不该调用注册表"


def test_generate_images_counts_images_the_script_never_shows():
    tools, _, _, images, _ = _globals(
        result=_Result(output=[{"type": "image"}, {"type": "text"}, {"type": "image"}])
    )
    asyncio.run(tools["models.generateImages"].execute([IMAGE, IMAGE_CONTEXT]))
    assert images == [2]

"""模型类型族公开符号测试（task-33）。

区分两类：
- **纯类型**（数据形状）：``ImageModel`` / ``ClassifierModel`` / ``ClassifierResult`` /
  ``ClassifierContext`` / ``AssistantImages`` / ``ImagesContext`` / ``ModelType`` /
  ``ModelTypeMap`` / ``AnyModel`` —— 只断言形状、别名与序列化契约。
- **带行为**：``get_model_type`` / ``is_model_type`` —— 断言判别与收窄逻辑。

上游对应：``types.ts:825-898``（类型族）、``utils/model-operations.ts``（判别）。
"""

from __future__ import annotations

from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError

from pi_ai.model_operations import get_model_type, is_model_type
from pi_ai.types import (
    AnyModel,
    AssistantImages,
    ClassifierBoolAnswer,
    ClassifierBoolQuestion,
    ClassifierChoiceAnswer,
    ClassifierChoiceQuestion,
    ClassifierContext,
    ClassifierModel,
    ClassifierResult,
    ClassifierScoreAnswer,
    ClassifierScoreQuestion,
    ImageModel,
    ImagesContext,
    Model,
    ModelCost,
    ModelType,
    ModelTypeMap,
    TextContent,
    Usage,
)


def _model(**kw: object) -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        baseUrl="https://x",
        **kw,
    )  # type: ignore[arg-type]


def _image_model(**kw: object) -> ImageModel:
    return ImageModel(
        id="img",
        name="Img",
        api="openrouter-images",
        provider="openrouter",
        baseUrl="https://x",
        **kw,
    )  # type: ignore[arg-type]


def _classifier_model(**kw: object) -> ClassifierModel:
    return ClassifierModel(
        id="jev",
        name="Jev",
        api="typesafe-system-one",
        provider="typesafe",
        baseUrl="https://x",
        **kw,
    )  # type: ignore[arg-type]


# ============================================================
# 纯类型：形状与契约
# ============================================================


def test_model_type_literal_values() -> None:
    assert set(get_args(ModelType)) == {"chat", "image", "classifier"}


def test_model_type_map_has_one_entry_per_type() -> None:
    # 注意：模块启用了 `from __future__ import annotations`，故 TypedDict 的
    # __annotations__ 在运行期是未求值的 ForwardRef，需 get_type_hints 解析。
    hints = get_type_hints(ModelTypeMap)
    assert set(hints) == {"chat", "image", "classifier"}
    # 字面量键下标是支持的（与上游 ModelTypeMap["chat"] 写法一一对应）
    assert hints["chat"] is Model


def test_any_model_is_the_three_way_union() -> None:
    assert set(get_args(AnyModel)) == {Model, ImageModel, ClassifierModel}


def test_chat_model_type_is_optional_and_defaults_to_none() -> None:
    """chat 是默认类型，故 ``type`` 可省（上游 ``Model.type?: "chat"``）。"""
    assert _model().type is None
    assert _model(type="chat").type == "chat"


def test_image_model_requires_image_type_and_output() -> None:
    im = _image_model()
    assert im.type == "image"
    assert im.output == ["image"]
    assert _image_model(output=["image", "text"]).output == ["image", "text"]


def test_classifier_model_requires_classifier_type_and_context_window() -> None:
    cm = _classifier_model(contextWindow=8192)
    assert cm.type == "classifier"
    assert cm.context_window == 8192
    # 缺省为 0（与上游 `contextWindow: number` 必填但本端给出可用缺省一致）
    assert _classifier_model().context_window == 0


def test_image_and_classifier_models_do_not_accept_chat_fields() -> None:
    """两类模型不含 chat 专有字段（``reasoning`` / ``maxTokens`` 等）。"""
    with pytest.raises(ValidationError):
        ImageModel(  # type: ignore[call-arg]
            id="i", name="i", api="a", provider="p", baseUrl="u", reasoning=True
        )
    with pytest.raises(ValidationError):
        ClassifierModel(  # type: ignore[call-arg]
            id="c", name="c", api="a", provider="p", baseUrl="u", maxTokens=5
        )


def test_shared_model_fields_are_camel_case_aliased() -> None:
    im = _image_model(cost=ModelCost(input=1.0), headers={"x": "y"})
    dumped = im.model_dump(by_alias=True)
    assert dumped["baseUrl"] == "https://x"
    assert dumped["type"] == "image"
    assert dumped["cost"]["input"] == 1.0
    assert dumped["headers"] == {"x": "y"}


# ---- ImagesContext / AssistantImages ----


def test_images_context_holds_input_blocks() -> None:
    ctx = ImagesContext(input=[TextContent(text="draw a cat")])
    assert ctx.input[0].text == "draw a cat"  # type: ignore[union-attr]


def test_assistant_images_shape_and_aliases() -> None:
    result = AssistantImages(
        api="openrouter-images",
        provider="openrouter",
        model="img",
        output=[TextContent(text="done")],
        usage=Usage(input=1, output=2, totalTokens=3),
        stopReason="stop",
        responseId="r1",
        timestamp=42,
    )
    assert result.stop_reason == "stop"
    assert result.timestamp == 42
    dumped = result.model_dump(by_alias=True)
    assert dumped["stopReason"] == "stop"
    assert dumped["responseId"] == "r1"
    assert dumped["usage"]["totalTokens"] == 3
    assert result.error_message is None


def test_assistant_images_rejects_unknown_stop_reason() -> None:
    with pytest.raises(ValidationError):
        AssistantImages(api="a", provider="p", model="m", stopReason="bogus")


# ---- ClassifierContext / ClassifierResult ----


def test_classifier_context_discriminates_questions_by_type() -> None:
    ctx = ClassifierContext(
        state={"prompt": "hi"},
        questions={
            "route": {"type": "choice", "instructions": "pick", "criteria": {"a": "A", "b": "B"}},
            "score": {"type": "score", "instructions": "rate", "criteria": ["x", "y"]},
            "safe": {
                "type": "bool",
                "instructions": "safe?",
                "criteria": {"true": "yes", "false": "no"},
            },
        },
    )
    kinds = {name: type(q).__name__ for name, q in ctx.questions.items()}
    assert kinds == {
        "route": "ClassifierChoiceQuestion",
        "score": "ClassifierScoreQuestion",
        "safe": "ClassifierBoolQuestion",
    }
    assert ctx.images is None


def test_classifier_context_accepts_optional_images() -> None:
    from pi_ai.types import ImageContent

    ctx = ClassifierContext(state={}, images=[ImageContent(data="abc", mimeType="image/png")])
    assert ctx.images is not None and len(ctx.images) == 1


def test_classifier_question_models_roundtrip() -> None:
    for q in (
        ClassifierChoiceQuestion(instructions="i", criteria={"a": "A"}),
        ClassifierScoreQuestion(instructions="i", criteria=["a"]),
        ClassifierBoolQuestion(instructions="i", criteria={"true": "y", "false": "n"}),
    ):
        assert q.model_dump()["type"] in {"choice", "score", "bool"}


def test_classifier_result_discriminates_answers_by_type() -> None:
    result = ClassifierResult(
        api="typesafe-system-one",
        provider="typesafe",
        model="jev-latest",
        answers={
            "route": {
                "type": "choice",
                "choice": "a",
                "probabilities": {"a": 0.9},
                "confidence": 0.9,
            },
            "score": {"type": "score", "score": 0.5, "confidence": 0.7},
            "safe": {"type": "bool", "probability": 0.99},
        },
        usage=Usage(input=3, output=1, totalTokens=4),
        stopReason="stop",
        timestamp=7,
    )
    kinds = {name: type(a).__name__ for name, a in result.answers.items()}
    assert kinds == {
        "route": "ClassifierChoiceAnswer",
        "score": "ClassifierScoreAnswer",
        "safe": "ClassifierBoolAnswer",
    }
    assert result.usage is not None and result.usage.total_tokens == 4
    assert result.model_dump(by_alias=True)["stopReason"] == "stop"


def test_classifier_result_supports_error_shape() -> None:
    result = ClassifierResult(
        api="a", provider="p", model="m", stopReason="error", errorMessage="boom"
    )
    assert result.stop_reason == "error"
    assert result.error_message == "boom"
    assert result.answers == {}


# ============================================================
# 带行为：get_model_type / is_model_type
# ============================================================


def test_get_model_type_returns_effective_type() -> None:
    assert get_model_type(_model()) == "chat"
    assert get_model_type(_model(type="chat")) == "chat"
    assert get_model_type(_image_model()) == "image"
    assert get_model_type(_classifier_model()) == "classifier"


def test_is_model_type_matches_effective_type() -> None:
    chat, image, classifier = _model(), _image_model(), _classifier_model()
    assert is_model_type(chat, "chat") is True
    assert is_model_type(chat, "image") is False
    assert is_model_type(image, "image") is True
    assert is_model_type(image, "chat") is False
    assert is_model_type(classifier, "classifier") is True
    assert is_model_type(classifier, "image") is False


def test_get_model_type_treats_unknown_type_as_chat() -> None:
    """防御：非三值（如历史数据里的脏值）按 chat 处理，与上游 ``?? "chat"`` 同宽松度。"""

    class _Odd:
        type = "something-else"

    assert get_model_type(_Odd()) == "chat"  # type: ignore[arg-type]


def test_narrowing_self_check_is_importable_and_runs() -> None:
    """``_narrowing_self_check`` 是 mypy 收窄自检；运行期也必须可用（纯分支）。"""
    from pi_ai.model_operations import _narrowing_self_check

    assert _narrowing_self_check(_model())[0] is not None
    assert _narrowing_self_check(_image_model())[1] is not None
    assert _narrowing_self_check(_classifier_model())[2] is not None


def test_typed_dict_typevar_subscript_is_not_supported() -> None:
    """固化一条给下游的约束：``ModelTypeMap[TType]`` 在 Python **不可用**。

    上游写法 ``ModelTypeMap[TType]``（``model-registry.ts:81`` 等）依赖 TS 的
    泛型下标；Python 的 ``TypedDict`` 只接受字面量键。下游需改用 ``@overload``
    或 :func:`is_model_type` 收窄。本用例断言"字面量键可用"，以固定这条边界。
    """
    hints = get_type_hints(ModelTypeMap)
    assert hints["image"] is ImageModel
    assert hints["classifier"] is ClassifierModel


# ============================================================
# 回归：Pydantic v2 的隐式递归类型别名限制
# ============================================================


def test_importing_pi_ai_never_hits_the_recursive_alias_recursion_error() -> None:
    """守卫：本模块能 import 即证明没有踩 Pydantic 的隐式递归别名限制。

    2026-10-18 19:45–19:52 期间 ``pi_ai`` **完全无法 import**（连带
    ``pi_agent_core`` / ``pi_coding_agent`` 及三个包的全部测试），报错为
    ``ClassifierContext`` 类创建时的 ``RecursionError: maximum recursion depth
    exceeded``。根因不是 ``ClassifierContext`` 自身，而是它引用的
    ``JsonObject`` 当时被写成**隐式递归类型别名**：

    .. code-block:: python

        JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

    Pydantic v2 在类创建期解析该别名会无限递归。**修复方式是改形态**（见下），
    不是修好了递归表达。

    本用例在**子进程**里 import，以独立证明"import 本身是健康的"——同进程内的
    其他用例无法区分"没坏"与"已被导入过"。
    """

    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "import pi_ai; import pi_ai.types"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"pi_ai import 失败：\n{proc.stderr}"
    assert "RecursionError" not in proc.stderr


def test_json_value_is_deliberately_not_recursive() -> None:
    """固化当前形态与其代价，防止有人"顺手改回递归"而再次炸掉全包 import。

    本端偏离：``JsonValue``/``JsonObject`` 退化为 ``Any`` / ``dict[str, Any]``。
    如需**忠实**的递归 JSON 类型且仍被 Pydantic 接受，有两条可行路径
    （本端评估后未采用，理由见 ``PORTING.md``）：

    1. PEP 695 ``type JsonValue = ...``（需 Python 3.12+；本包声明 >=3.11）
    2. ``typing_extensions.TypeAliasType``（**已验证 Pydantic 可用**，但会让本包
       新增一个未声明的直接依赖；它目前仅由 pydantic 传递引入）

    与既有同类偏离一致：``ToolCall.arguments`` 亦为 ``dict[str, Any]``。
    """
    from typing import Any

    from pi_ai.types import JsonObject, JsonValue

    assert JsonValue is Any
    assert JsonObject == dict[str, Any]
    # 「字面量键下标」这条下游约束仍然成立
    assert get_type_hints(ModelTypeMap)["chat"] is Model

"""模型类型判别。

对应上游 ``utils/model-operations.ts``（v1.3.0）。

本模块**只提供判别/守卫**，不提供任何模型能力实现。上游同文件里的
``assertChatModel`` / ``assertImageModel`` / ``assertClassifierModel`` /
``assertClassifierInputSupported`` / ``imageErrorResult`` / ``classifierErrorResult``
**本端未移植**（未在 task-33 的符号清单内；需要时各约 3 行，可按需补）。
"""

from __future__ import annotations

from typing import Literal, TypeGuard, overload

from .types import AnyModel, ClassifierModel, ImageModel, Model, ModelType

__all__ = ["get_model_type", "is_model_type"]


def get_model_type(model: AnyModel) -> ModelType:
    """返回模型的**有效**类型。对应上游 ``getModelType``。

    chat 是默认类型，故不带 ``type`` 的模型即 chat 模型（上游用 ``model.type ?? "chat"``
    表达同一语义）。
    """
    explicit = getattr(model, "type", None)
    return explicit if explicit in ("chat", "image", "classifier") else "chat"


@overload
def is_model_type(model: AnyModel, type: Literal["chat"]) -> TypeGuard[Model]: ...


@overload
def is_model_type(model: AnyModel, type: Literal["image"]) -> TypeGuard[ImageModel]: ...


@overload
def is_model_type(model: AnyModel, type: Literal["classifier"]) -> TypeGuard[ClassifierModel]: ...


def is_model_type(model: AnyModel, type: str) -> bool:
    """运行时判别的模型类型收窄。对应上游 ``isModelType``。

    **为什么用 ``@overload`` 而不是上游的 ``model is ModelTypeMap[TType]``**：
    Python 的 ``TypedDict`` 不支持 ``TypeVar`` 下标，因此无法表达
    "``type`` 参数决定返回类型" 的泛型守卫。改用按字面量重载的三条签名，
    在 ``if is_model_type(m, "image"):`` 之后 mypy 能正确把 ``m`` 收窄为
    ``ImageModel``（配合 ``--strict`` 验证过）。用变量传 ``type`` 时收窄不可用，
    这属于 Python 类型系统的固有限制。

    典型用法：

    .. code-block:: python

        if is_model_type(model, "image"):
            reveal_type(model)  # ImageModel
    """
    return get_model_type(model) == type


# 供 mypy 校验收窄效果的自检（仅类型层面，运行期不做任何事）
def _narrowing_self_check(
    model: AnyModel,
) -> tuple[Model | None, ImageModel | None, ClassifierModel | None]:
    chat: Model | None = None
    image: ImageModel | None = None
    classifier: ClassifierModel | None = None
    if is_model_type(model, "chat"):
        chat = model
    elif is_model_type(model, "image"):
        image = model
    elif is_model_type(model, "classifier"):
        classifier = model
    return chat, image, classifier

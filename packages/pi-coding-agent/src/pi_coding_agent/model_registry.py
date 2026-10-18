"""模型目录、分类器与图像生成的运行时句柄（**最小面 + 跨包阻塞登记**）。

对应上游 ``core/model-registry.ts``（244 行）中被内置扩展用到的那部分：
``getModelsOfType`` / ``getAvailableOfType`` / ``getModelOfType`` / ``classify`` / ``generateImages``
（上游 ``extensions/codemode/tool.ts`` 的 ``CodemodeModelRuntime`` 正是这五个的 ``Pick``）。

**⚠️ 跨包阻塞（未解决）**：上游这些方法的参数与返回类型来自 ``pi-ai`` 的**多模型类型面**
（``ModelType`` / ``AnyModel`` / ``ClassifierModel`` / ``ImageModel`` / ``ClassifierContext`` /
``ClassifierResult`` / ``ImagesContext`` / ``AssistantImages`` / ``isModelType`` / ``lazyStream``）。
实测 ``pi-py`` 的 ``pi-ai`` **一个都没有**
（``grep -rq 'ClassifierModel|ImageModel|ModelType'`` 全部无命中）。

因此本模块**只声明形状**：模型/结果类型以 ``Any`` 代替，方法名、参数顺序、异步性照上游。
这**不是**桩——:class:`ModelRegistry` 是 :class:`~typing.Protocol`，只描述契约、不含实现，
谁也不能"调用"它；等 ``pi-ai`` 补齐上述符号后，把这些 ``Any`` 换成真实类型即可，
其余代码不用动。

**未移植**（无消费者）：``getProviderDisplayName`` / ``hasConfiguredAuth`` / 流式与凭证相关方法。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

__all__ = ["ModelRegistry"]


@runtime_checkable
class ModelRegistry(Protocol):
    """模型目录 + 分类器 + 图像生成的运行时句柄。

    ``Any`` 标注的入参/返回见模块 docstring 的「跨包阻塞」说明。
    """

    async def get_models_of_type(
        self, model_type: str, provider: str | None = None
    ) -> Sequence[Any]:
        """某类型的全部模型（``model_type`` 为 ``"chat"`` / ``"image"`` / ``"classifier"``）。"""
        ...

    async def get_available_of_type(
        self, model_type: str, provider: str | None = None, options: Any = None
    ) -> Sequence[Any]:
        """某类型中 provider 凭据可用的模型。"""
        ...

    def get_model_of_type(self, model_type: str, provider: str, model_id: str) -> Any:
        """按 provider + id 取某类型的模型。"""
        ...

    async def classify(self, model: Any, context: Any, options: Any = None) -> Any:
        """用分类器模型对结构化状态分类，使用请求时解析的认证。**永不 reject。**"""
        ...

    async def generate_images(self, model: Any, context: Any, options: Any = None) -> Any:
        """用图像模型生成图像，使用请求时解析的认证。**永不 reject。**"""
        ...

"""`pi_durable` 的共享 **pytest fixture**。

⚠️ 普通的测试 helper **不在这里** —— 本仓**根目录已经有一个 `conftest.py`**，
测试文件里的 `from conftest import ...` 会解析到**根那个**。
⇒ helper 放在 :mod:`pi_durable.testing.harness_e2e`，按包路径导入。
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _serialization_warnings_are_errors() -> Any:
    """⚠️ **序列化警告当错误看。**

    ``PydanticSerializationUnexpectedValue`` 意味着**存下去的东西与声明的类型不符**——
    而它**不抛**，记录**照常写进存储**，读出来**也能用**（`1.0 == 1`）。
    直到某个严格类型的消费者（别的语言、别的版本、JSON Schema 校验）拿到才炸。

    **实证**：这条 fixture 上线**当天**就抓到了 `_RuntimeView.now()` 里的
    `float(self._now())` —— 宿主给的是 int 毫秒，而 `timestamp` 字段是 `int`。
    **「让静默的东西变响亮」的最好证明，就是它上线当天就抓到东西。**
    """
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=UserWarning, message=".*Pydantic.*")
        yield


@pytest.fixture(autouse=True)
def _clear_calls() -> Any:
    """每个用例前清空真工具的调用记录。"""
    from pi_durable.testing.harness_e2e import CALLS

    CALLS.clear()
    yield
    CALLS.clear()

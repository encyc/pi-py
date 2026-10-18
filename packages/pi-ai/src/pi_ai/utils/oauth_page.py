"""兼容转发：上游子路径 ``pi-ai/utils/oauth-page`` 的 Python 对应写法。

**不要在本文件添加任何逻辑。** 实现位于 :mod:`pi_ai.oauth_page`；此处仅转发，
使下列两种写法都可用，且解析到**同一批函数对象**（``is`` 同一性成立）：

.. code-block:: python

    from pi_ai.oauth_page import oauth_success_html, oauth_error_html   # 规范位置
    from pi_ai.utils.oauth_page import oauth_success_html, oauth_error_html  # 上游路径对齐
    from pi_ai import oauth_success_html, oauth_error_html              # 顶层便捷导出
"""

from __future__ import annotations

from ..oauth_page import oauth_error_html, oauth_success_html

__all__ = ["oauth_error_html", "oauth_success_html"]

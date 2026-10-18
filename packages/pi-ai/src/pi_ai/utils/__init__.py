"""``pi_ai.utils``：与上游 ``src/utils/`` 目录名对齐的**兼容命名空间**。

上游把本模块发布为 npm 子路径 ``@earendil-works/pi-ai/utils/oauth-page``。
Python 没有子路径导出机制，为让下游按上游路径书写 import 也能工作，
此处提供薄转发（见 :mod:`pi_ai.utils.oauth_page`）。

**规范位置仍是** :mod:`pi_ai.oauth_page`：本端此前的移植约定是把上游
``utils/*`` 各模块平铺在 ``pi_ai/`` 下（``transcript`` / ``estimate`` /
``headers`` / ``text`` / ``json_parse`` / ``transform_messages`` 皆如此），
以便与上游文件一一对应。本子包不承载任何逻辑。
"""

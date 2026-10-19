"""骨架冒烟测试：验证包可被 import 且元数据正确。"""

from importlib.metadata import version as dist_version


def test_import() -> None:
    import pi_ai

    # 与**包元数据**比对，而不是硬编码字面量。硬编码会在版本升级时漂移，而这条
    # 断言真正要保证的不变量是「``__version__`` 与 ``pyproject`` 声明一致」——
    # 本包的 pyproject 曾经是 0.85.1 而 ``__version__`` 是 1.3.0，两边不一致，
    # 而硬编码的断言"通过"了，正是这个不变量失守却没被发现的原因。
    assert pi_ai.__version__ == dist_version("pi-py-ai")
    # 版本号即上游对齐点（见 README「版本号约定」），两者必须一致。
    assert pi_ai.__upstream_ref__ == "earendil-works/pi@v1.3.0"

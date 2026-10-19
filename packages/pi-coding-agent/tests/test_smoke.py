"""骨架冒烟测试：验证包可被 import 且元数据正确。"""

from importlib.metadata import version as dist_version


def test_import() -> None:
    import pi_coding_agent

    # 与**包元数据**比对而不是硬编码字面量：后者会在版本升级时漂移，而这条断言
    # 真正要保证的不变量是「``__version__`` 与 ``pyproject`` 声明一致」。
    assert pi_coding_agent.__version__ == dist_version("pi-py-coding-agent")
    assert pi_coding_agent.__upstream_ref__ == "earendil-works/pi@v1.3.0"

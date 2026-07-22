"""骨架冒烟测试：验证包可被 import 且元数据正确。"""


def test_import() -> None:
    import pi_coding_agent

    assert pi_coding_agent.__version__
    assert pi_coding_agent.__upstream_ref__ == "earendil-works/pi@v0.81.1"

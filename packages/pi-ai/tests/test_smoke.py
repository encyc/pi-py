"""骨架冒烟测试：验证包可被 import 且元数据正确。"""


def test_import() -> None:
    import pi_ai

    assert pi_ai.__version__
    assert pi_ai.__upstream_ref__ == "earendil-works/pi@v0.81.1"

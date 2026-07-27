"""骨架冒烟测试：验证包可被 import 且元数据正确。"""


def test_import() -> None:
    import pi_server

    assert pi_server.__version__ == "0.82.1"
    assert pi_server.__upstream_ref__ == "earendil-works/pi@v0.82.1"

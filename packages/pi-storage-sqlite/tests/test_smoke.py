"""骨架冒烟测试：验证包可被 import 且元数据正确。"""


def test_import() -> None:
    import pi_storage_sqlite

    assert pi_storage_sqlite.__version__
    assert pi_storage_sqlite.__upstream_ref__ == "earendil-works/pi@v0.81.1"

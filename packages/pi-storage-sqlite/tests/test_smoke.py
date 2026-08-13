"""骨架冒烟测试：验证包可被 import 且元数据正确。"""


def test_import() -> None:
    import pi_storage_sqlite

    assert pi_storage_sqlite.__version__ == "0.84.1"
    assert pi_storage_sqlite.__upstream_ref__ == "earendil-works/pi@v0.84.1"

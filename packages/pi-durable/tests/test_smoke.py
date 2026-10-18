"""骨架冒烟测试：验证包可被 import、元数据正确、上游入口点存在。"""


def test_import() -> None:
    import pi_durable

    assert pi_durable.__version__ == "1.3.0"
    assert pi_durable.__upstream_ref__ == "earendil-works/pi@v1.3.0"


def test_env_subpaths_are_importable() -> None:
    from pi_durable.env import ExecutionEnv, FileError, Result, ok
    from pi_durable.env.node import NodeExecutionEnv
    from pi_durable.env.watch import NodeFileWatcher

    assert ExecutionEnv is not None
    assert NodeExecutionEnv is not None
    assert NodeFileWatcher is not None
    assert FileError is not None
    assert Result is not None
    assert ok(1).value == 1


def test_testing_subpath_is_importable() -> None:
    from pi_durable.testing import (
        PytestAssertions,
        create_env_conformance,
        env_conformance_cases,
    )

    assert PytestAssertions is not None
    assert create_env_conformance is not None
    assert env_conformance_cases is not None

"""骨架冒烟测试：验证包可被 import 且元数据正确。"""

from importlib.metadata import version as dist_version


def test_import() -> None:
    import pi_chord

    # 与**包元数据**比对而不是硬编码字面量：后者会在版本升级时漂移，而这条断言
    # 真正要保证的不变量是「``__version__`` 与 ``pyproject`` 声明一致」。
    assert pi_chord.__version__ == dist_version("pi-py-chord")
    assert pi_chord.__upstream_ref__ == "earendil-works/pi@v1.3.0"
    assert pi_chord.Context is not None
    assert pi_chord.ContextKey is not None


def test_context_subpath_is_importable() -> None:
    from pi_chord.context import BACKGROUND_CONTEXT, TODO_CONTEXT, with_abort_signal

    assert BACKGROUND_CONTEXT is not None
    assert TODO_CONTEXT is not None
    assert callable(with_abort_signal)

"""骨架冒烟测试：验证包可被 import 且元数据正确。"""


def test_import() -> None:
    import pi_chord

    assert pi_chord.__version__ == "1.3.0"
    assert pi_chord.__upstream_ref__ == "earendil-works/pi@v1.3.0"
    assert pi_chord.Context is not None
    assert pi_chord.ContextKey is not None


def test_context_subpath_is_importable() -> None:
    from pi_chord.context import BACKGROUND_CONTEXT, TODO_CONTEXT, with_abort_signal

    assert BACKGROUND_CONTEXT is not None
    assert TODO_CONTEXT is not None
    assert callable(with_abort_signal)

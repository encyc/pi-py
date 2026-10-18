"""globals 的 ``spread``：多参全局必须收到**全部**实参。

上游的发现类全局与 ``models.*`` 全部声明 ``spread: true``
（``coding-agent/src/extensions/codemode/execute.ts:601/623/635/771``），
语义由 prelude 决定：``serialize(spread ? args : args[0])``（``prelude.js:155``）。

``_globals_json()`` 曾经**硬编码** ``spread: False``，于是多参全局**静默丢掉**第一个以外的实参：
``two("a", "b")`` → 宿主只收到 ``"a"``，**且不报错**。
后果最重的是 ``searchTools(query, {limit, namespace})``：返回默认 8 条、不按 namespace 过滤，
脚本与用户都看不到任何异常。

> **这个 bug 藏在序列化里，任何只测「属性能设上」的测试都看不见它。**
> 所以下面测的是**上游那个真实失败场景**：带两个实参调用，断言宿主收到**两个**。
"""

from __future__ import annotations

from pi_codemode import CodemodeSandbox, CodemodeTool


def _sandbox(*tools: CodemodeTool) -> CodemodeSandbox:
    return CodemodeSandbox(globals=list(tools))


def _spy(sink: list[object]):
    def collect(args: object) -> str:
        sink.append(args)
        return "ok"

    return collect


def test_spread_true_global_receives_every_argument():
    """上游的真实场景：两参全局 + 脚本带两个实参 → 宿主必须收到**两个**。"""
    seen: list[object] = []
    sandbox = _sandbox(CodemodeTool(name="two", execute=_spy(seen), spread=True))
    try:
        assert sandbox.execute('return two("a", "b")').ok
    finally:
        sandbox.close()
    assert seen == [["a", "b"]], f"第二个实参被丢掉了：{seen}"


def test_spread_false_global_still_receives_only_the_first_argument():
    """反面：既有语义**没被改坏** —— 单参全局仍只拿到第一个实参。"""
    seen: list[object] = []
    sandbox = _sandbox(CodemodeTool(name="one", execute=_spy(seen)))
    try:
        assert sandbox.execute('return one("a", "b")').ok
    finally:
        sandbox.close()
    assert seen == ["a"], seen


def test_spread_defaults_to_false():
    """默认值不动：``tools.<name>(x)`` 的形状不受影响。"""
    assert CodemodeTool(name="x", execute=lambda _args: None).spread is False
    assert CodemodeTool(name="x", execute=lambda _args: None, spread=True).spread is True


def test_spread_global_keeps_object_options():
    """``searchTools(query, {limit, namespace})`` 的真形状：第二个实参是对象。"""
    seen: list[object] = []
    sandbox = _sandbox(CodemodeTool(name="searchTools", execute=_spy(seen), spread=True))
    try:
        assert sandbox.execute(
            'return searchTools("docs", { limit: 3, namespace: "mcp__docs" })'
        ).ok
    finally:
        sandbox.close()
    assert seen == [["docs", {"limit": 3, "namespace": "mcp__docs"}]], seen


def test_spread_global_with_no_arguments_receives_an_empty_list():
    """边界：不传实参时宿主收到空列表（而不是 ``None`` 或第一个实参）。"""
    seen: list[object] = []
    sandbox = _sandbox(CodemodeTool(name="none_", execute=_spy(seen), spread=True))
    try:
        assert sandbox.execute("return none_()").ok
    finally:
        sandbox.close()
    assert seen == [[]], seen

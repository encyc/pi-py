"""``uuidv7`` 测试。

对应上游 ``packages/ai/src/utils/uuid.ts``（v1.3.0，45 行；v0.85.1 → v1.3.0 未变更）。

覆盖：48 位毫秒时间戳、版本位、变体位、**单调性**（同毫秒内与跨毫秒）、显式时间戳
的"不钳制"语义、入参校验、以及两条导入路径共享同一份生成器状态。

**保真度验证**：``tests/data/uuid-reference.json`` 由上游 TypeScript 实现以**确定性
随机流**驱动产出（生成脚本 ``tests/data/generate_uuid_reference.mjs``）。本文件以
**完全相同的字节流**驱动 Python 实现并逐字节比对——位布局（含序列在字节 6–11 上的
跨字节打包）因此由机器判定，而非人读代码推断。
"""

from __future__ import annotations

import json
import re
import uuid as stdlib_uuid
from itertools import count
from pathlib import Path

import pytest

import pi_ai.uuid as uuid_mod
from pi_ai.uuid import MAX_SEQUENCE, MAX_UUID_V7_TIMESTAMP, uuidv7

_REFERENCE_PATH = Path(__file__).parent / "data" / "uuid-reference.json"
_REFERENCE = json.loads(_REFERENCE_PATH.read_text(encoding="utf-8"))
_REFERENCE_CASES = _REFERENCE["cases"]

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

#: 与 generate_uuid_reference.mjs 中**必须一致**的确定性随机流公式。
#: 第 c 次调用（c 从 0 起）的第 i 字节 = (c * 31 + i * 7 + 11) & 0xff
_DETERMINISTIC_RANDOM_SPEC = "byte(c, i) = (c * 31 + i * 7 + 11) & 0xff"


@pytest.fixture(autouse=True)
def _reset_generator_state():
    """每个用例前后都重置模块级状态，避免用例间相互影响。"""
    uuid_mod._reset_for_testing()
    yield
    uuid_mod._reset_for_testing()


def _install_deterministic_random(monkeypatch: pytest.MonkeyPatch) -> None:
    """用确定性字节流替换 ``os.urandom``（与 JS 生成脚本同公式）。"""
    counter = count()

    def fake_urandom(n: int) -> bytes:
        c = next(counter)
        return bytes(((c * 31 + i * 7 + 11) & 0xFF) for i in range(n))

    monkeypatch.setattr(uuid_mod.os, "urandom", fake_urandom)


# ============================================================
# 与上游逐字节比对
# ============================================================


def test_reference_fixture_provenance() -> None:
    prov = _REFERENCE["_provenance"]
    assert prov["upstreamRef"] == "earendil-works/pi@v1.3.0"
    assert prov["deterministicRandom"] == _DETERMINISTIC_RANDOM_SPEC, (
        "参考样本的随机流公式与本测试不一致——比对将失去意义"
    )
    assert len(_REFERENCE_CASES) == 12


def test_matches_upstream_byte_for_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心保真度断言：与上游真实实现逐字节一致。"""
    _install_deterministic_random(monkeypatch)
    for case in _REFERENCE_CASES:
        assert uuidv7(case["ts"]) == case["id"], f"ts={case['ts']}"


# ============================================================
# 结构：48 位时间戳 + 版本位 + 变体位
# ============================================================


def test_format_and_version_and_variant_bits() -> None:
    value = uuidv7()
    assert _UUID_RE.match(value), value
    assert len(value) == 36
    parsed = stdlib_uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == stdlib_uuid.RFC_4122


@pytest.mark.parametrize(
    "timestamp",
    [0, 1, 255, 256, 65535, 1_700_000_000_000, MAX_UUID_V7_TIMESTAMP],
)
def test_timestamp_field_roundtrips_exactly(timestamp: int) -> None:
    """前 48 位必须精确等于传入的毫秒时间戳（大端）。"""
    parsed = stdlib_uuid.UUID(uuidv7(timestamp))
    assert parsed.int >> 80 == timestamp


def test_timestamp_of_ordinary_call_is_current_time() -> None:
    import time

    before = time.time_ns() // 1_000_000
    parsed = stdlib_uuid.UUID(uuidv7())
    after = time.time_ns() // 1_000_000
    assert before <= (parsed.int >> 80) <= after


def test_version_nibble_is_byte6_high_nibble() -> None:
    """版本位位于字节 6 的高 4 位（不是别处）——位布局回归。"""
    parsed = stdlib_uuid.UUID(uuidv7(1_700_000_000_000))
    assert (parsed.bytes[6] >> 4) == 0x7


def test_variant_bits_are_byte8_high_bits() -> None:
    """变体位位于字节 8 的高 2 位，值 ``0b10``。"""
    parsed = stdlib_uuid.UUID(uuidv7(1_700_000_000_000))
    assert (parsed.bytes[8] >> 6) == 0b10


# ============================================================
# 单调性
# ============================================================


def test_same_millisecond_ids_strictly_increase() -> None:
    """同毫秒连发必须**严格**递增（上游靠 41 位序列每次 +1 实现）。"""
    ts = 1_700_000_000_000
    ids = [uuidv7(ts) for _ in range(50)]
    assert len(set(ids)) == 50, "出现重复"
    assert ids == sorted(ids), "同毫秒内未严格递增"
    ivals = [stdlib_uuid.UUID(i).int for i in ids]
    assert all(b > a for a, b in zip(ivals, ivals[1:], strict=False)), "整数序未严格递增"


def test_sequence_increments_one_per_call_across_milliseconds() -> None:
    """序列**每次调用**都 +1，而不是只在同毫秒内递增（上游实现如此）。"""
    first = stdlib_uuid.UUID(uuidv7(1_700_000_000_000)).bytes
    second = stdlib_uuid.UUID(uuidv7(1_700_000_000_001)).bytes

    def seq_of(raw: bytes) -> int:
        return (
            ((raw[6] & 0x0F) << 37)
            | (raw[7] << 29)
            | ((raw[8] & 0x3F) << 23)
            | (raw[9] << 15)
            | (raw[10] << 7)
            | (raw[11] >> 1)
        )

    assert seq_of(second) == seq_of(first) + 1


def test_ordinary_calls_never_go_backwards_in_time() -> None:
    """普通调用（不传时间戳）的 48 位时间戳单调不减。"""
    stamps = [stdlib_uuid.UUID(uuidv7()).int >> 80 for _ in range(200)]
    assert stamps == sorted(stamps)


def test_ordinary_calls_are_strictly_increasing_ids() -> None:
    ids = [stdlib_uuid.UUID(uuidv7()).int for _ in range(200)]
    assert all(b > a for a, b in zip(ids, ids[1:], strict=False))


def test_string_sort_order_matches_byte_order() -> None:
    """字符串排序 == 字节序（下游按 id 排序依赖此性质）。"""
    ids = [uuidv7(1_700_000_000_000) for _ in range(20)]
    assert ids == sorted(ids)


# ============================================================
# 显式时间戳的"不钳制"语义（follower ids）
# ============================================================


def test_explicit_timestamp_is_not_clamped() -> None:
    """上游注释："A supplied timestamp is preserved for follower ids"。

    即显式时间戳**按原样**写入，即便它早于前一次普通调用——此时整体不再单调，
    这是有意语义。
    """
    later = 1_800_000_000_000
    earlier = 1_600_000_000_000
    uuidv7(later)
    assert stdlib_uuid.UUID(uuidv7(earlier)).int >> 80 == earlier


def test_explicit_timestamp_does_not_advance_the_internal_clock() -> None:
    """显式调用不更新"上次普通调用时间戳"，故不影响后续普通调用的钳制基准。"""
    future = 4_000_000_000_000
    uuidv7(future)
    assert uuid_mod._last_ordinary_timestamp == -1
    # 随后的普通调用用的是当前时间，而不是被 future 抬高的基准
    import time

    now_ms = time.time_ns() // 1_000_000
    assert (stdlib_uuid.UUID(uuidv7()).int >> 80) <= now_ms


def test_ordinary_call_after_explicit_future_stamp_is_not_forced_forward() -> None:
    """反向验证：若错误地在显式调用时更新基准，普通调用会被抬到 future。"""
    uuidv7(4_000_000_000_000)
    ts = stdlib_uuid.UUID(uuidv7()).int >> 80
    assert ts < 4_000_000_000_000


# ============================================================
# 入参校验
# ============================================================


@pytest.mark.parametrize("bad", [-1, -1_000, MAX_UUID_V7_TIMESTAMP + 1, 1 << 48])
def test_out_of_range_timestamp_raises_value_error(bad: int) -> None:
    with pytest.raises(ValueError, match="between 0 and"):
        uuidv7(bad)


@pytest.mark.parametrize("bad", [True, False])
def test_bool_is_not_an_integer(bad: bool) -> None:
    """``Number.isInteger(true)`` 为 false；Python 的 ``bool`` 是 ``int`` 子类，须显式排除。"""
    with pytest.raises(ValueError, match="must be an integer"):
        uuidv7(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [1.5, float("nan"), float("inf"), float("-inf")])
def test_non_integral_or_non_finite_float_raises(bad: float) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        uuidv7(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("ok", [1.0, 1_700_000_000_000.0])
def test_integral_float_is_accepted_like_number_isinteger(ok: float) -> None:
    """``Number.isInteger(1.0) === true`` —— 整数值的 float 与上游同样接受。"""
    assert stdlib_uuid.UUID(uuidv7(ok)).int >> 80 == int(ok)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["1", object(), [1], {}, b"1"])
def test_non_numeric_raises_value_error(bad: object) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        uuidv7(bad)  # type: ignore[arg-type]


def test_boundary_values_are_accepted() -> None:
    assert stdlib_uuid.UUID(uuidv7(0)).int >> 80 == 0
    assert stdlib_uuid.UUID(uuidv7(MAX_UUID_V7_TIMESTAMP)).int >> 80 == MAX_UUID_V7_TIMESTAMP


def test_sequence_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """41 位序列耗尽时报错（上游 ``RangeError("UUIDv7 generator sequence exhausted")``）。"""
    uuidv7(1)  # 初始化 _sequence
    monkeypatch.setattr(uuid_mod, "_sequence", MAX_SEQUENCE)
    with pytest.raises(ValueError, match="sequence exhausted"):
        uuidv7(1)


# ============================================================
# 序列初始化 / 随机性
# ============================================================


def test_sequence_is_initialised_from_random_bytes_on_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次调用用随机字节 1–5 初始化序列（上游语义），而非从 0 开始。"""
    monkeypatch.setattr(uuid_mod.os, "urandom", lambda n: bytes(range(n)))
    uuidv7(1_700_000_000_000)
    expected = (1 << 32) | (2 << 24) | (3 << 16) | (4 << 8) | 5
    assert uuid_mod._sequence == expected


def test_two_fresh_processes_produce_different_ids() -> None:
    """随机初始化意味着不同"进程"（重置状态）产出不同 id —— 不是确定性计数器。"""
    first = {uuidv7(1_700_000_000_000) for _ in range(5)}
    uuid_mod._reset_for_testing()
    second = {uuidv7(1_700_000_000_000) for _ in range(5)}
    assert first != second


def test_low_bit_of_byte11_comes_from_random_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """字节 11 的最低位保留**首次随机**的 bit0（上游 ``bytes[11] & 0x01``）。"""
    monkeypatch.setattr(uuid_mod.os, "urandom", lambda n: bytes(range(n)))
    parsed = stdlib_uuid.UUID(uuidv7(1_700_000_000_000))
    # bytes(range(16))[11] == 11 -> bit0 == 1
    assert parsed.bytes[11] & 0x01 == 1

    uuid_mod._reset_for_testing()
    monkeypatch.setattr(uuid_mod.os, "urandom", lambda n: bytes([0] * 10 + [0, 0xFE] + [0] * 4))
    parsed2 = stdlib_uuid.UUID(uuidv7(1_700_000_000_000))
    assert parsed2.bytes[11] & 0x01 == 0


def test_ids_are_unique_over_many_calls() -> None:
    ids = {uuidv7() for _ in range(2000)}
    assert len(ids) == 2000


def test_sequence_never_exceeds_41_bits() -> None:
    for _ in range(100):
        uuidv7(1_700_000_000_000)
        assert uuid_mod._sequence is not None
        assert 0 <= uuid_mod._sequence <= MAX_SEQUENCE


# ============================================================
# 导入路径
# ============================================================


def test_all_three_import_paths_resolve_to_the_same_object() -> None:
    from pi_ai import uuidv7 as top
    from pi_ai.utils.uuid import uuidv7 as alias

    assert uuidv7 is top is alias


def test_both_import_paths_share_the_same_generator_state() -> None:
    """关键：转发模块**不得**复制模块级序列状态，否则两条路径会产生两个独立序列、
    破坏单调性。此处混合调用两条路径并断言整体仍严格递增。"""
    from pi_ai.utils.uuid import uuidv7 as alias

    ts = 1_700_000_000_000
    ids = [uuidv7(ts), alias(ts), uuidv7(ts), alias(ts)]
    ivals = [stdlib_uuid.UUID(i).int for i in ids]
    assert all(b > a for a, b in zip(ivals, ivals[1:], strict=False)), f"跨路径未共享序列：{ids}"


def test_alias_module_has_no_private_state() -> None:
    """转发模块不应出现 ``_sequence`` / ``_last_ordinary_timestamp`` 等状态副本。"""
    from pi_ai.utils import uuid as alias_mod

    assert alias_mod.__all__ == ["uuidv7"]
    assert not hasattr(alias_mod, "_sequence")
    assert not hasattr(alias_mod, "_last_ordinary_timestamp")


def test_reset_for_testing_is_not_public_api() -> None:
    import pi_ai.uuid as mod

    assert mod.__all__ == ["uuidv7"]
    assert "_reset_for_testing" not in mod.__all__

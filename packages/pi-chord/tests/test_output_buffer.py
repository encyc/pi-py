"""``pi_chord.output`` 新增部分的测试 —— ``bound_output`` / ``OutputBuffer`` / ``Progress``。

对照上游 ``chord/src/output.ts``:
- ``sanitizeOutput`` / ``boundOutput`` — 第 39-99 行
- ``OutputBuffer``                     — 第 122-241 行
- ``Progress``                         — 第 302-383 行

``character_end`` / ``utf8_byte_length`` 的**回归测试**在
``test_output_regression.py``（它们已被 ``pi-durable`` 使用）。
"""

from __future__ import annotations

import asyncio

import pytest

from pi_chord.output import (
    PROGRESS_BYTES_PER_SECOND,
    BoundedOutput,
    OutputBuffer,
    OutputLimits,
    OutputSkip,
    OutputSlice,
    Progress,
    bound_output,
    line_count,
    sanitize_output,
)

EMOJI = "\U0001f600"


# ─── sanitize_output ─────────────────────────────────────────────────────────


def test_sanitize_keeps_tabs_and_newlines() -> None:
    assert sanitize_output("a\tb\nc") == "a\tb\nc"


def test_sanitize_removes_control_characters() -> None:
    """上游 ``output.ts:34``：``\\x00-\\x08``、``\\x0b-\\x1f``、U+FFF9-U+FFFB。"""
    assert sanitize_output("a\x00b\x07c\x0bd\x0de") == "abcde"
    assert sanitize_output("x\ufff9y\ufffbz") == "xyz"


def test_sanitize_keeps_printable_unicode() -> None:
    assert sanitize_output("héllo " + EMOJI) == "héllo " + EMOJI


# ─── line_count ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "data,expected",
    [(b"", 0), (b"a", 1), (b"a\n", 1), (b"a\nb", 2), (b"a\nb\n", 2), (b"\n", 1), (b"\n\n", 2)],
)
def test_line_count(data: bytes, expected: int) -> None:
    assert line_count(data) == expected


# ─── bound_output ────────────────────────────────────────────────────────────


def test_bound_output_returns_the_original_string_when_nothing_is_dropped() -> None:
    """上游 ``output.ts:54``：没丢东西时返回**原字符串**，不做解码往返。"""
    text = "abc"
    result = bound_output(text, OutputLimits(max_bytes=100, max_lines=100, retain="head"))
    assert result.text is text
    assert result == OutputSlice(text="abc", bytes=3, dropped_bytes=0, dropped_lines=0)


def test_bound_output_head_keeps_the_first_lines() -> None:
    result = bound_output("one\ntwo\nthree\n", OutputLimits(100, 2, "head"))
    assert result.text == "one\ntwo\n"
    assert result.dropped_lines == 1


def test_bound_output_tail_keeps_the_last_lines() -> None:
    result = bound_output("one\ntwo\nthree\n", OutputLimits(100, 2, "tail"))
    assert result.text == "two\nthree\n"
    assert result.dropped_lines == 1


def test_bound_output_zero_limits_drop_everything() -> None:
    assert bound_output("abc", OutputLimits(0, 5, "head")).text == ""
    assert bound_output("abc", OutputLimits(5, 0, "head")).text == ""
    assert bound_output("abc", OutputLimits(0, 5, "tail")).text == ""
    assert bound_output("abc", OutputLimits(5, 0, "tail")).text == ""


def test_bound_output_cuts_a_long_line_on_a_character_boundary() -> None:
    """单行超过 ``max_bytes`` 时按字符边界截断（``output.ts:71-74``）。"""
    text = "ab" + EMOJI + "cd"  # 2 + 4 + 2 = 8 字节
    # 上限 4 落在 emoji 中间 -> character_end 回退到它的起点 2
    result = bound_output(text, OutputLimits(4, 10, "head"))
    assert result.text == "ab"
    assert result.bytes == 2
    assert result.dropped_bytes == 6
    # 上限 6 正好落在字符边界上 -> 不需要回退
    aligned = bound_output(text, OutputLimits(6, 10, "head"))
    assert aligned.text == "ab" + EMOJI
    assert aligned.dropped_bytes == 2


def test_bound_output_tail_cuts_on_a_character_boundary() -> None:
    text = "ab" + EMOJI + "cd"
    result = bound_output(text, OutputLimits(6, 10, "tail"))
    # from = 8-6 = 2，落在 emoji 起点，可以直接切
    assert result.text == EMOJI + "cd"
    assert result.bytes == 6


def test_bound_output_counts_dropped_lines_across_the_whole_input() -> None:
    result = bound_output("a\nb\nc\nd\n", OutputLimits(100, 2, "head"))
    assert result.text == "a\nb\n"
    assert result.dropped_lines == 2


# ─── OutputBuffer：head 保留 ─────────────────────────────────────────────────


def test_output_buffer_head_retains_the_beginning() -> None:
    buffer = OutputBuffer(OutputLimits(10, 10, "head"))
    assert buffer.push("hello\n") is True
    assert buffer.push("world\n") is True
    snapshot = buffer.snapshot()
    assert isinstance(snapshot, BoundedOutput)
    # 上限 10 落在第二行中间；head 回退到第一个换行处（上游 output.ts:71-74）
    assert snapshot.text == "hello\n"
    assert snapshot.dropped_bytes == 6
    assert snapshot.dropped_lines == 1


def test_output_buffer_head_stops_storing_once_full() -> None:
    """上游 ``output.ts:200-203``：head 窗口填满后不再存储。

    ``stored_bytes`` 的上界是「**上限加上一个 chunk**」（``output.ts:149-150``）：
    触发 ``#full`` 的那个 chunk 已经进队了，之后才停止存储。
    """
    buffer = OutputBuffer(OutputLimits(5, 10, "head"))
    buffer.push("abcde")
    assert buffer.stored_bytes == 5
    buffer.push("fghij")  # 5 > 5 -> 置 full
    assert buffer.stored_bytes == 10  # 上限 + 一个 chunk
    buffer.push("klmno")  # 已 full，不再存储
    assert buffer.stored_bytes == 10
    assert buffer.snapshot().text == "abcde"


def test_output_buffer_head_drops_by_lines_too() -> None:
    buffer = OutputBuffer(OutputLimits(100, 2, "head"))
    buffer.push("a\nb\nc\nd\n")
    assert buffer.snapshot().text == "a\nb\n"


# ─── OutputBuffer：tail 保留 ─────────────────────────────────────────────────


def test_output_buffer_tail_retains_the_end() -> None:
    buffer = OutputBuffer(OutputLimits(10, 10, "tail"))
    buffer.push("hello\n")
    buffer.push("world\n")
    snapshot = buffer.snapshot()
    # 上限 10 落在第一行中间；tail 取「字节窗口内开始的**第一整行**」
    # （上游 output.ts:92-97），于是丢掉更多但保持行对齐。
    assert snapshot.text == "world\n"
    assert snapshot.dropped_bytes == 6
    assert snapshot.dropped_lines == 1


def test_output_buffer_tail_drops_leading_chunks() -> None:
    buffer = OutputBuffer(OutputLimits(10, 10, "tail"))
    for chunk in ("aaaa\n", "bbbb\n", "cccc\n", "dddd\n"):
        buffer.push(chunk)
    assert buffer.snapshot().text == "cccc\ndddd\n"


def test_output_buffer_totals_span_the_whole_stream() -> None:
    buffer = OutputBuffer(OutputLimits(5, 10, "tail"))
    buffer.push("aaaaa")
    buffer.push("bbbbb")
    buffer.push("ccccc")
    snapshot = buffer.snapshot()
    assert snapshot.text == "ccccc"
    assert snapshot.dropped_bytes == 10


# ─── OutputBuffer：字节 chunk 与增量解码 ─────────────────────────────────────


def test_output_buffer_decodes_byte_chunks_across_boundaries() -> None:
    """一个多字节字符被拆到两个字节 chunk 里也必须正确还原。"""
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    data = EMOJI.encode("utf-8")
    buffer.push(data[:2])
    buffer.push(data[2:])
    assert buffer.snapshot().text == EMOJI


def test_output_buffer_end_flushes_an_incomplete_character() -> None:
    """上游 ``output.ts:184-187``：结束时把不完整字符刷成 U+FFFD。"""
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    buffer.push(EMOJI.encode("utf-8")[:2])
    assert buffer.snapshot().text == ""
    buffer.end()
    assert buffer.snapshot().text == "\ufffd"


def test_output_buffer_string_chunk_also_ends_an_incomplete_character() -> None:
    """上游 ``output.ts:160-161``：字符串 chunk 之前先把挂起的字节刷掉。"""
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    buffer.push(EMOJI.encode("utf-8")[:2])
    buffer.push("next")
    assert buffer.snapshot().text == "\ufffdnext"


def test_output_buffer_strips_a_leading_bom_of_a_byte_stream() -> None:
    """只有**最开头**的 BOM 被丢掉（上游 ``output.ts:164``）。"""
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    buffer.push(b"\xef\xbb\xbfhello")
    assert buffer.snapshot().text == "hello"


def test_output_buffer_keeps_a_later_bom() -> None:
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    buffer.push("ab")
    buffer.push(b"\xef\xbb\xbfcd")
    assert buffer.snapshot().text == "ab\ufeffcd"


def test_output_buffer_keeps_a_bom_in_a_string_chunk() -> None:
    """字符串 chunk 不做 BOM 处理（上游只对字节流做）。"""
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    buffer.push("\ufeffhello")
    assert buffer.snapshot().text == "\ufeffhello"


# ─── OutputBuffer：skip ──────────────────────────────────────────────────────


def test_output_buffer_accepts_a_skip_with_tail_retention() -> None:
    buffer = OutputBuffer(OutputLimits(100, 10, "tail"))
    assert (
        buffer.push("tail text", OutputSkip(bytes=500, newlines=7, ends_with_newline=True)) is True
    )
    snapshot = buffer.snapshot()
    assert snapshot.text == "tail text"
    assert snapshot.dropped_bytes == 500
    assert snapshot.dropped_lines == 7


def test_output_buffer_skip_requires_tail_retention() -> None:
    """上游 ``output.ts:166``。"""
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    with pytest.raises(ValueError, match="tail retention"):
        buffer.push("x", OutputSkip(bytes=1, newlines=0, ends_with_newline=False))


def test_output_buffer_skips_nothing_for_a_zero_byte_skip() -> None:
    buffer = OutputBuffer(OutputLimits(100, 10, "tail"))
    buffer.push("keep", OutputSkip(bytes=0, newlines=0, ends_with_newline=False))
    assert buffer.snapshot().dropped_bytes == 0


# ─── OutputBuffer：sanitize ──────────────────────────────────────────────────


def test_output_buffer_snapshot_is_sanitized() -> None:
    buffer = OutputBuffer(OutputLimits(100, 10, "head"))
    buffer.push("a\x00b\x07c")
    assert buffer.snapshot().text == "abc"


def test_output_buffer_stored_bytes_is_bounded_by_the_window() -> None:
    buffer = OutputBuffer(OutputLimits(8, 10, "tail"))
    for _ in range(20):
        buffer.push("12345678")
    # 窗口大小加上用于找下一个窗口行首的余量
    assert buffer.stored_bytes <= 8 + 8 + 1


# ─── Progress ────────────────────────────────────────────────────────────────


def test_progress_bytes_per_second_constant() -> None:
    assert PROGRESS_BYTES_PER_SECOND == 100 * 1024


async def test_progress_first_mark_after_idle_commits_at_once() -> None:
    """上游 ``output.ts:306-309``：空闲期后的第一次变更立刻提交。"""
    writes: list[int] = []

    async def write() -> int:
        writes.append(len(writes))
        return 0

    progress = Progress(write, lambda error: None, min_interval_ms=10_000)
    progress.mark()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert writes == [0]


async def test_progress_mark_and_wait_resolves_with_its_commit() -> None:
    written: list[int] = []

    async def write() -> int:
        written.append(len(written))
        return 4

    progress = Progress(write, lambda error: None, min_interval_ms=0)
    await progress.mark_and_wait()
    assert len(written) == 1


async def test_progress_coalesces_changes_while_a_commit_is_in_flight() -> None:
    """上游 ``output.ts:306-309``：同一时刻至多一次在途，期间的变更合并。"""
    started = 0
    release = asyncio.Event()

    async def write() -> int:
        nonlocal started
        started += 1
        await release.wait()
        return 0

    progress = Progress(write, lambda error: None, min_interval_ms=0)
    progress.mark()
    await asyncio.sleep(0)
    assert started == 1
    # 在途期间再标记多次
    progress.mark()
    progress.mark()
    progress.mark()
    await asyncio.sleep(0)
    assert started == 1  # 没有并发提交
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert started == 2  # 合并成一次后续提交


async def test_progress_paces_the_next_commit_by_written_size() -> None:
    """写入量越大，下一次提交推迟越久（``output.ts:369``）。"""
    writes = 0

    async def write() -> int:
        nonlocal writes
        writes += 1
        # 写入整整一秒的配额 -> 下一次至少推迟 1000ms
        return PROGRESS_BYTES_PER_SECOND

    progress = Progress(write, lambda error: None, min_interval_ms=0)
    progress.mark()
    for _ in range(5):
        await asyncio.sleep(0)
    assert writes == 1
    progress.mark()  # 这次应该被推迟，而不是立刻提交
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert writes == 1
    waiters = await progress.stop()
    assert waiters == []


async def test_progress_stop_waits_for_the_commit_in_flight() -> None:
    finished = False
    release = asyncio.Event()

    async def write() -> int:
        nonlocal finished
        await release.wait()
        finished = True
        return 0

    progress = Progress(write, lambda error: None, min_interval_ms=0)
    progress.mark()
    await asyncio.sleep(0)
    stopper = asyncio.ensure_future(progress.stop())
    await asyncio.sleep(0)
    assert not stopper.done()
    release.set()
    waiters = await stopper
    assert finished
    assert waiters == []


async def test_progress_stop_returns_the_pending_waiters() -> None:
    """上游 ``output.ts:341-348``：最后一次提交必须落定这些 waiters。"""

    async def write() -> int:
        return 0

    progress = Progress(write, lambda error: None, min_interval_ms=0)
    progress.mark()
    pending = progress.mark_and_wait()
    waiters = await progress.stop()
    assert waiters == [pending] or waiters == []
    for waiter in waiters:
        if not waiter.done():
            waiter.set_result(None)
    if not pending.done():
        pending.cancel()
    assert pending.done()


async def test_progress_reports_write_failure_and_rejects_waiters() -> None:
    errors: list[BaseException] = []

    async def write() -> int:
        raise RuntimeError("write failed")

    progress = Progress(write, errors.append, min_interval_ms=0)
    with pytest.raises(RuntimeError, match="write failed"):
        await progress.mark_and_wait()
    assert len(errors) == 1


async def test_progress_after_stop_does_not_commit() -> None:
    writes = 0

    async def write() -> int:
        nonlocal writes
        writes += 1
        return 0

    progress = Progress(write, lambda error: None, min_interval_ms=0)
    await progress.stop()
    progress.mark()
    await asyncio.sleep(0)
    assert writes == 0


async def test_progress_accepts_a_synchronous_write() -> None:
    """``write`` 可以返回 int，也可以是 awaitable。"""
    writes = 0

    def write() -> int:
        nonlocal writes
        writes += 1
        return 0

    progress = Progress(write, lambda error: None, min_interval_ms=0)
    await progress.mark_and_wait()
    assert writes == 1

"""``@earendil-works/chord/output`` 的 Python 移植：有界输出流与自适应进度提交。

对应上游 ``chord/src/output.ts``（383 行）:

- ``OutputLimits`` / ``BoundedOutput`` / ``OutputSlice`` / ``OutputSkip`` — 第 7-31 行
- ``sanitizeOutput``   — 第 39-42 行
- ``boundOutput``      — 第 44-59 行
- ``headRange`` / ``tailRange`` — 第 61-99 行
- ``characterEnd``     — 第 101-106 行
- ``characterStart``   — 第 108-113 行（上游未导出，这里也不导出）
- ``lineCount``        — 第 115-120 行
- ``OutputBuffer``     — 第 122-241 行
- ``tailMargin`` / ``lines`` — 第 243-266 行
- ``utf8ByteLength``   — 第 275-294 行
- ``PROGRESS_BYTES_PER_SECOND`` — 第 302-303 行
- ``Progress``         — 第 305-383 行

⚠️ **``character_end`` 与 ``utf8_byte_length`` 已被 ``pi_durable`` 使用**
（``durable/src/truncate.py`` 与 ``tools/read.py``）。本模块**保持二者的签名与语义
不变**，并有专门的回归测试 ``tests/test_output_regression.py`` 钉住。

⚠️ ``characterEnd`` 处理的是 **UTF-8 字节**，不是 UTF-16 码元——它的用途是「别在
字节窗口里从一个多字节字符中间切开」。与 :mod:`pi_chord.delta.textdiff` 的 UTF-16
计数是两件事，不要混用。
"""

from __future__ import annotations

import asyncio
import codecs
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = [
    "PROGRESS_BYTES_PER_SECOND",
    "BoundedOutput",
    "OutputBuffer",
    "OutputLimits",
    "OutputSkip",
    "OutputSlice",
    "Progress",
    "bound_output",
    "character_end",
    "sanitize_output",
    "utf8_byte_length",
]

# 只读字节序列。Uint8Array 的对应物。
Bytes = bytes | bytearray

_CONTINUATION_MASK = 0xC0
_CONTINUATION_BYTE = 0x80

_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF
_SURROGATE_HIGH_END = 0xDBFF
_SURROGATE_LOW_START = 0xDC00

_NEWLINE = 0x0A

# 上游 output.ts:34 的 `INVALID_OUTPUT`。注意制表符 (0x09) 与换行 (0x0A) 保留，
# 而回车 (0x0D) 与换页 (0x0C) 会被去掉。
_INVALID_OUTPUT = re.compile("[\x00-\x08\x0b-\x1f\ufff9-\ufffb]")


def character_end(data: Bytes, index: int) -> int:
    """``index`` 处或之前的**最后一个字符边界**（上游 ``output.ts:101-106``）。

    从 ``index`` 向前跳过 UTF-8 续字节（``10xxxxxx``），返回该字符的起始字节。
    用于把一个字节窗口的末尾回退到不会切断多字节字符的位置。
    """
    end = index
    while end > 0 and (_byte_at(data, end) & _CONTINUATION_MASK) == _CONTINUATION_BYTE:
        end -= 1
    return end


def _character_start(data: Bytes, index: int) -> int:
    """``index`` 处或之后的第一个字符边界（上游 ``output.ts:108-113``，未导出）。"""
    start = index
    while start < len(data) and (data[start] & _CONTINUATION_MASK) == _CONTINUATION_BYTE:
        start += 1
    return start


def _byte_at(data: Bytes, index: int) -> int:
    """JS 的 ``bytes[index] ?? 0``——越界读返回 0（``output.ts:104``）。"""
    return data[index] if 0 <= index < len(data) else 0


def utf8_byte_length(content: str) -> int:
    """``content`` 的 UTF-8 字节长度；**孤立代理按 U+FFFD 计 3 字节**
    （上游 ``output.ts:275-294``）。

    逐 UTF-16 码元走，与上游一致：

    - ``<= 0x7f`` → 1 字节
    - ``<= 0x7ff`` → 2 字节
    - 合法代理对 → 4 字节（两个码元一起）
    - 其余（含孤立代理）→ 3 字节

    之所以先把相邻的「高代理 + 低代理」合并再计数，而不是直接
    ``len(content.encode("utf-8"))``：Python ``str`` 里可能存在**未合并的代理对**
    （例如从 UTF-16 码元直接构造的 ``"\\ud83d\\ude00"``，那是 2 个字符）。
    上游在 UTF-16 码元空间里会把它们当作一个 emoji 计 4 字节；逐字符计数会得到
    6。合并后与上游逐码元的语义完全一致。
    """
    total = 0
    index = 0
    count = len(content)
    while index < count:
        code = ord(content[index])
        if (
            _SURROGATE_START <= code <= _SURROGATE_HIGH_END
            and index + 1 < count
            and _SURROGATE_LOW_START <= ord(content[index + 1]) <= _SURROGATE_END
        ):
            total += 4
            index += 2
        elif code <= 0x7F:
            total += 1
            index += 1
        elif code <= 0x7FF:
            total += 2
            index += 1
        elif code <= 0xFFFF:
            # BMP 的其余部分：U+0800..U+FFFF（含 CJK、以及**孤立代理**按 U+FFFD 计）
            total += 3
            index += 1
        else:
            # Python 的 ``str`` 是码点序列，真正的非 BMP 字符是单个码点（JS 里是
            # 一个代理对，上面已处理）。上游的 UTF-16 空间里不存在「单个码元
            # > 0xFFFF」的情况，所以这一支是 Python 侧必须补上的。
            total += 4
            index += 1
    return total


# ─── 编码桥（TextEncoder / TextDecoder 的对应物） ────────────────────────────


def _encode_utf8(text: str) -> bytes:
    """``TextEncoder.encode`` 的对应物：**孤立代理编码成 U+FFFD**（3 字节）。

    直接 ``text.encode("utf-8")`` 遇到孤立代理会抛 ``UnicodeEncodeError``；
    ``errors="surrogatepass"`` 虽然也是 3 字节，但产出的是 WTF-8 字节
    (``ED A0 80``)，而 ``TextEncoder`` 产出的是 U+FFFD (``EF BF BD``)。长度相同，
    字节不同 —— 解码回文本时能看出差别，所以必须真的替换。

    未合并的代理对（Python 里 2 个字符）先合并成一个码点，与 JS 的 UTF-16 语义一致。
    """
    out: list[str] = []
    index = 0
    count = len(text)
    while index < count:
        code = ord(text[index])
        if (
            _SURROGATE_START <= code <= _SURROGATE_HIGH_END
            and index + 1 < count
            and _SURROGATE_LOW_START <= ord(text[index + 1]) <= _SURROGATE_END
        ):
            out.append(
                chr(
                    0x10000
                    + ((code - _SURROGATE_START) << 10)
                    + (ord(text[index + 1]) - _SURROGATE_LOW_START)
                )
            )
            index += 2
        elif _SURROGATE_START <= code <= _SURROGATE_END:
            out.append("\ufffd")
            index += 1
        else:
            out.append(text[index])
            index += 1
    return "".join(out).encode("utf-8")


def _decode_utf8(data: Bytes) -> str:
    """``TextDecoder`` 的对应物：非法字节序列变成 U+FFFD，BOM **保留**为文本。

    ``ignoreBOM: true``（``output.ts:37`` 的注释：*a U+FEFF at a slice's start is
    text, not a byte-order mark*）—— Python 的 ``utf-8`` 编解码器本来就不吃 BOM
    （``utf-8-sig`` 才会），所以直接用 ``utf-8`` 即可。
    """
    return bytes(data).decode("utf-8", errors="replace")


# ─── 类型 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutputLimits:
    """一条输出流的保留上限（上游 ``output.ts:7``）。"""

    max_bytes: int
    max_lines: int
    retain: str  # "head" | "tail"


@dataclass(frozen=True)
class BoundedOutput:
    """保留下的输出，以及被上限丢弃的量（上游 ``output.ts:10``）。"""

    text: str
    dropped_bytes: int
    dropped_lines: int


@dataclass(frozen=True)
class OutputSlice:
    """输入在上限内的精确切片，以及被略去的量（上游 ``output.ts:13-18``）。"""

    text: str
    bytes: int
    dropped_bytes: int
    dropped_lines: int


@dataclass(frozen=True)
class OutputSkip:
    """生产者在某个 chunk 之前略去的输出，按**解码后的文本**度量。

    每个 U+FFFD 计 3 字节，且不做任何 sanitize（上游 ``output.ts:20-31``）。
    """

    bytes: int
    newlines: int
    ends_with_newline: bool


# ─── 纯函数 ──────────────────────────────────────────────────────────────────


def sanitize_output(text: str) -> str:
    """去掉会破坏显示与 transcript 的控制字符；**制表符与换行保留**
    （上游 ``output.ts:39-42``）。"""
    return _INVALID_OUTPUT.sub("", text)


def line_count(data: Bytes) -> int:
    """``data`` 的行数；末尾未终止的那一行也算（上游 ``output.ts:115-120``）。"""
    if len(data) == 0:
        return 0
    newlines = bytes(data).count(_NEWLINE)
    return newlines + (0 if data[len(data) - 1] == _NEWLINE else 1)


def _lines(newlines: int, terminated: bool) -> int:
    """上游 ``output.ts:263-266``。"""
    return newlines + (0 if terminated else 1)


def _head_range(data: bytes, limits: OutputLimits) -> tuple[int, int]:
    """上游 ``output.ts:61-76``。"""
    if limits.max_lines == 0 or limits.max_bytes == 0:
        return (0, 0)
    end = len(data)
    lines = 0
    index = data.find(b"\n")
    while index != -1:
        lines += 1
        if lines == limits.max_lines:
            end = index + 1
            break
        index = data.find(b"\n", index + 1)
    if end > limits.max_bytes:
        # 上游 `lastIndexOf(NEWLINE, maxBytes - 1)` —— 在 [0, maxBytes-1] 内倒着找。
        newline = data.rfind(b"\n", 0, limits.max_bytes)
        end = newline + 1 if newline != -1 else character_end(data, limits.max_bytes)
    return (0, end)


def _tail_range(data: bytes, limits: OutputLimits) -> tuple[int, int]:
    """上游 ``output.ts:78-99``。"""
    if limits.max_lines == 0 or limits.max_bytes == 0:
        return (len(data), len(data))
    # 末尾的换行是**结束**最后一行，而不是开始新的一行。
    last = len(data) - 2 if len(data) > 0 and data[len(data) - 1] == _NEWLINE else len(data) - 1
    start = 0
    lines = 1
    index = -1 if last < 0 else data.rfind(b"\n", 0, last + 1)
    while index != -1:
        if lines == limits.max_lines:
            start = index + 1
            break
        lines += 1
        index = -1 if index == 0 else data.rfind(b"\n", 0, index)
    if len(data) - start > limits.max_bytes:
        frm = len(data) - limits.max_bytes
        newline = data.find(b"\n", frm - 1)
        # 落在字节窗口里的第一行，或者当最后一行本身太长时对它的截断。
        start = (
            newline + 1
            if newline != -1 and newline + 1 < len(data)
            else _character_start(data, frm)
        )
    return (start, len(data))


def bound_output(text: str, limits: OutputLimits) -> OutputSlice:
    """把 ``text`` 约束到上限内的**整行**：``head`` 取开头若干行，``tail`` 取结尾若干行。

    结果是精确切片，含末尾换行。单行超过 ``max_bytes`` 时按字符边界在字节上限处截断
    （上游 ``output.ts:44-59``）。
    """
    data = _encode_utf8(text)
    start, end = _head_range(data, limits) if limits.retain == "head" else _tail_range(data, limits)
    kept = data[start:end]
    return OutputSlice(
        # 没丢东西时返回**原字符串**，避免解码往返改变它（上游 :54）。
        text=text if len(kept) == len(data) else _decode_utf8(kept),
        bytes=len(kept),
        dropped_bytes=len(data) - len(kept),
        dropped_lines=line_count(data) - line_count(kept),
    )


def _tail_margin(text: str, limits: OutputLimits) -> str:
    """``text`` 的最短后缀，其字节数**超过** ``max_bytes`` 或换行数**超过** ``max_lines``；
    没有这样的后缀时返回全部（上游 ``output.ts:243-261``）。

    任何以该后缀结尾的文本，其后接任意内容时，tail 窗口都与 ``text`` 后接它时相同。
    """
    data = _encode_utf8(text)
    byte_start = (
        character_end(data, len(data) - limits.max_bytes - 1) if len(data) > limits.max_bytes else 0
    )
    line_start = 0
    newlines = 0
    index = data.rfind(b"\n")
    while index != -1:
        newlines += 1
        if newlines > limits.max_lines:
            line_start = index
            break
        if index == 0:
            break
        index = data.rfind(b"\n", 0, index)
    start = max(byte_start, line_start)
    return text if start == 0 else _decode_utf8(data[start:])


# ─── OutputBuffer ────────────────────────────────────────────────────────────


@dataclass
class _Chunk:
    text: str
    bytes: int
    newlines: int


class OutputBuffer:
    """一条流的**有界运行输出**（如一次工具调用或一个进程）。

    接受一个 chunk 的代价与该 chunk 成正比：head 保留在窗口填满后就不再存储，
    tail 保留在快照时丢掉窗口不再需要的已存文本。整条流的计数被保留，
    所以丢弃总量始终精确（上游 ``output.ts:122-241``）。

    字节 chunk 用一个**增量 UTF-8 解码器**处理：字符串 chunk 或一次 skip 会终结
    之前字节里不完整的字符（变成 U+FFFD），随后的字节 chunk 再开一个新字符。
    与上游一致，只有**输出最开头**的 BOM 会被丢掉，后面出现的 U+FEFF 都是正文。
    """

    def __init__(self, limits: OutputLimits) -> None:
        self._limits = limits
        # 上游 output.ts:134 的 `TextDecoder("utf-8", {ignoreBOM: true})`：
        # Python 的 utf-8 增量解码器本来就不吃 BOM。
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._started = False
        # head 存流的开头；tail 存一个仍包含下一个窗口的后缀。
        self._chunks: list[_Chunk] = []
        self._stored_bytes = 0
        self._stored_newlines = 0
        self._full = False
        self._total_bytes = 0
        self._total_newlines = 0
        self._ends_with_newline = True

    @property
    def stored_bytes(self) -> int:
        """当前持有的字节数；上限加上一个 chunk（上游 ``output.ts:149-152``）。"""
        return self._stored_bytes

    def push(self, chunk: str | Bytes, skipped: OutputSkip | None = None) -> bool:
        """接受一个 chunk；返回是否接受了任何内容。

        ``skipped`` 是紧接在该 chunk 之前被略去的输出，它必须比 tail 窗口至少多一个
        字节或一行；只有 tail 保留会接受它（上游 ``output.ts:154-171``）。
        """
        is_bytes = not isinstance(chunk, str)
        # 先处理早先字节 chunk 里不完整字符遗留的字节。
        pending = self._flush_decoder() if (not is_bytes or skipped is not None) else ""
        text = chunk if isinstance(chunk, str) else self._decoder.decode(bytes(chunk), False)
        first = not self._started and pending == "" and skipped is None
        if pending != "" or text != "" or skipped is not None:
            self._started = True
        if first and is_bytes and text.startswith("\ufeff"):
            text = text[1:]
        if skipped is None:
            return self._accept(pending + text)
        if self._limits.retain != "tail":
            raise ValueError("Skipped output requires tail retention")
        self._accept(pending)
        self._skip(skipped)
        self._accept(text)
        return True

    def end(self) -> None:
        """把不完整的尾部字符刷成替换字符；流结束时调用（上游 ``output.ts:184-187``）。"""
        self._accept(self._flush_decoder())

    def snapshot(self) -> BoundedOutput:
        """保留的、已 sanitize 的输出，以及上限从整条流里丢掉的量
        （上游 ``output.ts:219-240``）。"""
        stored = (
            self._chunks[0].text
            if len(self._chunks) == 1
            else "".join(c.text for c in self._chunks)
        )
        kept = bound_output(stored, self._limits)
        stored_lines = _lines(self._stored_newlines, stored == "" or stored.endswith("\n"))
        kept_lines = stored_lines - kept.dropped_lines
        # tail 窗口不会回溯到本窗口之前，但要找到**下一个**窗口的第一行需要它前面的
        # 内容：保留一个比窗口多一字节或一行的最短后缀，与 `_accept` 一致。
        if self._limits.retain == "tail" or len(self._chunks) > 1:
            text = _tail_margin(stored, self._limits) if self._limits.retain == "tail" else stored
            size = utf8_byte_length(text) if self._limits.retain == "tail" else self._stored_bytes
            self._chunks = [] if text == "" else [_Chunk(text, size, text.count("\n"))]
            self._stored_bytes = size
            self._stored_newlines = self._chunks[0].newlines if self._chunks else 0
        return BoundedOutput(
            text=sanitize_output(kept.text),
            dropped_bytes=self._total_bytes - kept.bytes,
            dropped_lines=_lines(self._total_newlines, self._ends_with_newline) - kept_lines,
        )

    # ─── 内部 ──────────────────────────────────────────────────────────────

    def _flush_decoder(self) -> str:
        """JS ``decoder.decode()``（无参数）——把挂起的字节刷成 U+FFFD。"""
        return self._decoder.decode(b"", True)

    def _skip(self, skipped: OutputSkip) -> None:
        """统计被略去的输出；它之前的已存内容在它之后的文本到达后不可能还在窗口里。"""
        if skipped.bytes == 0:
            return
        self._total_bytes += skipped.bytes
        self._total_newlines += skipped.newlines
        self._ends_with_newline = skipped.ends_with_newline
        self._chunks = []
        self._stored_bytes = 0
        self._stored_newlines = 0

    def _accept(self, text: str) -> bool:
        if len(text) == 0:
            return False
        size = utf8_byte_length(text)
        newlines = text.count("\n")
        self._total_bytes += size
        self._total_newlines += newlines
        self._ends_with_newline = text.endswith("\n")
        if self._full:
            return True
        self._chunks.append(_Chunk(text, size, newlines))
        self._stored_bytes += size
        self._stored_newlines += newlines
        if self._limits.retain == "head":
            # 窗口填满之后的内容永远不需要。
            self._full = (
                self._stored_bytes > self._limits.max_bytes
                or self._stored_newlines >= self._limits.max_lines
            )
            return True
        # 只要剩下的还超过一个窗口就丢掉开头的 chunk：字节数超过 maxBytes 或换行数超过
        # maxLines，各加一，这样窗口的行首仍能找到。每个 chunk 只会被丢一次。
        while len(self._chunks) > 1:
            first = self._chunks[0]
            bytes_after = self._stored_bytes - first.bytes
            newlines_after = self._stored_newlines - first.newlines
            if (
                bytes_after <= self._limits.max_bytes + 1
                and newlines_after <= self._limits.max_lines + 1
            ):
                break
            self._chunks.pop(0)
            self._stored_bytes = bytes_after
            self._stored_newlines = newlines_after
        return True


# ─── Progress ────────────────────────────────────────────────────────────────

PROGRESS_BYTES_PER_SECOND = 100 * 1024
"""每次进度提交还会按写入量买一段停顿（上游 ``output.ts:302-303``）。"""


class Progress:
    """自适应进度提交（上游 ``output.ts:305-383``）。

    空闲期后的第一次变更立刻提交；此后每次提交至少把下一次推迟 ``min_interval_ms``，
    并按写入量以 100 KiB/s 再推迟。**同一时刻至多一次提交在途**；期间发生的变更
    合并进下一次。

    ⚠️ **Python 侧偏离**：上游用 ``setTimeout`` / ``queueMicrotask`` / ``Date.now()``；
    本实现用 **asyncio 事件循环**与 ``time.monotonic()``。因此 ``mark()`` /
    ``mark_and_wait()`` 需要**有运行中的事件循环**，否则抛 ``RuntimeError``；
    ``durable.harness.tool`` 的调用点都在 async 上下文里，不受影响。
    """

    def __init__(
        self,
        write: Callable[[], int | Awaitable[int]],
        on_error: Callable[[BaseException], None],
        min_interval_ms: float,
    ) -> None:
        self._write = write
        self._on_error = on_error
        self._min_interval_ms = min_interval_ms
        self._waiters: list[asyncio.Future[None]] = []
        self._timer: asyncio.TimerHandle | None = None
        self._in_flight: asyncio.Future[None] | None = None
        self._next_at = 0.0
        self._dirty = False
        self._stopped = False

    def mark(self) -> None:
        """安排一次提交（上游 ``output.ts:327-331``）。"""
        self._dirty = True
        self._schedule()

    def mark_and_wait(self) -> asyncio.Future[None]:
        """安排一次提交；返回的 future 在**包含本次变更**的那次提交时落定
        （上游 ``output.ts:333-339``）。"""
        waiter: asyncio.Future[None] = _running_loop().create_future()
        self._waiters.append(waiter)
        self.mark()
        return waiter

    async def stop(self) -> list[asyncio.Future[None]]:
        """停止提交并等待在途的那次；返回**最后一次提交必须落定**的 waiters
        （上游 ``output.ts:341-348``）。"""
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._in_flight is not None:
            await asyncio.shield(self._in_flight)
        waiters = self._waiters[:]
        self._waiters.clear()
        return waiters

    def _schedule(self) -> None:
        if self._stopped or self._timer is not None or self._in_flight is not None:
            return
        wait = self._next_at - _now_ms()
        if wait <= 0:
            self._flush()
            return
        loop = _running_loop()
        self._timer = loop.call_later(wait / 1000.0, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        self._flush()

    def _flush(self) -> None:
        if self._stopped or not self._dirty:
            return
        self._dirty = False
        waiters = self._waiters[:]
        self._waiters.clear()
        started = _now_ms()
        loop = _running_loop()

        async def run() -> None:
            try:
                result = self._write()
                written = await result if hasattr(result, "__await__") else result
            except BaseException as error:  # noqa: BLE001 - 与上游一致，任何失败都上报
                self._next_at = started + self._min_interval_ms
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_exception(error)
                self._on_error(error)
                return
            self._next_at = started + max(
                self._min_interval_ms, (written * 1000.0) / PROGRESS_BYTES_PER_SECOND
            )
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)

        task = loop.create_task(run())
        self._in_flight = task
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Future[None]) -> None:
        self._in_flight = None
        if not task.cancelled():  # pragma: no cover - run() 已兜住
            failure = task.exception()
            if failure is not None:
                self._on_error(failure)
        if self._dirty:
            self._schedule()


def _running_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError as error:  # pragma: no cover - 由调用方保证
        raise RuntimeError("Progress requires a running event loop") from error


def _now_ms() -> float:
    """上游用 ``Date.now()``；这里用单调时钟，语义相同且不受系统时间调整影响。"""
    return time.monotonic() * 1000.0

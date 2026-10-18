"""``pi_durable.env.decode`` / ``line_scan`` 的逐字节等价性测试。"""

from __future__ import annotations

import random

import pytest

from pi_durable.env import LineScan, LineScanner, StreamDecoder, range_decoder, starts_with_bom

#: 上游 env-conformance 用例里的样例字节。
SAMPLE = bytes(
    [
        0xEF,
        0xBB,
        0xBF,
        0x61,
        0x0A,
        0xE2,
        0x82,
        0x0A,
        0x0A,
        0xEF,
        0xBB,
        0xBF,
        0x62,
        0x0A,
        0xC3,
        0xA9,
    ]
)


def _whole_file_text(data: bytes) -> str:
    """`new TextDecoder().decode(data)`：丢掉开头的 BOM。"""
    text = data.decode("utf-8", "replace")
    return text[1:] if text.startswith("\ufeff") else text


def _decode_range(data: bytes, start: int, end: int) -> str:
    """`new TextDecoder("utf-8", {ignoreBOM: start > 0})`。"""
    text = data[start:end].decode("utf-8", "replace")
    return text[1:] if start == 0 and text.startswith("\ufeff") else text


def test_stream_decoder_matches_decoding_everything_at_once() -> None:
    for size in range(1, 6):
        decoder = StreamDecoder()
        pieces = [
            decoder.decode(SAMPLE[index : index + size]) for index in range(0, len(SAMPLE), size)
        ]
        pieces.append(decoder.decode())
        assert "".join(pieces) == _whole_file_text(SAMPLE)


def test_stream_decoder_keeps_a_later_mark() -> None:
    decoder = StreamDecoder()
    assert decoder.decode(b"\xef\xbb\xbf") == ""
    assert decoder.decode(b"ab\xef\xbb\xbfc") == "ab\ufeffc"
    assert decoder.decode() == ""


def test_stream_decoder_replaces_invalid_sequences_like_whatwg() -> None:
    decoder = StreamDecoder()
    assert decoder.decode(b"\xe2\x82") == ""
    assert decoder.decode(b"\n") == "\ufffd\n"
    assert decoder.decode() == ""


def test_range_decoder_keeps_a_leading_mark() -> None:
    decoder = range_decoder()
    assert decoder.decode(b"\xef\xbb\xbfa") == "\ufeffa"
    assert decoder.decode() == ""


def test_starts_with_bom() -> None:
    assert starts_with_bom(b"\xef\xbb\xbf")
    assert starts_with_bom(b"\xef\xbb\xbfrest")
    assert not starts_with_bom(b"\xef\xbb")
    assert not starts_with_bom(b"")


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 16, 64])
def test_line_scanner_matches_the_conformance_expectations(chunk_size: int) -> None:
    lines = _whole_file_text(SAMPLE).split("\n")
    for start_line, end_line in [(0, None), (0, 1), (1, 3), (2, 3), (3, None), (4, 9), (9, None)]:
        scan = LineScanner(start_line, end_line)
        for index in range(0, len(SAMPLE), chunk_size):
            scan.push(SAMPLE[index : index + chunk_size])
        result = scan.finish()
        assert result.newlines == len(lines) - 1
        if start_line >= len(lines):
            assert result == LineScan(
                len(lines) - 1, len(SAMPLE), len(SAMPLE), len(SAMPLE), len(SAMPLE), 0, 0
            )
            continue
        selected = lines[start_line:end_line]
        joined = "\n".join(selected)
        assert _decode_range(SAMPLE, result.start, result.end) == joined
        assert result.selected_bytes == len(joined.encode())
        assert _decode_range(SAMPLE, result.start, result.first_line_end) == lines[start_line]
        assert result.first_line_bytes == len(lines[start_line].encode())


def test_line_scanner_is_chunking_invariant_over_random_input() -> None:
    rng = random.Random(20261018)
    for _case in range(200):
        length = rng.randrange(0, 60)
        data = bytes(
            rng.choice([0x0A, 0x41, 0x42, 0xEF, 0xBB, 0xBF, 0xE2, 0x82, 0xC3, 0xA9])
            for _ in range(length)
        )
        text = _whole_file_text(data)
        lines = text.split("\n")
        start_line = rng.randrange(0, 4)
        end_line = rng.choice([None, start_line + 1, start_line + 2])
        reference = LineScanner(start_line, end_line)
        reference.push(data)
        expected = reference.finish()

        scanner = LineScanner(start_line, end_line)
        position = 0
        while position < len(data):
            step = rng.randrange(1, 5)
            scanner.push(data[position : position + step])
            position += step
        assert scanner.finish() == expected
        if start_line < len(lines):
            assert _decode_range(data, expected.start, expected.end) == "\n".join(
                lines[start_line:end_line]
            )


def test_line_scanner_rejects_invalid_ranges() -> None:
    for start_line, end_line in [(-1, None), (2, 2), (3, 1), (1.5, None)]:
        with pytest.raises(ValueError, match="Invalid line range"):
            LineScanner(start_line, end_line)  # type: ignore[arg-type]

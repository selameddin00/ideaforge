import json
import sys
import time
import uuid
from pathlib import Path

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parse  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

FORMAT_FIXTURES = {
    "plain": "transcript_raw.txt",
    "claude_export": "transcript_claude_export.txt",
    "timestamped": "transcript_timestamped.txt",
}


@pytest.mark.parametrize("fmt,filename", FORMAT_FIXTURES.items())
def test_fixture_parses_and_validates(fmt, filename):
    text = (FIXTURES / filename).read_text(encoding="utf-8")
    assert parse.detect_format(text) == fmt

    result = parse.parse_file(FIXTURES / filename)
    jsonschema.validate(result, parse.TRANSCRIPT_SCHEMA)
    uuid.UUID(result["session_id"])
    assert result["message_count"] == len(result["transcript"]) > 0
    assert [m["id"] for m in result["transcript"]] == list(range(1, result["message_count"] + 1))
    for msg in result["transcript"]:
        assert msg["role"] in ("user", "assistant")
        assert msg["content"].strip()
        assert msg["word_count"] == len(msg["content"].split())


def test_raw_matches_expected_output():
    expected = json.loads((FIXTURES / "transcript_expected.json").read_text(encoding="utf-8"))
    result = parse.parse_file(FIXTURES / "transcript_raw.txt")
    assert result["message_count"] == expected["message_count"]
    assert result["transcript"] == expected["transcript"]


def test_claude_export_keeps_turkish_and_multiline():
    result = parse.parse_file(FIXTURES / "transcript_claude_export.txt")
    msgs = result["transcript"]
    assert msgs[0]["content"].startswith("Bir fikir olgunlaştığında")
    assert "1. Hızlı regex tetikleyicileri\n2. Yerel" in msgs[3]["content"]
    assert all(m["timestamp"] is None for m in msgs)


def test_timestamped_extracts_iso_timestamps():
    msgs = parse.parse_file(FIXTURES / "transcript_timestamped.txt")["transcript"]
    assert msgs[0]["timestamp"] == "2026-09-27T14:00:00"
    assert msgs[2]["timestamp"] == "2026-09-27T14:02:30"
    # Continuation line without a header belongs to the previous message.
    assert msgs[1]["content"].endswith("arguments stop being new.")


def test_system_and_empty_messages_dropped():
    text = "System: be nice\nUser: hi\nAssistant:   \nUser:\nAssistant: hello there\n"
    msgs = parse.parse_text(text)["transcript"]
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "hi"),
        ("assistant", "hello there"),
    ]


def test_encoding_cleanup():
    # cp1254 bytes (not valid UTF-8), CRLF newlines, BOM-less.
    cp1254 = "User: Merhaba dünya, şöyle\r\nAssistant: Tamam\r\n".encode("cp1254")
    msgs = parse.parse_text(parse.decode_bytes(cp1254))["transcript"]
    assert msgs[0]["content"] == "Merhaba dünya, şöyle"

    # UTF-8 BOM + zero-width space + control char + mojibake ("ş" read as cp1252).
    utf8 = "﻿User: a​b\x07c\nAssistant: ÅŸimdi\n".encode("utf-8")
    msgs = parse.parse_text(parse.decode_bytes(utf8))["transcript"]
    assert msgs[0]["content"] == "abc"
    assert msgs[1]["content"] == "şimdi"


def test_no_messages_raises():
    with pytest.raises(parse.ParseError):
        parse.parse_text("just some text\nwith no speakers\n")


def test_100_messages_under_one_second():
    lines = []
    for i in range(50):
        lines.append(f"User: Question number {i} about the idea and its feasibility?")
        lines.append(f"Assistant: Answer number {i}, with some detail.\nAnd a second line.")
    text = "\n".join(lines)

    start = time.perf_counter()
    result = parse.parse_text(text)
    elapsed = time.perf_counter() - start

    assert result["message_count"] == 100
    assert elapsed < 1.0, f"parsing 100 messages took {elapsed:.3f}s"


def test_cli_writes_output(tmp_path):
    out = tmp_path / "nested" / "transcript.json"
    assert parse.main([str(FIXTURES / "transcript_raw.txt"), "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    for key in ("session_id", "parsed_at", "message_count", "transcript"):
        assert key in data
    for msg in data["transcript"]:
        for key in ("id", "role", "content", "word_count"):
            assert key in msg


def test_cli_fails_on_unparseable_input(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("nothing to see here", encoding="utf-8")
    assert parse.main([str(bad), "--out", str(tmp_path / "out.json")]) == 1

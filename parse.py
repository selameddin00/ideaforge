"""Parser: raw conversation text -> structured transcript JSON.

Supported input formats (auto-detected):
  plain          "User: ...\\nAssistant: ..."
  claude_export  "[Human]\\n...\\n[Assistant]\\n..."
  timestamped    "2026-09-27 14:00 [user] ..."

Usage:
  python parse.py input.txt --out transcript.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

logger = logging.getLogger(__name__)

# --- Role handling -----------------------------------------------------------

ROLE_MAP = {
    "user": "user",
    "human": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "claude": "assistant",
    "system": "system",
}
DROPPED_ROLES = {"system"}

_ROLE_ALT = "|".join(ROLE_MAP)

# --- Format header patterns --------------------------------------------------

PLAIN_RE = re.compile(rf"^\s*({_ROLE_ALT})\s*:\s?(.*)$", re.IGNORECASE)
CLAUDE_EXPORT_RE = re.compile(rf"^\s*\[({_ROLE_ALT})\]\s*$", re.IGNORECASE)
TIMESTAMPED_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s+\[(\w+)\]\s?(.*)$"
)

# Tie-break order when two formats match the same number of headers:
# more specific formats win.
FORMAT_PRIORITY = ("timestamped", "claude_export", "plain")

# --- Cleaning patterns -------------------------------------------------------

# Zero-width chars, BOM, replacement char.
INVISIBLE_RE = re.compile("[​‌‍⁠﻿�]")
# C0/C1 control chars except tab and newline.
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Typical UTF-8-read-as-cp1252 artifacts (e.g. "ÅŸ" for "ş", "â€™" for "’").
MOJIBAKE_RE = re.compile("[ÃÄÅÂâ][\u0080-¿Œ-ƒˆ-˜–-›€™]")

# --- Output schema -----------------------------------------------------------

TRANSCRIPT_SCHEMA = {
    "type": "object",
    "required": ["session_id", "parsed_at", "message_count", "transcript"],
    "properties": {
        "session_id": {"type": "string", "minLength": 1},
        "parsed_at": {"type": "string", "minLength": 1},
        "message_count": {"type": "integer", "minimum": 0},
        "transcript": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "role", "content", "timestamp", "word_count"],
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "role": {"enum": ["user", "assistant"]},
                    "content": {"type": "string", "minLength": 1},
                    "timestamp": {"type": ["string", "null"]},
                    "word_count": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}


class ParseError(ValueError):
    """Raised when no messages can be extracted from the input."""


# --- Encoding / cleaning -----------------------------------------------------

def decode_bytes(raw: bytes) -> str:
    """Decode bytes as UTF-8 (BOM-aware), falling back to Turkish cp1254."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        logger.warning("Input is not valid UTF-8; falling back to cp1254")
        return raw.decode("cp1254", errors="replace")


def fix_mojibake(line: str) -> str:
    """Repair a line that was UTF-8 but got decoded as cp1252/latin-1."""
    if not MOJIBAKE_RE.search(line):
        return line
    for codec in ("cp1252", "latin-1"):
        try:
            return line.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return line


def clean_text(text: str) -> str:
    """Normalize newlines/unicode and strip invisible, control and mojibake chars."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(fix_mojibake(line) for line in text.split("\n"))
    text = unicodedata.normalize("NFC", text)
    text = INVISIBLE_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    return text


# --- Format detection & parsing ---------------------------------------------

def detect_format(text: str) -> str:
    """Return the format whose header pattern matches the most lines."""
    lines = text.split("\n")
    counts = {
        "timestamped": sum(1 for l in lines if TIMESTAMPED_RE.match(l)),
        "claude_export": sum(1 for l in lines if CLAUDE_EXPORT_RE.match(l)),
        "plain": sum(1 for l in lines if PLAIN_RE.match(l)),
    }
    best = max(FORMAT_PRIORITY, key=lambda f: (counts[f], -FORMAT_PRIORITY.index(f)))
    if counts[best] == 0:
        raise ParseError("No recognizable message headers found in input")
    logger.debug("Format counts: %s -> %s", counts, best)
    return best


def _split_messages(text: str, fmt: str) -> list[dict]:
    """Split text into raw {role, lines, timestamp} blocks for the given format."""
    blocks: list[dict] = []
    current: dict | None = None

    for line in text.split("\n"):
        header = None
        if fmt == "timestamped":
            m = TIMESTAMPED_RE.match(line)
            if m:
                date, clock, role, rest = m.groups()
                if len(clock) == 5:
                    clock += ":00"
                header = (role, [rest], f"{date}T{clock}")
        elif fmt == "claude_export":
            m = CLAUDE_EXPORT_RE.match(line)
            if m:
                header = (m.group(1), [], None)
        else:
            m = PLAIN_RE.match(line)
            if m:
                header = (m.group(1), [m.group(2)], None)

        if header:
            role, first_lines, ts = header
            current = {"role": role.lower(), "lines": first_lines, "timestamp": ts}
            blocks.append(current)
        elif current is not None:
            current["lines"].append(line)
        elif line.strip():
            logger.warning("Dropping text before first message header: %r", line[:60])

    return blocks


def parse_text(text: str) -> dict:
    """Parse raw conversation text into the transcript JSON structure."""
    text = clean_text(text)
    fmt = detect_format(text)

    transcript = []
    skipped_system = skipped_empty = skipped_unknown = 0
    for block in _split_messages(text, fmt):
        role = ROLE_MAP.get(block["role"])
        if role is None:
            skipped_unknown += 1
            continue
        if role in DROPPED_ROLES:
            skipped_system += 1
            continue
        content = "\n".join(block["lines"]).strip()
        if not content:
            skipped_empty += 1
            continue
        transcript.append({
            "id": len(transcript) + 1,
            "role": role,
            "content": content,
            "timestamp": block["timestamp"],
            "word_count": len(content.split()),
        })

    if not transcript:
        raise ParseError(f"Detected format '{fmt}' but no usable messages were found")

    logger.info(
        "Parsed %d messages (format=%s, skipped: system=%d empty=%d unknown_role=%d)",
        len(transcript), fmt, skipped_system, skipped_empty, skipped_unknown,
    )

    result = {
        "session_id": str(uuid.uuid4()),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "message_count": len(transcript),
        "transcript": transcript,
    }
    jsonschema.validate(result, TRANSCRIPT_SCHEMA)
    return result


def parse_file(path: str | Path) -> dict:
    """Read a transcript file (any supported format/encoding) and parse it."""
    return parse_text(decode_bytes(Path(path).read_bytes()))


def run_parser(input_path: str, output_path: str) -> dict:
    """Parse input_path and write the transcript JSON to output_path."""
    result = parse_file(input_path)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", out)
    return result


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a conversation transcript into JSON.")
    parser.add_argument("input", help="Path to the raw transcript text file")
    parser.add_argument("--out", help="Output JSON path (default: stdout)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        result = parse_file(args.input)
    except (OSError, ParseError, jsonschema.ValidationError) as exc:
        logger.error("Failed to parse %s: %s", args.input, exc)
        return 1

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
        logger.info("Wrote %s", out_path)
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

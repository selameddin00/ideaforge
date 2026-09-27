"""Dikizci: maturity check on a parsed transcript.

Reads transcript.json, adds a `maturity` field, and writes it back (merge,
never overwrite other fields).

  1. Hard trigger — regex scan over every message; on match score = 1.0,
     no LLM call.
  2. Soft score   — local Ollama (qwen2.5:7b) judges 4 boolean signals on the
     last messages; score = true_count / 4.
  3. Manual       — --manual forces score = 1.0.

Usage:
  python observer.py --input outputs/2026-09-27_demo/transcript.json
  python observer.py --input transcript.json --output result.json
  python observer.py --input transcript.json --manual
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
)

load_dotenv()

logger = logging.getLogger("ideaforge.observer")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# --- Configuration -----------------------------------------------------------

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT_SEC = 60

SOFT_SCORE_WINDOW = 20          # last N messages sent to the LLM
MAX_MESSAGE_CHARS = 400         # per-message truncation for the prompt
MAX_TRANSCRIPT_CHARS = 5000     # ~1250 tokens; keeps full prompt < 2000 tokens
MAX_LLM_ATTEMPTS = 3            # 1 call + 2 retries

SIGNAL_KEYS = (
    "lexical_saturation",
    "question_type_shift",
    "feasibility_query",
    "summary_confirmation",
)

HARD_TRIGGERS = [
    r"\b(bunu gerçekten yapalım|let's actually build|şimdi yapalım|implement this now)\b",
    r"\b(nasıl implement ederiz|how (do|would) (we|I) implement)\b",
    r"\b(production'a hazır|production.?ready|mvp (için|for))\b",
    r"\b(hangi (teknoloji|stack|dil|framework)|what (tech|stack|language|framework))\b",
    r"\b(repo (kur|oluştur|aç)|create (a )?(repo|repository)|github'a koy)\b",
]
HARD_TRIGGER_PATTERNS = [re.compile(p, re.IGNORECASE) for p in HARD_TRIGGERS]

SYSTEM_PROMPT = """You are a classifier that judges whether an idea discussed in a conversation has matured.
Read the conversation and evaluate exactly these 4 boolean signals:

1. lexical_saturation: the same key concepts/terms keep repeating across messages.
2. question_type_shift: questions shift from "what/why" (exploring) to "how" (implementing).
3. feasibility_query: someone asks about technical feasibility, cost, or time/effort.
4. summary_confirmation: the user summarizes the idea and asks for confirmation.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{"lexical_saturation": true, "question_type_shift": false, "feasibility_query": true, "summary_confirmation": false}"""

MATURITY_SCHEMA = {
    "type": "object",
    "required": ["score", "triggered_by", "signals"],
    "properties": {
        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "triggered_by": {"enum": ["hard_trigger", "soft_score", "manual"]},
        "signals": {
            "type": "object",
            "required": list(SIGNAL_KEYS),
            "properties": {k: {"type": "boolean"} for k in SIGNAL_KEYS},
            "additionalProperties": False,
        },
    },
}


class InvalidLLMResponse(ValueError):
    """Ollama answered, but not with the expected 4-signal JSON."""


# --- I/O ---------------------------------------------------------------------

def load_transcript(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        logger.error("Transcript not found: %s", path)
        raise FileNotFoundError(f"Transcript not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %s", path, exc)
        raise
    if not isinstance(data.get("transcript"), list):
        logger.error("%s has no 'transcript' array", path)
        raise ValueError(f"{path} has no 'transcript' array")
    return data


def write_output(data: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    logger.info("Output written: %s", path)


# --- Hard trigger ------------------------------------------------------------

def find_hard_trigger(transcript: list[dict]) -> str | None:
    """Return the first matching trigger pattern, or None. Scans all roles."""
    for msg in transcript:
        content = msg.get("content", "")
        for pattern in HARD_TRIGGER_PATTERNS:
            if pattern.search(content):
                return pattern.pattern
    return None


# --- Soft score --------------------------------------------------------------

def build_prompt(transcript: list[dict]) -> str:
    """Format the last messages as 'role: content', trimmed to the char budget."""
    lines = []
    for msg in transcript[-SOFT_SCORE_WINDOW:]:
        content = " ".join(msg.get("content", "").split())
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + "..."
        lines.append(f"{msg.get('role', 'unknown')}: {content}")

    # Drop oldest messages until within budget (always keep the latest one).
    while len(lines) > 1 and sum(len(l) + 1 for l in lines) > MAX_TRANSCRIPT_CHARS:
        lines.pop(0)

    return "Conversation:\n" + "\n".join(lines) + "\n\nReturn the JSON now."


def parse_signals(raw: str) -> dict[str, bool]:
    """Parse and validate the LLM's JSON answer into the 4 boolean signals."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidLLMResponse(f"Ollama did not return JSON: {raw[:200]!r}") from exc
    if not isinstance(obj, dict):
        raise InvalidLLMResponse(f"Expected a JSON object, got: {raw[:200]!r}")
    missing = [k for k in SIGNAL_KEYS if not isinstance(obj.get(k), bool)]
    if missing:
        raise InvalidLLMResponse(f"Missing/non-boolean signals {missing} in: {raw[:200]!r}")
    return {k: obj[k] for k in SIGNAL_KEYS}


@retry(
    retry=retry_if_exception_type(InvalidLLMResponse),
    stop=stop_after_attempt(MAX_LLM_ATTEMPTS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def query_ollama_signals(prompt: str) -> dict[str, bool]:
    """One Ollama call; retried (tenacity) only on malformed answers."""
    logger.debug("Ollama prompt: %s", prompt[:200])
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "system": SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=OLLAMA_TIMEOUT_SEC,
        )
        if resp.status_code == 404:
            logger.error("Ollama model '%s' not found - run `ollama pull %s`", OLLAMA_MODEL, OLLAMA_MODEL)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        logger.error("Cannot reach Ollama at %s - is `ollama serve` running?", OLLAMA_URL)
        raise
    except requests.exceptions.RequestException as exc:
        logger.error("Ollama request failed: %s", exc)
        raise

    try:
        raw = resp.json()["response"]
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidLLMResponse(f"Unexpected Ollama payload: {resp.text[:200]!r}") from exc
    return parse_signals(raw)


# --- Orchestration -----------------------------------------------------------

def evaluate_maturity(transcript: list[dict], manual: bool = False) -> dict:
    if manual:
        logger.info("Manual trigger: score forced to 1.00")
        maturity = {
            "score": 1.0,
            "triggered_by": "manual",
            "signals": {k: True for k in SIGNAL_KEYS},
        }
    elif (pattern := find_hard_trigger(transcript)) is not None:
        logger.info("Hard trigger detected: '%s'", pattern)
        maturity = {
            "score": 1.0,
            "triggered_by": "hard_trigger",
            "signals": {k: True for k in SIGNAL_KEYS},
        }
    else:
        try:
            signals = query_ollama_signals(build_prompt(transcript))
        except InvalidLLMResponse as exc:
            logger.error("Ollama gave no valid signals after %d attempts: %s", MAX_LLM_ATTEMPTS, exc)
            raise
        score = sum(signals.values()) / 4.0
        logger.info("Soft score: %.2f", score)
        maturity = {"score": score, "triggered_by": "soft_score", "signals": signals}

    logger.info("Signals: %s", maturity["signals"])
    return maturity


def run_observer(data: dict, manual: bool = False) -> dict:
    """Merge `maturity` into an in-memory transcript document and return it."""
    if not isinstance(data.get("transcript"), list):
        raise ValueError("data has no 'transcript' array")
    data["maturity"] = evaluate_maturity(data["transcript"], manual=manual)
    return data


def run(input_path: str | Path, output_path: str | Path | None = None, manual: bool = False) -> dict:
    """Load transcript.json, merge in `maturity`, write to output (default: input)."""
    data = run_observer(load_transcript(input_path), manual=manual)
    write_output(data, output_path or input_path)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dikizci: score idea maturity of a transcript.")
    parser.add_argument("--input", required=True, help="Path to transcript.json")
    parser.add_argument("--output", help="Output JSON path (default: same as --input)")
    parser.add_argument("--manual", action="store_true", help="Force triggered_by=manual, score=1.0")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT)

    try:
        run(args.input, args.output, manual=args.manual)
    except Exception as exc:
        logger.error("Observer failed: %s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

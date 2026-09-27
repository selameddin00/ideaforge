"""Baron: synthesize the debate into PRD -> CLAUDE.md -> prompt chain.

Three sequential Anthropic API calls, never merged, never parallel. After every
call the enriched JSON is written to disk so a failure loses at most one step.

Outputs (in the output transcript's directory):
  transcript.json  synthesis filled, meta.total_tokens / total_cost_usd updated
  CLAUDE.md        synthesis.claude_md
  prompts.json     synthesis.prompt_chain

Usage:
  python baron.py tests/fixtures/post_debate.json
  python baron.py outputs/2026-09-27_slug/transcript.json          # in place
  python baron.py input.json --output outputs/2026-09-27_demo/transcript.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import anthropic
import jsonschema
from dotenv import load_dotenv
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from debate import atomic_write_json

load_dotenv()

logger = logging.getLogger("ideaforge.baron")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# --- Configuration -----------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_TOKENS_PER_CALL = 8192
MAX_ATTEMPTS = 3

INPUT_COST_PER_MTOK = 3.00        # Sonnet pricing, USD
OUTPUT_COST_PER_MTOK = 15.00
COST_WARNING_USD = 0.50

OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PRD_PROMPT_PATH = PROMPTS_DIR / "baron_prd.txt"
CLAUDE_MD_PROMPT_PATH = PROMPTS_DIR / "baron_claude_md.txt"
PROMPTS_PROMPT_PATH = PROMPTS_DIR / "baron_prompts.txt"

CLAUDE_MD_REQUIRED_SECTIONS = ("What It Does", "Architecture", "Key Design Decisions", "What NOT To Do")

# field -> required Python type
PROMPT_FIELDS = {
    "order": int,
    "trigger": bool,
    "input": str,
    "task": str,
    "success_criteria": str,
    "on_error": str,
}

_STR_LIST = {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
PRD_SCHEMA = {
    "type": "object",
    "required": [
        "problem_statement", "target_users", "core_features",
        "constraints", "success_metrics", "unknowns",
    ],
    "properties": {
        "problem_statement": {"type": "string", "minLength": 1},
        "target_users": _STR_LIST,
        "core_features": _STR_LIST,
        "constraints": _STR_LIST,
        "success_metrics": _STR_LIST,
        "unknowns": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
}

JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n?```", re.DOTALL | re.IGNORECASE)
OUTER_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.DOTALL)


class BaronParseError(ValueError):
    """Model output could not be parsed/validated. Triggers a retry."""


# --- Parsing & validation ----------------------------------------------------

def parse_json_response(text: str):
    """1) ```json fenced block, 2) raw JSON, else BaronParseError."""
    match = JSON_FENCE_RE.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            logger.debug("Fenced JSON block did not parse: %s", exc)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise BaronParseError(f"No parseable JSON in response: {text[:200]!r}") from exc


def validate_prompt_chain(chain: list[dict]) -> None:
    """Raise ValueError on any missing/mistyped field. Never passes silently."""
    if not isinstance(chain, list) or not chain:
        raise ValueError("prompt_chain must be a non-empty list")
    for i, item in enumerate(chain):
        if not isinstance(item, dict):
            raise ValueError(f"prompt_chain[{i}] is not an object")
        for field, expected in PROMPT_FIELDS.items():
            if field not in item:
                raise ValueError(f"prompt_chain[{i}] missing field '{field}'")
            value = item[field]
            # bool is a subclass of int; don't accept True as an order.
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ValueError(
                    f"prompt_chain[{i}].{field} must be {expected.__name__}, got {type(value).__name__}"
                )
            if expected is str and not value.strip():
                raise ValueError(f"prompt_chain[{i}].{field} is empty")
    orders = [item["order"] for item in chain]
    if orders != list(range(1, len(chain) + 1)):
        raise ValueError(f"prompt_chain orders must be 1..{len(chain)} in sequence, got {orders}")


def _strip_outer_fence(text: str) -> str:
    match = OUTER_FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


def _validate_claude_md(text: str) -> None:
    missing = [
        s for s in CLAUDE_MD_REQUIRED_SECTIONS
        if not re.search(rf"^##\s+{re.escape(s)}\s*$", text, re.MULTILINE | re.IGNORECASE)
    ]
    if missing:
        raise BaronParseError(f"CLAUDE.md missing required sections: {missing}")


# --- API ---------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (BaronParseError, anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


baron_retry = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(MAX_ATTEMPTS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


def _create(client, system_path: Path, user_content: str) -> tuple[str, int, int]:
    """One Messages API call. Returns (text, input_tokens, output_tokens)."""
    response = client.messages.create(
        model=os.getenv("MODEL", DEFAULT_MODEL),
        max_tokens=MAX_TOKENS_PER_CALL,
        system=system_path.read_text(encoding="utf-8").strip(),
        messages=[{"role": "user", "content": user_content}],
    )
    usage = response.usage
    logger.info("%s: %d in / %d out tokens", system_path.stem, usage.input_tokens, usage.output_tokens)
    if response.stop_reason == "max_tokens":
        raise BaronParseError(f"{system_path.stem}: output truncated at max_tokens")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise BaronParseError(f"{system_path.stem}: empty response (stop_reason={response.stop_reason})")
    return text, usage.input_tokens, usage.output_tokens


def _format_debate_input(data: dict) -> str:
    transcript = "\n\n".join(
        f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in data.get("transcript") or []
    )
    rounds = (data.get("debate") or {}).get("rounds") or []
    if not rounds:
        raise ValueError("Input has no debate.rounds — run debate.py first")
    debate_text = "\n\n".join(
        f"Round {r['round']}\nAdvocate:\n{r['advocate']['content']}\n\nDevil's Advocate:\n{r['devil']['content']}"
        for r in rounds
    )
    return f"<conversation>\n{transcript}\n</conversation>\n\n<debate>\n{debate_text}\n</debate>"


@baron_retry
def _call_prd(client, data: dict) -> tuple[dict, int, int]:
    text, tokens_in, tokens_out = _create(client, PRD_PROMPT_PATH, _format_debate_input(data))
    prd = parse_json_response(text)
    try:
        jsonschema.validate(prd, PRD_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise BaronParseError(f"PRD does not match schema: {exc.message}") from exc
    return prd, tokens_in, tokens_out


@baron_retry
def _call_claude_md(client, data: dict) -> tuple[str, int, int]:
    prd = data["synthesis"]["prd"]
    user = f"<prd>\n{json.dumps(prd, ensure_ascii=False, indent=2)}\n</prd>"
    text, tokens_in, tokens_out = _create(client, CLAUDE_MD_PROMPT_PATH, user)
    claude_md = _strip_outer_fence(text)
    _validate_claude_md(claude_md)
    return claude_md, tokens_in, tokens_out


@baron_retry
def _call_prompts(client, data: dict) -> tuple[list[dict], int, int]:
    synthesis = data["synthesis"]
    user = (
        f"<prd>\n{json.dumps(synthesis['prd'], ensure_ascii=False, indent=2)}\n</prd>\n\n"
        f"<claude_md>\n{synthesis['claude_md']}\n</claude_md>"
    )
    text, tokens_in, tokens_out = _create(client, PROMPTS_PROMPT_PATH, user)
    chain = parse_json_response(text)
    try:
        validate_prompt_chain(chain)
    except ValueError as exc:
        raise BaronParseError(f"Invalid prompt chain: {exc}") from exc
    return chain, tokens_in, tokens_out


# --- Bookkeeping -------------------------------------------------------------

def _add_usage(data: dict, tokens_in: int, tokens_out: int) -> None:
    """Accumulate tokens and recompute pipeline cost; warn (never stop) over budget."""
    meta = data.setdefault("meta", {})
    meta["total_tokens"] = meta.get("total_tokens", 0) + tokens_in + tokens_out
    meta["total_input_tokens"] = meta.get("total_input_tokens", 0) + tokens_in
    meta["total_output_tokens"] = meta.get("total_output_tokens", 0) + tokens_out
    meta["total_cost_usd"] = round(
        meta["total_input_tokens"] * INPUT_COST_PER_MTOK / 1_000_000
        + meta["total_output_tokens"] * OUTPUT_COST_PER_MTOK / 1_000_000,
        6,
    )
    if meta["total_cost_usd"] > COST_WARNING_USD:
        logger.warning(
            "Pipeline cost $%.4f exceeds $%.2f budget — continuing",
            meta["total_cost_usd"], COST_WARNING_USD,
        )


def _slugify(text: str, max_words: int = 5) -> str:
    text = text.replace("ı", "i").replace("İ", "I")
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    return "-".join(words[:max_words]) or "session"


def default_output_path(input_path: Path, data: dict) -> Path:
    """In place if the input already lives under outputs/, else outputs/YYYY-MM-DD_slug/."""
    try:
        input_path.resolve().relative_to(OUTPUTS_DIR)
        return input_path
    except ValueError:
        pass
    first_user = next((m["content"] for m in data.get("transcript", []) if m.get("role") == "user"), "")
    return OUTPUTS_DIR / f"{date.today():%Y-%m-%d}_{_slugify(first_user)}" / "transcript.json"


# --- Pipeline ----------------------------------------------------------------

def run_baron(source: dict | str, output_path: str | None = None, client=None) -> dict:
    """PRD -> CLAUDE.md -> prompt chain. Returns the enriched document.

    `source` is a transcript dict (enriched in place) or a path to one. When
    `output_path` is given, transcript.json is written there after every call
    and CLAUDE.md / prompts.json next to it. Path input requires output_path.
    """
    if isinstance(source, dict):
        data = source
    else:
        input_path = Path(source)
        if not input_path.is_file():
            raise FileNotFoundError(f"Transcript not found: {input_path}")
        if output_path is None:
            raise ValueError("output_path is required when source is a path")
        data = json.loads(input_path.read_text(encoding="utf-8"))

    out = Path(output_path) if output_path is not None else None
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)

    def _save(extra_name: str | None = None, extra_text: str | None = None) -> None:
        if out is None:
            return
        atomic_write_json(data, out)
        if extra_name:
            (out.parent / extra_name).write_text(extra_text + "\n", encoding="utf-8")

    # tenacity owns retries; disable the SDK's built-in retry layer.
    client = client or anthropic.Anthropic(max_retries=0)

    synthesis = data.setdefault("synthesis", {})
    meta = data.setdefault("meta", {})
    if meta.get("total_tokens") and "total_input_tokens" not in meta:
        logger.warning(
            "meta has total_tokens but no input/output split; earlier stages are "
            "excluded from total_cost_usd (re-run debate.py to record the split)"
        )

    prd, tokens_in, tokens_out = _call_prd(client, data)
    synthesis["prd"] = prd
    data["unknowns"] = list(prd.get("unknowns", []))
    _add_usage(data, tokens_in, tokens_out)
    _save()
    logger.info("1/3 PRD done (%d unknowns)", len(data["unknowns"]))

    claude_md, tokens_in, tokens_out = _call_claude_md(client, data)
    synthesis["claude_md"] = claude_md
    _add_usage(data, tokens_in, tokens_out)
    _save("CLAUDE.md", claude_md)
    logger.info("2/3 CLAUDE.md done")

    chain, tokens_in, tokens_out = _call_prompts(client, data)
    validate_prompt_chain(chain)
    synthesis["prompt_chain"] = chain
    _add_usage(data, tokens_in, tokens_out)
    _save("prompts.json", json.dumps(chain, ensure_ascii=False, indent=2))
    logger.info("3/3 prompt chain done (%d prompts)", len(chain))

    logger.info(
        "Baron finished: total_tokens=%d, total_cost_usd=$%.4f",
        meta["total_tokens"], meta["total_cost_usd"],
    )
    return data


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Baron: synthesize PRD, CLAUDE.md and prompt chain.")
    parser.add_argument("input", help="Path to post-debate transcript.json")
    parser.add_argument(
        "--output",
        help="Output transcript.json path (default: in place under outputs/, else outputs/YYYY-MM-DD_slug/)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stderr,
    )

    try:
        input_path = Path(args.input)
        output_path = Path(args.output) if args.output else None
        if output_path is None:
            data = json.loads(input_path.read_text(encoding="utf-8"))
            output_path = default_output_path(input_path, data)
        run_baron(str(input_path), str(output_path))
    except Exception as exc:
        logger.error("Baron failed: %s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Debate: Advocate vs Devil, up to 3 sequential rounds via the Anthropic API.

Reads transcript.json, writes the result under the `debate` key and updates
`meta.total_tokens`, then atomically replaces transcript.json.

The debate stops early on argument saturation: from round 2 on, if the Devil's
new response is too similar to its previous one (novelty ratio < 0.15).

Usage:
  python debate.py --transcript outputs/2026-09-27_slug/transcript.json
  TRANSCRIPT_PATH=... python debate.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from tenacity import (
    RetryError,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()

logger = logging.getLogger("ideaforge.debate")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# --- Configuration -----------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_ROUNDS = 3
MAX_TOKENS_PER_TURN = 1024        # ~400 words fits comfortably
SATURATION_THRESHOLD = 0.15
IDEA_MESSAGE_WINDOW = 20          # last N transcript messages when no PRD exists

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ADVOCATE_PROMPT_PATH = PROMPTS_DIR / "advocate.txt"
DEVIL_PROMPT_PATH = PROMPTS_DIR / "devil.txt"

RETRYABLE_ERRORS = (anthropic.APIError, anthropic.RateLimitError, anthropic.APIConnectionError)


# --- Pure helpers ------------------------------------------------------------

def compute_novelty(text_a: str, text_b: str) -> float:
    """1 - Jaccard similarity of lowercase whitespace tokens. 0.0 = identical."""
    tokens_a = set(text_a.lower().split())
    tokens_b = set(text_b.lower().split())
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return 1.0 - len(tokens_a & tokens_b) / len(union)


def build_idea_context(data: dict) -> str:
    """Idea source: synthesis.prd if present, else the last transcript messages."""
    prd = (data.get("synthesis") or {}).get("prd")
    if prd:
        return "Product requirements (PRD):\n" + json.dumps(prd, ensure_ascii=False, indent=2)

    messages = data.get("transcript") or []
    if not messages:
        raise ValueError("transcript.json has neither synthesis.prd nor transcript messages")
    lines = [f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in messages[-IDEA_MESSAGE_WINDOW:]]
    return "Conversation where the idea was developed:\n" + "\n\n".join(lines)


def build_advocate_messages(idea: str, rounds: list[dict]) -> list[dict]:
    """Advocate's own turns are `assistant`; the Devil's replies are `user`."""
    messages = [{
        "role": "user",
        "content": f"{idea}\n\nRound 1: make your strongest case FOR this idea.",
    }]
    for r in rounds:
        messages.append({"role": "assistant", "content": r["advocate"]["content"]})
        messages.append({
            "role": "user",
            "content": (
                f"Devil's Advocate (round {r['round']}):\n{r['devil']['content']}\n\n"
                f"Round {r['round'] + 1}: respond and add a NEW argument."
            ),
        })
    return messages


def build_devil_messages(idea: str, rounds: list[dict], advocate_now: str) -> list[dict]:
    """Devil's own turns are `assistant`; the Advocate's arguments are `user`."""
    advocate_turns = [r["advocate"]["content"] for r in rounds] + [advocate_now]
    messages = []
    for i, adv in enumerate(advocate_turns):
        prefix = f"{idea}\n\n" if i == 0 else ""
        messages.append({
            "role": "user",
            "content": (
                f"{prefix}Advocate (round {i + 1}):\n{adv}\n\n"
                f"Round {i + 1}: attack the Advocate's most recent argument with a NEW objection."
            ),
        })
        if i < len(rounds):
            messages.append({"role": "assistant", "content": rounds[i]["devil"]["content"]})
    return messages


# --- API ---------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(RETRYABLE_ERRORS),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def call_agent(client: anthropic.Anthropic, model: str, system: str, messages: list[dict]):
    """One Messages API call. Returns (text, input_tokens, output_tokens)."""
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS_PER_TURN,
        system=system,
        messages=messages,
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "refusal" or not text:
        raise ValueError(f"Empty or refused response (stop_reason={response.stop_reason})")
    return text, response.usage.input_tokens, response.usage.output_tokens


# --- I/O ---------------------------------------------------------------------

def load_transcript(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Transcript not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(data: dict, path: Path) -> None:
    """Write to <name>.tmp in the same dir, then os.replace() over the target."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --- Debate ------------------------------------------------------------------

def run_debate(transcript_path: Path, client: anthropic.Anthropic | None = None) -> dict:
    """Run the debate, merge it into transcript.json, and return the full document.

    Raises SystemExit(1) if the API is still failing after all retries.
    """
    transcript_path = Path(transcript_path)
    data = load_transcript(transcript_path)
    idea = build_idea_context(data)
    advocate_system = ADVOCATE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    devil_system = DEVIL_PROMPT_PATH.read_text(encoding="utf-8").strip()
    model = os.getenv("MODEL", DEFAULT_MODEL)
    # tenacity owns retries; disable the SDK's built-in retry layer.
    client = client or anthropic.Anthropic(max_retries=0)

    rounds: list[dict] = []
    total_input = total_output = 0
    termination_reason = "max_rounds"

    try:
        for round_no in range(1, MAX_ROUNDS + 1):
            adv_text, adv_in, adv_out = call_agent(
                client, model, advocate_system, build_advocate_messages(idea, rounds)
            )
            dev_text, dev_in, dev_out = call_agent(
                client, model, devil_system, build_devil_messages(idea, rounds, adv_text)
            )
            total_input += adv_in + dev_in
            total_output += adv_out + dev_out

            previous_devil = rounds[-1]["devil"]["content"] if rounds else None
            rounds.append({
                "round": round_no,
                "advocate": {"content": adv_text, "token_count": adv_in + adv_out},
                "devil": {"content": dev_text, "token_count": dev_in + dev_out},
            })
            logger.info(
                "Round %d done (advocate=%d tokens, devil=%d tokens)",
                round_no, adv_in + adv_out, dev_in + dev_out,
            )

            if previous_devil is not None:
                novelty = compute_novelty(dev_text, previous_devil)
                logger.info("Round %d devil novelty ratio: %.3f", round_no, novelty)
                if novelty < SATURATION_THRESHOLD:
                    termination_reason = "argument_saturation"
                    logger.info("Argument saturation reached; stopping after round %d", round_no)
                    break
    except (RetryError, *RETRYABLE_ERRORS) as exc:
        logger.error("Anthropic API failed after retries: %s", exc)
        raise SystemExit(1) from exc

    data["debate"] = {"rounds": rounds, "termination_reason": termination_reason}
    meta = data.setdefault("meta", {})
    meta["total_tokens"] = total_input + total_output
    # Split kept so Baron can price the whole pipeline (input/output rates differ).
    meta["total_input_tokens"] = total_input
    meta["total_output_tokens"] = total_output
    logger.info(
        "Debate finished: %d rounds, %s, tokens in=%d out=%d",
        len(rounds), termination_reason, total_input, total_output,
    )

    atomic_write_json(data, transcript_path)
    logger.info("Output written: %s", transcript_path)
    return data


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Advocate vs Devil debate.")
    parser.add_argument(
        "--transcript",
        default=os.getenv("TRANSCRIPT_PATH"),
        help="Path to transcript.json (default: $TRANSCRIPT_PATH)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stderr,
    )

    if not args.transcript:
        logger.error("No transcript given: use --transcript or set TRANSCRIPT_PATH")
        return 1

    try:
        run_debate(Path(args.transcript))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        logger.error("Debate failed: %s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

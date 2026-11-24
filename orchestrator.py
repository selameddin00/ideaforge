"""Orchestrator: runs parse -> observer -> debate -> baron sequentially.

Every agent is imported and called directly (no subprocess). After each stage
the enriched document is saved to <output_dir>/transcript.json, so a failure
leaves the partial JSON of every completed stage on disk.

Usage:
  python ideaforge.py run input.txt
  python ideaforge.py run input.txt --output-dir outputs/my_run
  python ideaforge.py run input.txt --dry-run          # parse + observer only
  python ideaforge.py run input.txt --no-interactive   # skip unknowns Q&A
  python ideaforge.py run input.txt --manual           # force maturity = 1.0
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path

from baron import INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK, run_baron
from debate import MAX_ROUNDS, atomic_write_json, run_debate
from observer import HARD_TRIGGER_PATTERNS, run_observer
from parse import run_parser

logger = logging.getLogger("ideaforge.orchestrator")

CONSOLE_FORMAT = "[IdeaForge] %(message)s"
FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"
COST_LIMIT_USD = 0.50
MATURITY_THRESHOLD = 0.7
RULE = "━" * 34

TRANSCRIPT_FILE = "transcript.json"
DEBATE_FILE = "debate.json"
PRD_FILE = "prd.json"
CLAUDE_MD_FILE = "CLAUDE.md"
PROMPTS_FILE = "prompts.json"
LOG_FILE = "pipeline.log"


# --- Output folder -----------------------------------------------------------

def slugify(input_path: Path) -> str:
    """File name without extension, lowercase, whitespace -> underscore."""
    slug = re.sub(r"\s+", "_", input_path.stem.strip().lower())
    slug = re.sub(r"[^\w\-]", "", slug)
    return slug or "input"


def resolve_output_dir(input_path: Path, output_dir: str | None) -> Path:
    """--output-dir as given, else outputs/YYYY-MM-DD_slug[_001, _002, ...]."""
    if output_dir:
        path = Path(output_dir)
    else:
        base = OUTPUTS_DIR / f"{date.today():%Y-%m-%d}_{slugify(input_path)}"
        path, n = base, 0
        while path.exists():
            n += 1
            path = base.with_name(f"{base.name}_{n:03d}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_hard_trigger_hit(transcript: list[dict]) -> tuple[int, str] | None:
    """(message id, matched phrase) of the first hard trigger, for the console."""
    for msg in transcript:
        for pattern in HARD_TRIGGER_PATTERNS:
            if match := pattern.search(msg.get("content", "")):
                return msg["id"], match.group(0)
    return None


# --- Logging -----------------------------------------------------------------

class _ConsoleFilter(logging.Filter):
    """Console shows orchestrator progress, plus WARNING+ from everything else."""

    def __init__(self, debug: bool):
        super().__init__()
        self.debug = debug

    def filter(self, record: logging.LogRecord) -> bool:
        return self.debug or record.name == logger.name or record.levelno >= logging.WARNING


class _ConsoleFormatter(logging.Formatter):
    """Tracebacks go to pipeline.log; the console shows them only with --debug."""

    def __init__(self, debug: bool):
        super().__init__(CONSOLE_FORMAT)
        self.debug = debug

    def format(self, record: logging.LogRecord) -> str:
        if self.debug or not record.exc_info:
            return super().format(record)
        saved = record.exc_info, record.exc_text
        record.exc_info = record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = saved


def setup_logging(log_path: Path, debug: bool) -> tuple[list[logging.Handler], int]:
    level = logging.DEBUG if debug else logging.INFO
    if hasattr(sys.stderr, "reconfigure"):
        # Non-UTF-8 consoles/pipes must not crash on ▶ ✓ ━.
        sys.stderr.reconfigure(errors="replace")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_ConsoleFormatter(debug))
    console.addFilter(_ConsoleFilter(debug))

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(level)
    for h in (console, file_handler):
        h.setLevel(level)
        root.addHandler(h)
    return [console, file_handler], previous_level


def teardown_logging(handlers: list[logging.Handler], previous_level: int) -> None:
    root = logging.getLogger()
    for h in handlers:
        root.removeHandler(h)
        h.close()
    root.setLevel(previous_level)


# --- Helpers -----------------------------------------------------------------

def _usage(data: dict) -> tuple[int, float]:
    """(total_tokens, cost_usd) from meta's input/output split."""
    meta = data.get("meta") or {}
    tokens_in = meta.get("total_input_tokens", 0)
    tokens_out = meta.get("total_output_tokens", 0)
    tokens = tokens_in + tokens_out if (tokens_in or tokens_out) else meta.get("total_tokens", 0)
    cost = tokens_in * INPUT_COST_PER_MTOK / 1_000_000 + tokens_out * OUTPUT_COST_PER_MTOK / 1_000_000
    return tokens, cost


def _finalize_meta(data: dict, started: float) -> None:
    tokens, cost = _usage(data)
    meta = data.setdefault("meta", {})
    meta["total_tokens"] = tokens
    meta["total_cost_usd"] = round(cost, 6)
    meta["pipeline_duration_sec"] = round(time.perf_counter() - started, 2)


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _display(path: Path) -> str:
    try:
        return f"{path.resolve().relative_to(Path.cwd().resolve()).as_posix()}/"
    except ValueError:
        return f"{path}/"


def ask_unknowns(data: dict, interactive: bool) -> None:
    """Collect answers for Baron's unknowns into synthesis.unknowns[*].answer."""
    synthesis = data.setdefault("synthesis", {})
    questions = data.get("unknowns") or (synthesis.get("prd") or {}).get("unknowns") or []
    existing = {u["question"]: u for u in synthesis.get("unknowns") or [] if isinstance(u, dict)}
    items = [existing.get(q, {"question": q, "answer": None}) for q in questions]
    synthesis["unknowns"] = items
    if not items:
        return

    n = len(items)
    if not interactive:
        logger.info("Unknowns loop skipped (--no-interactive): %d open questions", n)
        return
    try:
        reply = input(f"⚠️  Baron {n} belirsiz nokta tespit etti. Cevaplamak ister misiniz? [Y/n] ")
    except EOFError:
        reply = "n"
    if reply.strip().lower() not in ("", "y", "yes", "e", "evet"):
        logger.info("Unknowns loop skipped by user: %d open questions", n)
        return

    for i, item in enumerate(items, start=1):
        try:
            answer = input(f"[{i}/{n}] {item['question']} → ").strip()
        except EOFError:
            logger.info("Input closed; stopping unknowns loop at %d/%d", i, n)
            break
        if answer:
            item["answer"] = answer
    answered = sum(1 for item in items if item.get("answer"))
    logger.info("Unknowns answered: %d/%d", answered, n)


# --- Pipeline ----------------------------------------------------------------

def run_pipeline(
    input_path: Path,
    out_dir: Path,
    dry_run: bool = False,
    interactive: bool = True,
    manual: bool = False,
) -> int:
    started = time.perf_counter()
    transcript_path = out_dir / TRANSCRIPT_FILE
    data: dict | None = None
    stage = "parse"

    def save() -> None:
        _finalize_meta(data, started)
        atomic_write_json(data, transcript_path)

    try:
        # 1. parse
        logger.info("▶ %-9s %s → %s", "parse", input_path.name, TRANSCRIPT_FILE)
        t = time.perf_counter()
        data = run_parser(str(input_path), str(transcript_path))
        logger.info("✓ %-9s %d messages | %.1fs", "parse", data["message_count"], time.perf_counter() - t)

        # 2. observer
        stage = "observer"
        logger.info("▶ %-9s %s → maturity check", "observer", TRANSCRIPT_FILE)
        t = time.perf_counter()
        data = run_observer(data, manual=manual)
        save()
        maturity = data["maturity"]
        logger.info(
            "✓ %-9s score=%.2f | trigger=%s | %.1fs",
            "observer", maturity["score"], maturity["triggered_by"], time.perf_counter() - t,
        )
        if maturity["triggered_by"] == "hard_trigger" and (hit := find_hard_trigger_hit(data["transcript"])):
            logger.info("🔥 TRIGGER FIRED  message #%d: \"%s\"", *hit)

        if dry_run:
            logger.info("Dry run: debate and baron skipped")
            return _finish(data, out_dir, started, "Dry run tamamlandı")
        if maturity["score"] < MATURITY_THRESHOLD:
            logger.warning(
                "Fikir henüz olgun değil (score=%.2f < %.2f) — debate/baron atlandı. "
                "Zorlamak için --manual kullanın.",
                maturity["score"], MATURITY_THRESHOLD,
            )
            return _finish(data, out_dir, started, "Pipeline durdu: fikir olgun değil")

        # 3. debate
        stage = "debate"
        logger.info("▶ %-9s %d rounds (Advocate vs Devil)", "debate", MAX_ROUNDS)
        t = time.perf_counter()
        tokens_before, cost_before = _usage(data)
        data = run_debate(data)
        save()
        _write_json(out_dir / DEBATE_FILE, data["debate"])
        tokens_after, cost_after = _usage(data)
        logger.info(
            "✓ %-9s %s tokens | $%.3f | %.1fs",
            "debate", f"{tokens_after - tokens_before:,}", cost_after - cost_before, time.perf_counter() - t,
        )

        # 4. baron
        stage = "baron"
        logger.info("▶ %-9s PRD → CLAUDE.md → Prompts", "baron")
        t = time.perf_counter()
        tokens_before, cost_before = tokens_after, cost_after
        data = run_baron(data, output_path=str(transcript_path))
        synthesis = data["synthesis"]
        _write_json(out_dir / PRD_FILE, synthesis["prd"])
        (out_dir / CLAUDE_MD_FILE).write_text(synthesis["claude_md"] + "\n", encoding="utf-8")
        _write_json(out_dir / PROMPTS_FILE, synthesis["prompt_chain"])
        save()
        tokens_after, cost_after = _usage(data)
        logger.info(
            "✓ %-9s %s tokens | $%.3f | %.1fs",
            "baron", f"{tokens_after - tokens_before:,}", cost_after - cost_before, time.perf_counter() - t,
        )

        # 5. unknowns Q&A
        stage = "unknowns"
        ask_unknowns(data, interactive)
        return _finish(data, out_dir, started, "Pipeline tamamlandı")

    except (Exception, SystemExit, KeyboardInterrupt) as exc:
        # debate.run_debate raises SystemExit(1) when API retries are exhausted.
        detail = f"exit code {exc.code}" if isinstance(exc, SystemExit) else f"{type(exc).__name__}: {exc}"
        logger.error("✗ %-9s %s", stage, detail, exc_info=not isinstance(exc, SystemExit))
        if data is not None:
            save()
            logger.error("Kısmi çıktı kaydedildi: %s", transcript_path)
        return 1


def _finish(data: dict, out_dir: Path, started: float, title: str) -> int:
    _finalize_meta(data, started)
    atomic_write_json(data, out_dir / TRANSCRIPT_FILE)
    meta = data["meta"]
    logger.info(RULE)
    logger.info("✅ %s", title)
    logger.info("   Klasör : %s", _display(out_dir))
    logger.info(
        "   Toplam : %s tokens | $%.3f | %.1fs",
        f"{meta['total_tokens']:,}", meta["total_cost_usd"], meta["pipeline_duration_sec"],
    )
    if meta["total_cost_usd"] > COST_LIMIT_USD:
        logger.warning("⚠️ Maliyet limiti aşıldı: $%.2f", meta["total_cost_usd"])
    return 0


# --- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ideaforge", description="IdeaForge multi-agent pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the full pipeline on a transcript file")
    run.add_argument("input_file", help="Raw conversation transcript (.txt)")
    run.add_argument("--output-dir", help="Output folder (default: outputs/YYYY-MM-DD_<slug>/)")
    run.add_argument("--dry-run", action="store_true", help="Only parse + observer; skip debate and baron")
    run.add_argument("--no-interactive", action="store_true", help="Skip the unknowns Q&A loop")
    run.add_argument("--manual", action="store_true", help="Force maturity (score=1.0, no Ollama call)")
    run.add_argument("--debug", action="store_true", help="DEBUG logging, all modules on console")
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = Path(args.input_file)

    if not input_path.is_file():
        logging.basicConfig(format=CONSOLE_FORMAT)
        logger.error("Input file not found: %s", input_path)
        return 1

    out_dir = resolve_output_dir(input_path, args.output_dir)
    handlers, previous_level = setup_logging(out_dir / LOG_FILE, args.debug)
    try:
        return run_pipeline(
            input_path,
            out_dir,
            dry_run=args.dry_run,
            interactive=not args.no_interactive,
            manual=args.manual,
        )
    finally:
        teardown_logging(handlers, previous_level)


def main(argv: list[str] | None = None) -> None:
    sys.exit(run_cli(argv))


if __name__ == "__main__":
    main()

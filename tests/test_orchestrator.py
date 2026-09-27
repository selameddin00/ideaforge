import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_raw.txt"

EXPECTED_FILES = {
    "transcript.json", "debate.json", "prd.json", "CLAUDE.md", "prompts.json", "pipeline.log",
}
UNKNOWNS = ["Who owns the generated spec?", "How accurate is the maturity score?"]


# --- Fakes (patched where orchestrator looks them up) -------------------------

def fake_observer(data, manual=False):
    data["maturity"] = {
        "score": 0.87,
        "triggered_by": "soft_score",
        "signals": {
            "lexical_saturation": True,
            "question_type_shift": True,
            "feasibility_query": True,
            "summary_confirmation": False,
        },
    }
    return data


def fake_debate(data):
    data["debate"] = {
        "rounds": [{
            "round": 1,
            "advocate": {"content": "for", "token_count": 300},
            "devil": {"content": "against", "token_count": 300},
        }],
        "termination_reason": "max_rounds",
    }
    meta = data.setdefault("meta", {})
    meta.update(total_tokens=600, total_input_tokens=400, total_output_tokens=200)
    return data


def fake_baron(data, output_path=None):
    data["synthesis"] = {
        "prd": {"problem_statement": "p", "unknowns": UNKNOWNS},
        "claude_md": "# X\n\n## What It Does\nstuff",
        "prompt_chain": [{"order": 1, "trigger": True, "input": "i", "task": "t",
                          "success_criteria": "s", "on_error": "e"}],
    }
    data["unknowns"] = list(UNKNOWNS)
    meta = data["meta"]
    meta["total_input_tokens"] += 1000
    meta["total_output_tokens"] += 500
    meta["total_tokens"] += 1500
    return data


@pytest.fixture
def outputs_dir(tmp_path):
    """Redirect the default outputs/ root so tests never touch the real one."""
    root = tmp_path / "outputs"
    with patch.object(orchestrator, "OUTPUTS_DIR", root):
        yield root


@pytest.fixture
def agents():
    with patch.object(orchestrator, "run_observer", side_effect=fake_observer) as obs, \
         patch.object(orchestrator, "run_debate", side_effect=fake_debate) as deb, \
         patch.object(orchestrator, "run_baron", side_effect=fake_baron) as bar, \
         patch("builtins.input", side_effect=AssertionError("input() must not be called")) as inp:
        yield {"observer": obs, "debate": deb, "baron": bar, "input": inp}


def default_dir(outputs_dir, suffix=""):
    return outputs_dir / f"{date.today():%Y-%m-%d}_transcript_raw{suffix}"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- Required scenarios -------------------------------------------------------

def test_happy_path(outputs_dir, agents):
    assert orchestrator.run_cli(["run", str(FIXTURE), "--no-interactive"]) == 0

    out = default_dir(outputs_dir)
    assert {p.name for p in out.iterdir()} == EXPECTED_FILES

    data = read_json(out / "transcript.json")
    assert data["message_count"] == 7                     # real parse ran
    assert data["maturity"]["score"] == 0.87
    assert data["debate"]["rounds"]
    assert data["synthesis"]["prompt_chain"]
    meta = data["meta"]
    assert meta["total_tokens"] == 2100
    assert meta["total_cost_usd"] == pytest.approx(1400 * 3 / 1e6 + 700 * 15 / 1e6)
    assert meta["pipeline_duration_sec"] >= 0

    assert read_json(out / "debate.json") == data["debate"]
    assert read_json(out / "prd.json") == data["synthesis"]["prd"]
    assert read_json(out / "prompts.json") == data["synthesis"]["prompt_chain"]
    assert (out / "CLAUDE.md").read_text(encoding="utf-8").startswith("# X")

    log = (out / "pipeline.log").read_text(encoding="utf-8")
    for stage in ("parse", "observer", "debate", "baron"):
        assert f"▶ {stage}" in log and f"✓ {stage}" in log
    assert "Pipeline tamamlandı" in log

    # Sequential, one call each; baron gets the output path for partial saves.
    for name in ("observer", "debate", "baron"):
        assert agents[name].call_count == 1
    assert agents["baron"].call_args.kwargs["output_path"] == str(out / "transcript.json")


def test_output_dir_override(tmp_path, outputs_dir, agents):
    custom = tmp_path / "test_out"
    assert orchestrator.run_cli(["run", str(FIXTURE), "--output-dir", str(custom), "--no-interactive"]) == 0
    assert {p.name for p in custom.iterdir()} == EXPECTED_FILES
    assert not outputs_dir.exists()


def test_dry_run(outputs_dir, agents):
    assert orchestrator.run_cli(["run", str(FIXTURE), "--dry-run"]) == 0

    assert agents["observer"].call_count == 1
    assert agents["debate"].call_count == 0
    assert agents["baron"].call_count == 0
    out = default_dir(outputs_dir)
    assert {p.name for p in out.iterdir()} == {"transcript.json", "pipeline.log"}
    assert read_json(out / "transcript.json")["maturity"]["score"] == 0.87


def test_partial_failure(outputs_dir, agents):
    agents["observer"].side_effect = RuntimeError("Ollama exploded")
    assert orchestrator.run_cli(["run", str(FIXTURE), "--no-interactive"]) == 1

    out = default_dir(outputs_dir)
    data = read_json(out / "transcript.json")
    assert data["message_count"] == 7          # parse output kept
    assert "maturity" not in data
    assert agents["debate"].call_count == 0
    log = (out / "pipeline.log").read_text(encoding="utf-8")
    assert "✗ observer" in log and "Ollama exploded" in log


def test_unknowns_no_interactive(outputs_dir, agents):
    assert orchestrator.run_cli(["run", str(FIXTURE), "--no-interactive"]) == 0

    agents["input"].assert_not_called()
    data = read_json(default_dir(outputs_dir) / "transcript.json")
    assert data["synthesis"]["unknowns"] == [{"question": q, "answer": None} for q in UNKNOWNS]
    assert "Unknowns loop skipped (--no-interactive)" in (
        default_dir(outputs_dir) / "pipeline.log").read_text(encoding="utf-8")


# --- Extra coverage -----------------------------------------------------------

def test_unknowns_interactive_answers_saved(outputs_dir, agents):
    agents["input"].side_effect = ["y", "The user", ""]
    assert orchestrator.run_cli(["run", str(FIXTURE)]) == 0

    assert agents["input"].call_count == 3
    prompts = [c.args[0] for c in agents["input"].call_args_list]
    assert "Baron 2 belirsiz nokta" in prompts[0] and "[Y/n]" in prompts[0]
    assert prompts[1].startswith("[1/2] Who owns")
    data = read_json(default_dir(outputs_dir) / "transcript.json")
    assert data["synthesis"]["unknowns"] == [
        {"question": UNKNOWNS[0], "answer": "The user"},
        {"question": UNKNOWNS[1], "answer": None},
    ]


def test_unknowns_declined(outputs_dir, agents):
    agents["input"].side_effect = ["n"]
    assert orchestrator.run_cli(["run", str(FIXTURE)]) == 0
    assert agents["input"].call_count == 1
    assert "skipped by user" in (default_dir(outputs_dir) / "pipeline.log").read_text(encoding="utf-8")


def test_same_day_collision_gets_suffix(outputs_dir, agents):
    for _ in range(3):
        assert orchestrator.run_cli(["run", str(FIXTURE), "--dry-run"]) == 0
    assert default_dir(outputs_dir).is_dir()
    assert default_dir(outputs_dir, "_001").is_dir()
    assert default_dir(outputs_dir, "_002").is_dir()


def test_slugify():
    assert orchestrator.slugify(Path("My Chat Export.TXT")) == "my_chat_export"
    assert orchestrator.slugify(Path("input.txt")) == "input"


def test_immature_idea_stops_before_debate(outputs_dir, agents):
    def low_score(data, manual=False):
        fake_observer(data)
        data["maturity"]["score"] = 0.5
        return data

    agents["observer"].side_effect = low_score
    assert orchestrator.run_cli(["run", str(FIXTURE), "--no-interactive"]) == 0
    assert agents["debate"].call_count == 0
    assert "olgun değil" in (default_dir(outputs_dir) / "pipeline.log").read_text(encoding="utf-8")


def test_manual_flag_forwarded(outputs_dir, agents):
    orchestrator.run_cli(["run", str(FIXTURE), "--dry-run", "--manual"])
    assert agents["observer"].call_args.kwargs["manual"] is True


def test_debate_system_exit_is_caught_and_partial_saved(outputs_dir, agents):
    agents["debate"].side_effect = SystemExit(1)       # debate.py on retry exhaustion
    assert orchestrator.run_cli(["run", str(FIXTURE), "--no-interactive"]) == 1
    data = read_json(default_dir(outputs_dir) / "transcript.json")
    assert data["maturity"]["score"] == 0.87
    assert "debate" not in data
    assert agents["baron"].call_count == 0


def test_cost_limit_warning(outputs_dir, agents, caplog):
    def expensive(data, output_path=None):
        fake_baron(data)
        data["meta"]["total_output_tokens"] += 40_000   # +$0.60
        return data

    agents["baron"].side_effect = expensive
    assert orchestrator.run_cli(["run", str(FIXTURE), "--no-interactive"]) == 0   # not stopped
    assert "Maliyet limiti aşıldı" in (default_dir(outputs_dir) / "pipeline.log").read_text(encoding="utf-8")


def test_missing_input_exits_1(tmp_path, outputs_dir):
    assert orchestrator.run_cli(["run", str(tmp_path / "nope.txt")]) == 1
    assert not outputs_dir.exists()


def test_main_exits_with_code(outputs_dir, agents):
    with pytest.raises(SystemExit) as exc:
        orchestrator.main(["run", str(FIXTURE), "--dry-run"])
    assert exc.value.code == 0


def test_traceback_in_log_not_console(outputs_dir, agents, capsys):
    agents["observer"].side_effect = RuntimeError("Ollama exploded")
    orchestrator.run_cli(["run", str(FIXTURE), "--no-interactive"])

    console = capsys.readouterr().err
    assert "Ollama exploded" in console and "Traceback" not in console
    assert "Traceback" in (default_dir(outputs_dir) / "pipeline.log").read_text(encoding="utf-8")

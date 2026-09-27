import json
import logging
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baron  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
POST_DEBATE = FIXTURES / "post_debate.json"

PRD = {
    "problem_statement": "Turning AI conversations into specs is manual and slow.",
    "target_users": ["solo developers"],
    "core_features": ["maturity detection", "debate", "spec synthesis"],
    "constraints": ["pipeline cost under $0.50"],
    "success_metrics": ["spec generated in under 5 minutes"],
    "unknowns": ["how accurate is the maturity score?", "who owns the generated spec?"],
}
CLAUDE_MD = """# SpecForge

## What It Does
Turns conversations into specs.

## Architecture
parse -> score -> debate -> synthesize

## Key Design Decisions
Sequential agents, JSON on disk.

## What NOT To Do
Do not run agents in parallel.
"""
CHAIN = [
    {"order": 1, "trigger": True, "input": "empty repo", "task": "Scaffold the project.",
     "success_criteria": "pytest runs", "on_error": "Check Python version."},
    {"order": 2, "trigger": False, "input": "scaffold", "task": "Implement parse.py.",
     "success_criteria": "tests/test_parse.py passes", "on_error": "Inspect fixtures."},
]


@pytest.fixture
def transcript_path(tmp_path):
    dst = tmp_path / "in" / "transcript.json"
    dst.parent.mkdir()
    shutil.copy(POST_DEBATE, dst)
    return dst


@pytest.fixture
def output_path(tmp_path):
    return tmp_path / "out" / "transcript.json"


@pytest.fixture(autouse=True)
def no_retry_wait():
    """Keep the retry policy but skip real backoff sleeps."""
    with patch.object(baron._call_prd.retry, "wait", wait_none()), \
         patch.object(baron._call_claude_md.retry, "wait", wait_none()), \
         patch.object(baron._call_prompts.retry, "wait", wait_none()):
        yield


def system_of(path):
    return path.read_text(encoding="utf-8").strip()


def response(text, input_tokens=1000, output_tokens=500, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )


def fenced(obj):
    return f"Here you go:\n```json\n{json.dumps(obj)}\n```"


GOOD_REPLIES = {
    "prd": fenced(PRD),
    "claude_md": CLAUDE_MD,
    "prompts": fenced(CHAIN),
}


def make_client(replies=None, **usage):
    """Fake client that answers by system prompt. replies[k] may be a list (per attempt)."""
    replies = {**GOOD_REPLIES, **(replies or {})}
    by_system = {
        system_of(baron.PRD_PROMPT_PATH): "prd",
        system_of(baron.CLAUDE_MD_PROMPT_PATH): "claude_md",
        system_of(baron.PROMPTS_PROMPT_PATH): "prompts",
    }
    attempts = {"prd": 0, "claude_md": 0, "prompts": 0}

    def create(**kwargs):
        key = by_system[kwargs["system"]]
        reply = replies[key]
        if isinstance(reply, list):
            reply = reply[min(attempts[key], len(reply) - 1)]
        attempts[key] += 1
        if isinstance(reply, Exception):
            raise reply
        return response(reply, **usage)

    client = MagicMock()
    client.messages.create.side_effect = create
    client.by_system = by_system
    return client


def called_keys(client):
    return [client.by_system[c.kwargs["system"]] for c in client.messages.create.call_args_list]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_all_required_fields_present(transcript_path, output_path):
    result = baron.run_baron(str(transcript_path), str(output_path), client=make_client())
    synthesis = result["synthesis"]

    assert synthesis["prd"] == PRD
    assert isinstance(synthesis["claude_md"], str) and "## What It Does" in synthesis["claude_md"]
    assert synthesis["prompt_chain"] == CHAIN
    for item in synthesis["prompt_chain"]:
        assert set(item) == set(baron.PROMPT_FIELDS)
    assert result["unknowns"] == PRD["unknowns"]

    # Earlier pipeline stages preserved.
    assert result["debate"]["rounds"]
    assert result["maturity"]["score"] == 0.87

    out_dir = output_path.parent
    assert (out_dir / "CLAUDE.md").read_text(encoding="utf-8").startswith("# SpecForge")
    assert read_json(out_dir / "prompts.json") == CHAIN
    assert read_json(output_path) == result


def test_tokens_and_cost_accumulate(transcript_path, output_path):
    before = read_json(transcript_path)["meta"]
    result = baron.run_baron(str(transcript_path), str(output_path), client=make_client())
    meta = result["meta"]

    assert meta["total_tokens"] == before["total_tokens"] + 3 * 1500
    assert meta["total_input_tokens"] == before["total_input_tokens"] + 3000
    assert meta["total_output_tokens"] == before["total_output_tokens"] + 1500
    expected = meta["total_input_tokens"] * 3 / 1e6 + meta["total_output_tokens"] * 15 / 1e6
    assert meta["total_cost_usd"] == pytest.approx(expected)
    assert meta["total_cost_usd"] > 0


@pytest.mark.parametrize("field", list(baron.PROMPT_FIELDS))
def test_validate_raises_on_missing_field(field):
    chain = [dict(item) for item in CHAIN]
    del chain[1][field]
    with pytest.raises(ValueError, match=field):
        baron.validate_prompt_chain(chain)


@pytest.mark.parametrize("field,bad", [
    ("order", "1"), ("order", True), ("trigger", "yes"), ("task", 42), ("on_error", "   "),
])
def test_validate_raises_on_bad_type_or_empty(field, bad):
    chain = [dict(item) for item in CHAIN]
    chain[0][field] = bad
    with pytest.raises(ValueError):
        baron.validate_prompt_chain(chain)


@pytest.mark.parametrize("chain", [[], "not a list", [CHAIN[1]], [CHAIN[0], CHAIN[0]]])
def test_validate_raises_on_bad_chain_shape(chain):
    with pytest.raises(ValueError):
        baron.validate_prompt_chain(chain)


def test_validate_accepts_good_chain():
    baron.validate_prompt_chain(CHAIN)


def test_sequential_calls_order(transcript_path, output_path):
    client = make_client()
    baron.run_baron(str(transcript_path), str(output_path), client=client)

    assert called_keys(client) == ["prd", "claude_md", "prompts"]
    calls = client.messages.create.call_args_list
    # Each call gets the previous call's output as input.
    assert "Devil's Advocate" in calls[0].kwargs["messages"][0]["content"]
    assert PRD["problem_statement"] in calls[1].kwargs["messages"][0]["content"]
    assert "## Key Design Decisions" in calls[2].kwargs["messages"][0]["content"]
    assert PRD["problem_statement"] in calls[2].kwargs["messages"][0]["content"]


def test_partial_write_after_each_call(transcript_path, output_path):
    snapshots = []
    client = make_client()
    inner = client.messages.create.side_effect

    def create(**kwargs):
        snapshots.append(read_json(output_path) if output_path.exists() else None)
        return inner(**kwargs)

    client.messages.create.side_effect = create
    baron.run_baron(str(transcript_path), str(output_path), client=client)

    assert snapshots[0] is None                                  # before call 1
    assert snapshots[1]["synthesis"]["prd"] == PRD               # after call 1
    assert snapshots[2]["synthesis"]["claude_md"] == CLAUDE_MD.strip()  # after call 2
    assert "prompt_chain" not in snapshots[2]["synthesis"] or not snapshots[2]["synthesis"]["prompt_chain"]


def test_partial_results_survive_failure_in_last_call(transcript_path, output_path):
    client = make_client({"prompts": "definitely not json"})
    with pytest.raises(baron.BaronParseError):
        baron.run_baron(str(transcript_path), str(output_path), client=client)

    on_disk = read_json(output_path)
    assert on_disk["synthesis"]["prd"] == PRD
    assert on_disk["synthesis"]["claude_md"] == CLAUDE_MD.strip()
    assert not on_disk["synthesis"]["prompt_chain"]
    assert (output_path.parent / "CLAUDE.md").exists()
    assert not (output_path.parent / "prompts.json").exists()


def test_cost_warning_logged(transcript_path, output_path, caplog):
    # 3 calls x (100k in, 10k out) = $0.90 + $0.45 -> over $0.50
    client = make_client(input_tokens=100_000, output_tokens=10_000)
    with caplog.at_level(logging.WARNING, logger="ideaforge.baron"):
        result = baron.run_baron(str(transcript_path), str(output_path), client=client)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "exceeds" in r.message]
    assert warnings
    # Pipeline is not stopped.
    assert result["synthesis"]["prompt_chain"] == CHAIN


def test_no_cost_warning_under_budget(transcript_path, output_path, caplog):
    with caplog.at_level(logging.WARNING, logger="ideaforge.baron"):
        baron.run_baron(str(transcript_path), str(output_path), client=make_client())
    assert not [r for r in caplog.records if "exceeds" in r.message]


def test_baron_parse_error_triggers_retry(transcript_path, output_path):
    client = make_client({"prd": ["I think the PRD is...", "```json\n{broken\n```", fenced(PRD)]})
    result = baron.run_baron(str(transcript_path), str(output_path), client=client)

    assert called_keys(client) == ["prd", "prd", "prd", "claude_md", "prompts"]
    assert result["synthesis"]["prd"] == PRD


def test_parse_error_exhausts_after_3_attempts(transcript_path, output_path):
    client = make_client({"prd": "never json"})
    with pytest.raises(baron.BaronParseError):
        baron.run_baron(str(transcript_path), str(output_path), client=client)
    assert called_keys(client) == ["prd"] * baron.MAX_ATTEMPTS


def test_invalid_chain_and_missing_sections_are_retried(transcript_path, output_path):
    bad_chain = [dict(CHAIN[0])]
    del bad_chain[0]["on_error"]
    client = make_client({
        "claude_md": ["# Title\n\n## What It Does\nOnly one section.", CLAUDE_MD],
        "prompts": [fenced(bad_chain), fenced(CHAIN)],
    })
    result = baron.run_baron(str(transcript_path), str(output_path), client=client)
    assert called_keys(client) == ["prd", "claude_md", "claude_md", "prompts", "prompts"]
    assert result["synthesis"]["prompt_chain"] == CHAIN


def test_parse_json_response_strategies():
    assert baron.parse_json_response('text\n```json\n{"a": 1}\n```\nmore') == {"a": 1}
    assert baron.parse_json_response('  [1, 2]  ') == [1, 2]
    with pytest.raises(baron.BaronParseError):
        baron.parse_json_response("no json here")


def test_claude_md_outer_fence_stripped(transcript_path, output_path):
    client = make_client({"claude_md": f"```markdown\n{CLAUDE_MD}```"})
    result = baron.run_baron(str(transcript_path), str(output_path), client=client)
    assert result["synthesis"]["claude_md"].startswith("# SpecForge")


def test_default_output_path(tmp_path):
    data = read_json(POST_DEBATE)
    out = baron.default_output_path(POST_DEBATE, data)
    assert out.parent.parent == baron.OUTPUTS_DIR
    assert out.parent.name.endswith("_i-want-to-build-a")
    assert out.name == "transcript.json"

    inside = baron.OUTPUTS_DIR / "2026-09-27_demo" / "transcript.json"
    assert baron.default_output_path(inside, data) == inside


def test_cli_missing_input_exits_1(tmp_path):
    assert baron.main([str(tmp_path / "nope.json")]) == 1


def test_dict_input_without_output_path_writes_nothing(transcript_path, tmp_path):
    data = read_json(transcript_path)
    before = set(tmp_path.rglob("*"))
    result = baron.run_baron(data, client=make_client())

    assert result is data
    assert result["synthesis"]["prompt_chain"] == CHAIN
    assert set(tmp_path.rglob("*")) == before


def test_path_input_requires_output_path(transcript_path):
    with pytest.raises(ValueError):
        baron.run_baron(str(transcript_path), client=make_client())

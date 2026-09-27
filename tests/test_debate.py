import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
import pytest
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import debate  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Distinct vocabularies so consecutive devil turns are clearly novel.
ADVOCATE_TEXTS = [
    "Users save hours because maturity detection removes manual review of chats.",
    "A local Qwen model keeps scoring private and nearly free to operate.",
    "Structured prompt chains turn vague brainstorming into executable engineering tasks.",
]
DEVIL_TEXTS = [
    "Regex triggers will misfire constantly on casual phrasing and flood users.",
    "Sonnet costs compound across six calls, silently breaking any budget promise.",
    "Nobody trusts auto-generated CLAUDE.md files without heavy manual editing anyway.",
]


@pytest.fixture
def transcript_path(tmp_path):
    data = json.loads((FIXTURES / "transcript_expected.json").read_text(encoding="utf-8"))
    data["session_id"] = "00000000-0000-4000-8000-000000000000"
    data["parsed_at"] = "2026-09-27T12:00:00+00:00"
    data["maturity"] = {
        "score": 0.75,
        "triggered_by": "soft_score",
        "signals": {
            "lexical_saturation": True,
            "question_type_shift": True,
            "feasibility_query": True,
            "summary_confirmation": False,
        },
    }
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_retry_wait():
    """Keep tenacity's retry policy but skip the real backoff sleeps."""
    with patch.object(debate.call_agent.retry, "wait", wait_none()):
        yield


def make_response(text, input_tokens=120, output_tokens=80):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="end_turn",
    )


def make_client(advocate_texts=ADVOCATE_TEXTS, devil_texts=DEVIL_TEXTS):
    """Fake client: picks Advocate/Devil text by system prompt, in call order."""
    advocate_system = debate.ADVOCATE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    counters = {"advocate": 0, "devil": 0}

    def create(**kwargs):
        side = "advocate" if kwargs["system"] == advocate_system else "devil"
        texts = advocate_texts if side == "advocate" else devil_texts
        text = texts[counters[side]]
        counters[side] += 1
        return make_response(text)

    client = MagicMock()
    client.messages.create.side_effect = create
    return client


def api_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIError("server exploded", request, body=None)


def test_three_rounds_complete(transcript_path):
    client = make_client()
    result = debate.run_debate(transcript_path, client=client)

    assert len(result["debate"]["rounds"]) == 3
    assert result["debate"]["termination_reason"] == "max_rounds"
    assert client.messages.create.call_count == 6

    # Sequential alternation: advocate, devil, advocate, devil, ...
    advocate_system = debate.ADVOCATE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    order = [c.kwargs["system"] == advocate_system for c in client.messages.create.call_args_list]
    assert order == [True, False] * 3


def test_early_termination_on_saturation(transcript_path):
    # Devil repeats itself every round. Saturation is checked from round 2 on,
    # so the debate must stop right after round 2 and never run round 3.
    devil = [DEVIL_TEXTS[1], DEVIL_TEXTS[1], DEVIL_TEXTS[1]]
    client = make_client(devil_texts=devil)
    result = debate.run_debate(transcript_path, client=client)

    assert result["debate"]["termination_reason"] == "argument_saturation"
    assert len(result["debate"]["rounds"]) == 2
    assert client.messages.create.call_count == 4


def test_token_count_logged(transcript_path):
    result = debate.run_debate(transcript_path, client=make_client())
    rounds = result["debate"]["rounds"]
    for r in rounds:
        assert r["advocate"]["token_count"] > 0
        assert r["devil"]["token_count"] > 0
        assert r["advocate"]["token_count"] == 200  # 120 in + 80 out, from usage
    assert result["meta"]["total_tokens"] == 200 * 2 * len(rounds)


def test_retry_on_api_error(transcript_path):
    client = make_client()
    real_create = client.messages.create.side_effect
    errors = [api_error(), api_error()]

    def flaky_create(**kwargs):
        if errors:
            raise errors.pop(0)
        return real_create(**kwargs)

    client.messages.create.side_effect = flaky_create
    result = debate.run_debate(transcript_path, client=client)

    assert result["debate"]["termination_reason"] == "max_rounds"
    assert len(result["debate"]["rounds"]) == 3
    assert client.messages.create.call_count == 6 + 2


def test_retry_exhaustion_exits_1_and_leaves_file_untouched(transcript_path):
    original = transcript_path.read_bytes()
    client = MagicMock()
    client.messages.create.side_effect = api_error()

    with pytest.raises(SystemExit) as exc_info:
        debate.run_debate(transcript_path, client=client)

    assert exc_info.value.code == 1
    assert client.messages.create.call_count == 4
    assert transcript_path.read_bytes() == original
    assert not transcript_path.with_name("transcript.json.tmp").exists()


def test_atomic_write_on_success(transcript_path):
    debate.run_debate(transcript_path, client=make_client())

    on_disk = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert "debate" in on_disk
    assert on_disk["debate"]["rounds"]
    assert not transcript_path.with_name("transcript.json.tmp").exists()
    # Existing fields preserved.
    assert on_disk["maturity"]["score"] == 0.75
    assert on_disk["session_id"] == "00000000-0000-4000-8000-000000000000"


def test_output_schema_valid(transcript_path):
    debate.run_debate(transcript_path, client=make_client())
    result = json.loads(transcript_path.read_text(encoding="utf-8"))["debate"]

    assert result["termination_reason"] in {"argument_saturation", "max_rounds"}
    for i, r in enumerate(result["rounds"], start=1):
        assert r["round"] == i
        for side in ("advocate", "devil"):
            assert isinstance(r[side]["content"], str) and r[side]["content"]
            assert isinstance(r[side]["token_count"], int)


def test_saturation_detected_in_round_3(transcript_path):
    # Rounds 2 and 3 identical (round 1 different): detected at end of round 3.
    devil = [DEVIL_TEXTS[0], DEVIL_TEXTS[1], DEVIL_TEXTS[1]]
    result = debate.run_debate(transcript_path, client=make_client(devil_texts=devil))
    assert result["debate"]["termination_reason"] == "argument_saturation"
    assert len(result["debate"]["rounds"]) == 3


def test_novelty_ratio_pure_function():
    assert debate.compute_novelty("the same words here", "The SAME words here") == 0.0
    assert debate.compute_novelty("alpha beta gamma", "delta epsilon zeta") == 1.0
    # {a,b,c} vs {b,c,d}: intersection 2, union 4 -> jaccard 0.5 -> novelty 0.5
    assert debate.compute_novelty("a b c", "b c d") == pytest.approx(0.5)


def test_context_uses_prd_when_present(transcript_path):
    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    data["synthesis"] = {"prd": {"title": "UNIQUE_PRD_MARKER"}}
    transcript_path.write_text(json.dumps(data), encoding="utf-8")

    client = make_client()
    debate.run_debate(transcript_path, client=client)
    first_user_msg = client.messages.create.call_args_list[0].kwargs["messages"][0]["content"]
    assert "UNIQUE_PRD_MARKER" in first_user_msg


def test_message_roles_alternate(transcript_path):
    client = make_client()
    debate.run_debate(transcript_path, client=client)
    for call in client.messages.create.call_args_list:
        roles = [m["role"] for m in call.kwargs["messages"]]
        assert roles[0] == "user" and roles[-1] == "user"
        assert all(a != b for a, b in zip(roles, roles[1:]))


def test_cli_missing_transcript_exits_1(tmp_path):
    assert debate.main(["--transcript", str(tmp_path / "nope.json")]) == 1

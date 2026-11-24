import json
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import jsonschema
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import observer  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# transcript_raw.txt parsed output; contains no hard-trigger phrases.
BASE_FIXTURE = FIXTURES / "transcript_expected.json"

ALL_TRUE = {k: True for k in observer.SIGNAL_KEYS}


@pytest.fixture
def transcript_path(tmp_path):
    """Fresh copy of the fixture so tests never mutate tests/fixtures/."""
    dst = tmp_path / "transcript.json"
    shutil.copy(BASE_FIXTURE, dst)
    return dst


def add_message(path, content, role="user"):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["transcript"].append({
        "id": len(data["transcript"]) + 1,
        "role": role,
        "content": content,
        "timestamp": None,
        "word_count": len(content.split()),
    })
    path.write_text(json.dumps(data), encoding="utf-8")


def ollama_response(signals):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"response": json.dumps(signals)}
    return resp


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_hard_trigger_fires_instantly(transcript_path):
    add_message(transcript_path, "OK, how do we implement this now?")
    with patch("observer.requests.post") as mock_post:
        start = time.perf_counter()
        observer.run(transcript_path)
        elapsed = time.perf_counter() - start

    maturity = read_json(transcript_path)["maturity"]
    assert maturity["score"] == 1.0
    assert maturity["triggered_by"] == "hard_trigger"
    assert all(maturity["signals"].values())
    assert mock_post.call_count == 0
    assert elapsed < 0.1, f"hard trigger took {elapsed * 1000:.1f}ms"


def test_hard_trigger_no_llm_call(transcript_path):
    add_message(transcript_path, "Bence artık repo oluştur ve başlayalım.", role="assistant")
    with patch("observer.requests.post") as mock_post:
        observer.run(transcript_path)
    assert mock_post.call_count == 0
    assert read_json(transcript_path)["maturity"]["triggered_by"] == "hard_trigger"


def test_hard_trigger_1000_messages_under_500ms():
    transcript = [
        {"id": i, "role": "user", "content": f"Just chatting about idea number {i} and why it matters."}
        for i in range(1, 1000)
    ]
    transcript.append({"id": 1000, "role": "user", "content": "Is this production-ready?"})
    with patch("observer.requests.post") as mock_post:
        start = time.perf_counter()
        maturity = observer.evaluate_maturity(transcript)
        elapsed = time.perf_counter() - start
    assert maturity["triggered_by"] == "hard_trigger"
    assert mock_post.call_count == 0
    assert elapsed < 0.5, f"1000-message hard trigger took {elapsed * 1000:.1f}ms"


def test_soft_score_all_true(transcript_path):
    with patch("observer.requests.post", return_value=ollama_response(ALL_TRUE)) as mock_post:
        observer.run(transcript_path)

    maturity = read_json(transcript_path)["maturity"]
    assert maturity["score"] == 1.0
    assert maturity["triggered_by"] == "soft_score"
    assert mock_post.call_count == 1
    body = mock_post.call_args.kwargs["json"]
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert mock_post.call_args.args[0].endswith("/api/generate")


def test_soft_score_partial(transcript_path):
    signals = {
        "lexical_saturation": True,
        "question_type_shift": False,
        "feasibility_query": True,
        "summary_confirmation": False,
    }
    with patch("observer.requests.post", return_value=ollama_response(signals)):
        observer.run(transcript_path)

    maturity = read_json(transcript_path)["maturity"]
    assert maturity["score"] == 0.5
    assert maturity["signals"] == signals


def test_soft_score_retries_then_raises(transcript_path):
    bad = MagicMock()
    bad.raise_for_status.return_value = None
    bad.json.return_value = {"response": '{"lexical_saturation": true}'}
    with patch("observer.requests.post", return_value=bad) as mock_post:
        with pytest.raises(observer.InvalidLLMResponse):
            observer.run(transcript_path)
    assert mock_post.call_count == observer.MAX_LLM_ATTEMPTS  # 1 call + 2 retries
    assert "maturity" not in read_json(transcript_path)


def test_soft_score_recovers_on_retry(transcript_path):
    bad = MagicMock()
    bad.raise_for_status.return_value = None
    bad.json.return_value = {"response": "not json at all"}
    with patch("observer.requests.post", side_effect=[bad, ollama_response(ALL_TRUE)]) as mock_post:
        observer.run(transcript_path)
    assert mock_post.call_count == 2
    assert read_json(transcript_path)["maturity"]["score"] == 1.0


def test_prompt_uses_last_20_messages_in_role_content_format():
    transcript = [{"id": i, "role": "user", "content": f"msg-{i}"} for i in range(1, 31)]
    prompt = observer.build_prompt(transcript)
    assert "user: msg-11" in prompt
    assert "user: msg-30" in prompt
    assert "msg-10\n" not in prompt
    # Rough token budget check (~4 chars/token) including the system prompt.
    long = [{"id": i, "role": "user", "content": "word " * 1000} for i in range(20)]
    assert (len(observer.build_prompt(long)) + len(observer.SYSTEM_PROMPT)) / 4 < 2000


def test_output_json_schema_valid(transcript_path, tmp_path):
    out = tmp_path / "out" / "result.json"
    original = read_json(transcript_path)
    with patch("observer.requests.post", return_value=ollama_response(ALL_TRUE)):
        observer.run(transcript_path, out)

    result = read_json(out)
    jsonschema.validate(result["maturity"], observer.MATURITY_SCHEMA)
    for key, value in original.items():
        assert result[key] == value
    # --output given: input file is left untouched.
    assert read_json(transcript_path) == original


def test_existing_maturity_is_replaced_other_fields_kept(transcript_path):
    data = read_json(transcript_path)
    data["maturity"] = {"score": 0.0, "triggered_by": "soft_score", "signals": {}}
    data["debate"] = {"rounds": []}
    transcript_path.write_text(json.dumps(data), encoding="utf-8")

    observer.run(transcript_path, manual=True)
    result = read_json(transcript_path)
    assert result["debate"] == {"rounds": []}
    assert result["maturity"]["triggered_by"] == "manual"


def test_manual_flag(transcript_path):
    with patch("observer.requests.post") as mock_post:
        assert observer.main(["--input", str(transcript_path), "--manual"]) == 0
    maturity = read_json(transcript_path)["maturity"]
    assert maturity["score"] == 1.0
    assert maturity["triggered_by"] == "manual"
    assert mock_post.call_count == 0


def test_ollama_connection_error_raises(transcript_path):
    with patch("observer.requests.post", side_effect=requests.exceptions.ConnectionError("refused")) as mock_post:
        with pytest.raises(requests.exceptions.ConnectionError):
            observer.run(transcript_path)
    assert mock_post.call_count == 1  # connection errors are not retried


def test_missing_transcript_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        observer.run(tmp_path / "does_not_exist.json")


def test_invalid_json_raises(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        observer.run(bad)


def test_mature_fixture_fires_hard_trigger():
    import parse

    transcript = parse.parse_file(FIXTURES / "transcript_mature.txt")["transcript"]
    with patch("observer.requests.post") as mock_post:
        maturity = observer.evaluate_maturity(transcript)
    assert maturity["triggered_by"] == "hard_trigger"
    assert maturity["score"] == 1.0
    assert mock_post.call_count == 0
    # The fixture exercises every hard trigger pattern, not just the first.
    for pattern in observer.HARD_TRIGGER_PATTERNS:
        assert any(pattern.search(m["content"]) for m in transcript), pattern.pattern

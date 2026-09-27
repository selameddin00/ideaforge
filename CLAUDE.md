# IdeaForge — Project Constitution

**Maintainer:** Selameddin · Ozal's Lab  
**Version:** 0.1 — Pre-MVP  
**Last Updated:** 2026-09-27

---

## What This Project Does

IdeaForge is a multi-agent pipeline that monitors an AI conversation, detects when an idea has matured, then automatically runs a structured debate, synthesizes the result, and produces actionable Claude Code prompts.

**One-line flow:**
```
Transcript → Dikizci (maturity check) → Debate (Advocate vs Devil) → Baron (synthesize) → CLAUDE.md + Prompt Chain
```

The pipeline is sequential. Agents do NOT run in parallel. Each agent writes to a shared JSON file and passes it to the next.

---

## Architecture

```
parse.py        →  transcript.json
observer.py     →  transcript.json (adds maturity + trigger fields)
debate.py       →  debate.json
baron.py        →  prd.json + CLAUDE.md + prompts.json
orchestrator.py →  runs the full chain, manages I/O
```

### Agent Responsibilities

| File | Agent | Runtime | Task |
|---|---|---|---|
| `parse.py` | Parser | Local Python | Raw text → structured JSON |
| `observer.py` | Dikizci | Local · Ollama Qwen 2.5 7B | Maturity scoring + trigger |
| `debate.py` | Advocate + Devil | API · Claude Sonnet | 3-round structured debate |
| `baron.py` | Baron | API · Claude Sonnet | 3 sequential calls: PRD → CLAUDE.md → Prompts |
| `orchestrator.py` | Orchestrator | Local Python | Pipeline runner, CLI entry point |

---

## Implementation Details

### The Shared JSON Schema

Every agent reads and enriches a single JSON file. Never break this schema.

```json
{
  "session_id": "uuid-v4",
  "parsed_at": "ISO-8601",
  "transcript": [
    { "id": 1, "role": "user", "content": "...", "timestamp": "...", "word_count": 87 }
  ],
  "maturity": {
    "score": 0.87,
    "triggered_by": "hard_trigger | soft_score | manual",
    "signals": {
      "lexical_saturation": true,
      "question_type_shift": true,
      "feasibility_query": true,
      "summary_confirmation": false
    }
  },
  "debate": {
    "rounds": [
      {
        "round": 1,
        "advocate": { "content": "...", "token_count": 0 },
        "devil":    { "content": "...", "token_count": 0 }
      }
    ],
    "termination_reason": "argument_saturation | max_rounds"
  },
  "synthesis": {
    "prd": {},
    "claude_md": "...",
    "prompt_chain": [
      {
        "order": 1,
        "trigger": true,
        "input": "...",
        "task": "...",
        "success_criteria": "...",
        "on_error": "..."
      }
    ]
  },
  "unknowns": [],
  "meta": {
    "total_tokens": 0,
    "total_cost_usd": 0.0,
    "pipeline_duration_sec": 0
  }
}
```

### Key Design Decisions

- **JSON is the single source of truth.** If an agent fails, the partial JSON tells you exactly where it stopped.
- **No shared state in memory.** Every agent reads from disk, writes to disk. This makes each agent independently testable.
- **Hard triggers bypass soft scoring.** If a regex hard trigger fires in observer.py, maturity score is immediately set to 1.0 — no LLM call needed.
- **Debate terminates on argument saturation, not consensus.** Target: novelty ratio < 0.15 after round 2. Do NOT try to make agents agree.
- **Baron splits into 3 calls.** Do not merge them into one. Each call has a focused context window.
- **Every prompt in prompt_chain must have all 4 fields.** If Baron fails to generate one, validation raises an error — do not silently pass.

### Naming Conventions

- Files: `snake_case.py`
- Functions: `snake_case()`
- JSON keys: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Output folders: `outputs/YYYY-MM-DD_slug/`

### Environment

```bash
# .env (never commit this)
ANTHROPIC_API_KEY=sk-...

# requirements.txt
anthropic>=0.34
python-dotenv
tenacity
ollama
jsonschema
```

Python version: 3.11+  
Ollama must be running locally: `ollama serve`  
Qwen model: `ollama pull qwen2.5:7b`

---

## Testing

### Unit Test Strategy

Each module has an independent test. Tests use fixture files in `tests/fixtures/`.

| Test | What it checks |
|---|---|
| `test_parse.py` | 5 different input formats parse without error; output JSON validates against schema |
| `test_observer.py` | Hard trigger fires instantly; soft score computes correctly for known inputs |
| `test_debate.py` | 3 rounds complete; token count logged; termination logic triggers correctly |
| `test_baron.py` | All 4 mandatory fields present in every prompt; unknowns list populated correctly |
| `test_orchestrator.py` | Full pipeline runs on a fixture transcript; output folder created with expected files |

### Running Tests

```bash
python -m pytest tests/ -v
```

### Fixtures

- `tests/fixtures/transcript_raw.txt` — plain text conversation (no formatting)
- `tests/fixtures/transcript_claude_export.txt` — Claude.ai export format
- `tests/fixtures/transcript_expected.json` — expected parse output

### Success Criteria Per Phase

**parse.py:** 5 formats → valid JSON, < 1s for 100-message transcript  
**observer.py:** Hard trigger < 100ms, false positive < 20% on 10 manual tests  
**debate.py:** 3 rounds complete, output in debate.json, retry on API error  
**baron.py:** All 3 calls sequential, all 4 prompt fields validated  
**orchestrator.py:** Full pipeline on real transcript, CLAUDE.md generated, cost ≤ $0.50

---

## Cost & Token Limits

| Agent | Budget |
|---|---|
| Debate (6 calls total) | ~12k tokens |
| Baron (3 calls) | ~20k tokens |
| Single pipeline run | < $0.50 |

If `meta.total_cost_usd` exceeds $0.50, log a warning and continue. Do NOT hard-stop the pipeline.

---

## What NOT To Do

- Do not add a web framework in Phase 1-4. Terminal only.
- Do not run agents in parallel. Sequential only.
- Do not merge Baron's 3 calls into 1.
- Do not skip JSON schema validation after Baron output.
- Do not store API keys anywhere except `.env`.
- Do not write intermediate results to `/tmp`. Use `outputs/` folder.
- Do not use `print()` for logging. Use Python's `logging` module.

---

## Project Structure

```
ideaforge/
├── CLAUDE.md              ← you are here
├── parse.py
├── observer.py
├── debate.py
├── baron.py
├── orchestrator.py
├── prompts/
│   ├── advocate.txt
│   ├── devil.txt
│   ├── baron_prd.txt
│   ├── baron_claude_md.txt
│   └── baron_prompts.txt
├── tests/
│   ├── fixtures/
│   │   ├── transcript_raw.txt
│   │   ├── transcript_claude_export.txt
│   │   └── transcript_expected.json
│   ├── test_parse.py
│   ├── test_observer.py
│   ├── test_debate.py
│   ├── test_baron.py
│   └── test_orchestrator.py
├── outputs/               ← gitignore this
├── .env                   ← gitignore this
├── .gitignore
└── requirements.txt
```

---

*IdeaForge v0.1 · Ozal's Lab · 2026*

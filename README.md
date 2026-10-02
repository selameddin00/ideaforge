# IdeaForge

Multi-agent pipeline that detects idea maturity in AI conversations and auto-generates debate, synthesis, and Claude Code prompts.

IdeaForge reads an exported AI conversation and decides whether the idea in it has matured. It uses regex hard triggers plus a local Qwen 2.5 7B model (via Ollama) for soft maturity scoring. When the idea is ready, two Claude agents run a structured debate: an **Advocate** and a **Devil**, for up to 3 rounds. The debate stops on argument saturation, not consensus. A third agent, **Baron**, then turns the transcript and the debate into a PRD, a project `CLAUDE.md`, and an ordered chain of Claude Code prompts. Every agent reads and enriches one shared JSON document on disk. If a run fails, the partial `transcript.json` shows exactly where it stopped.

## Pipeline

```
Sohbet (.txt)
   │
   ▼
parse.py ──► observer.py ──► debate.py ──► baron.py ──► outputs/YYYY-MM-DD_slug/
 (parse)     (maturity)      (Advocate      (PRD → CLAUDE.md
                              vs Devil)       → prompt chain)
```

The agents run one after another, never in parallel. `orchestrator.py` (entry point: `ideaforge.py`) drives the whole chain.

## Agents

| File | Agent | What it does | LLM |
|---|---|---|---|
| `parse.py` | Parser | Turns a raw transcript (plain, Claude export, timestamped) into structured JSON | None (local Python) |
| `observer.py` | Dikizci | Scores maturity; a regex hard trigger sets the score to 1.0 without an LLM call | Ollama · Qwen 2.5 7B (local) |
| `debate.py` | Advocate + Devil | Runs a 3-round structured debate that stops on argument saturation | Claude Sonnet (API) |
| `baron.py` | Baron | Makes 3 sequential calls: PRD → CLAUDE.md → prompt chain, with schema validation | Claude Sonnet (API) |
| `orchestrator.py` | Orchestrator | Runs the pipeline and handles the CLI, output folders, logging and cost tracking | None (local Python) |

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally (`ollama serve`) with the `qwen2.5:7b` model. You don't need it with `--manual`.
- An Anthropic API key

## Installation

```bash
git clone https://github.com/selameddin00/ideaforge.git
cd ideaforge
pip install -r requirements.txt
ollama pull qwen2.5:7b
cp .env.example .env   # add your ANTHROPIC_API_KEY
```

`.env` settings:

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for `debate.py` and `baron.py` |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server address |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Model used by Dikizci |
| `MODEL` | `claude-sonnet-4-5` | Claude model used for debate and synthesis |

## Usage

```bash
python ideaforge.py run sohbet.txt
python ideaforge.py run sohbet.txt --manual   # without Ollama (forces maturity = 1.0)
```

Other flags:

| Flag | Effect |
|---|---|
| `--output-dir DIR` | Write to a custom folder instead of `outputs/YYYY-MM-DD_slug/` |
| `--dry-run` | Run only parse and observer; skip debate and Baron (no API cost) |
| `--no-interactive` | Skip the interactive Q&A loop for unknowns |
| `--debug` | Turn on DEBUG logging for every module on the console |

You can also run each agent on its own against a `transcript.json`, for example `python observer.py --input outputs/.../transcript.json`. See each file's docstring.

## Output

Each run creates a dated folder. The slug comes from the input file name, and a repeat run on the same day adds a suffix (`_001`, `_002`, …):

```
outputs/
└── 2026-09-27_sohbet/
    ├── transcript.json   # shared document: transcript + maturity + debate + synthesis + meta
    ├── debate.json       # Advocate/Devil rounds and termination reason
    ├── prd.json          # Baron call 1: product requirements
    ├── CLAUDE.md         # Baron call 2: project constitution for Claude Code
    ├── prompts.json      # Baron call 3: ordered prompt chain (input/task/success_criteria/on_error)
    └── pipeline.log      # full run log
```

A single run targets less than $0.50. If `meta.total_cost_usd` goes over that, the pipeline logs a warning and keeps going.

## Tests

```bash
python -m pytest tests/ -v
```

The tests use the fixtures in `tests/fixtures/` and mock the LLM calls, so they don't need Ollama or an API key.

## License

See [LICENSE](LICENSE).

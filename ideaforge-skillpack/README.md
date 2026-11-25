# IdeaForge Skill Pack

*Ozal's Lab · v1.0*

**Turn a messy AI brainstorm into a build plan: maturity check → structured debate → PRD, CLAUDE.md, and a ready-to-run prompt chain.**

---

## What this pack does

Most ideas die in the chat window. You go back and forth with an AI for an hour, the idea gets sharper, and then… nothing. You don't know if it's ready, nobody has argued against it, and turning the conversation into a spec and a set of coding prompts is work you keep putting off. IdeaForge is three skills that pick up exactly where the brainstorm ends. **Idea Maturity Detector** reads your conversation and tells you whether the idea is ready to build. **Structured Idea Debate** pits an Advocate against a Devil's Advocate until no new arguments appear. **Idea to PRD** turns what survived into a PRD, a CLAUDE.md project constitution, and five prompts you can paste straight into Claude Code, Cursor, or Copilot.

There is no server, no Python, no API key. Each skill is a plain instruction file your own AI agent reads and follows. State moves between skills as copyable text blocks: every skill ends with a block marked `=== IDEAFORGE · ... ===`, and you paste that block into the next skill. That keeps you in control at every step: you can read, edit, or stop between any two stages, and nothing is hidden in a database.

---

## Installation

### Option A: Agensi MCP connector (recommended)

1. Add the Agensi MCP server to your agent. The server URL is:
   ```
   https://mcp.agensi.io/mcp
   ```
   For agents that use a JSON MCP config, add an entry like:
   ```json
   {
     "mcpServers": {
       "agensi": { "type": "sse", "url": "https://mcp.agensi.io/mcp" }
     }
   }
   ```
   Where this config lives depends on your agent (Claude Code, Claude Desktop, Cursor, etc.). Follow Agensi's setup guide at agensi.io/docs/mcp-setup for your client.
2. Restart your agent and ask it: *"Install the IdeaForge Skill Pack from Agensi."*
3. Check that it worked: *"List my IdeaForge skills."* You should see `observer` (Idea Maturity Detector), `debate` (Structured Idea Debate), and `baron` (Idea to PRD).

### Option B: Manual

Download the ZIP from the listing and unzip it. It contains one folder per skill:

```
ideaforge-skillpack/
├── observer/SKILL.md
├── debate/SKILL.md
├── baron/SKILL.md
└── README.md
```

Copy the three folders (`observer/`, `debate/`, `baron/`) into your agent's skills folder as they are. For Claude Code, that is `.claude/skills/` in your project or `~/.claude/skills/` for all projects, so you end up with `.claude/skills/observer/SKILL.md` and so on. Any agent that can read a Markdown instruction file also works: attach the `SKILL.md` to the chat and say "follow this skill".

---

## Step-by-step usage

```
Your AI conversation
      │  paste
      ▼
[1] Idea Maturity Detector ──► MATURITY REPORT
      │  paste the block
      ▼
[2] Structured Idea Debate ──► DEBATE SUMMARY
      │  paste the block
      ▼
[3] Idea to PRD ──► PRD ──(approve)──► CLAUDE.md ──(approve)──► PROMPT CHAIN
```

### Step 1: Idea Maturity Detector

**You say:** "Is this idea mature?" and paste the conversation.

**Example input:**
```
User: I'm thinking of an app that tracks invoices for freelancers.
AI: Interesting. What problem does it solve?
User: Automatic reminders to clients who pay late. What if they went out on WhatsApp?
AI: Possible. To summarize: a tool that reminds clients about overdue invoices via WhatsApp.
User: Exactly. How do we build it? Can we use Stripe for payments?
```

**Example output:**
```text
=== IDEAFORGE · MATURITY REPORT · v1 ===
idea_title: Freelancer Invoice Reminder
idea_summary: A tool for freelancers that tracks invoices and automatically reminds late-paying clients via WhatsApp. Payments may go through Stripe. Pricing and platform are undecided.
score: 1.0
triggered_by: hard_trigger
hard_trigger_matches: "Stripe" (msg #5, stack)
signals:
  lexical_saturation: false
  question_type_shift: true
  feasibility_query: true
  summary_confirmation: true
...
verdict: MATURE
next_step: Paste this whole block into "Structured Idea Debate"
=== END MATURITY REPORT ===
```

**How it scores:** if a **hard trigger** is found (a stack name like Next.js, Stripe, or Supabase; a product name like "let's call it X"; a competitor reference like "Notion for Y") **in one of your own messages**, the score is 1.0. Hard triggers fire only on user messages, not assistant responses. Otherwise four **soft signals** are checked and the score is *true signals ÷ 4*. A score of **0.7 or higher** (3 of 4 signals) means `MATURE`.

If the verdict is `NOT YET`, the report lists which signals are missing and what to ask next. Keep chatting, then run it again.

### Step 2: Structured Idea Debate

**You say:** "Debate this idea" and paste the MATURITY REPORT block. You can add a line or two about the idea in your own words.

**What happens:**
- **Round 1:** Advocate gives its 3 strongest arguments, then Devil gives its 3 most critical weaknesses.
- **Round 2:** each side rebuts the other.
- **Round 3:** only runs if Round 2 produced genuinely new arguments.

The two sides never compromise. The point is to surface every angle, not to agree.

**Example output:**
```text
=== IDEAFORGE · DEBATE SUMMARY · v1 ===
idea_title: Freelancer Invoice Reminder
idea_summary: A tool for freelancers that tracks invoices and automatically reminds late-paying clients via WhatsApp. ...
maturity_score: 1.0
rounds_completed: 2
termination: argument_saturation
strongest_arguments:
  - A1: Late payment is a frequent, painful problem; automation removes the social cost of chasing clients.
  - A2: WhatsApp has far higher open rates than email in the target market.
  - A3: A narrow MVP can be built quickly on Stripe's invoicing APIs.
key_risks:
  - D1 [high]: WhatsApp Business API template rules and per-message fees may break the economics.
  - D2 [medium]: Existing invoicing tools already send reminders; the differentiator may be thin.
  - D3 [medium]: Freelancers may fear automated messages will hurt client relationships.
open_questions:
  - What does a WhatsApp template message cost per reminder in the target market?
  - Will freelancers pay a monthly fee for reminders alone?
  - Do clients respond faster to WhatsApp than to email?
next_step: Paste this whole block into "Idea to PRD"
=== END DEBATE SUMMARY ===
```

If you paste a `NOT YET` report, the debate does not start. You get the list of missing signals instead.

### Step 3: Idea to PRD

**You say:** "Turn this into a PRD" and paste the DEBATE SUMMARY block.

Baron works in **three stages** and waits for your approval after each one. Reply `ok` to continue, or say what to change.

**Stage 1: PRD** (example excerpt)
```text
=== IDEAFORGE · PRD · v1 ===
idea_title: Freelancer Invoice Reminder
problem_statement: Freelancers lose time and income chasing late payments, and chasing clients manually feels awkward. ...
core_features:
  - [must] Invoice list: add invoices with client contact and due date
  - [must] Scheduled reminders: send a reminder when an invoice becomes overdue
  - [experiment] WhatsApp vs email A/B: measure which channel gets paid faster
...
unknowns:
  - What does a WhatsApp template message cost per reminder?
=== END PRD ===
```

**Stage 2: CLAUDE.md.** A project constitution with Architecture Decisions, Naming Conventions, What NOT To Do, Testing Strategy, and Open Unknowns. Save it as `CLAUDE.md` in your repo root.

**Stage 3: Prompt chain.** Five prompts, each with six required fields:

```text
=== IDEAFORGE · PROMPT CHAIN · v1 ===
- order: 1
  trigger: true
  input: Approved CLAUDE.md in repo root; empty repository.
  task: |
    Scaffold the project as defined in CLAUDE.md "Architecture Decisions"...
  success_criteria: The test command exits 0; .env.example exists; .env is gitignored.
  on_error: Fix config errors and retry up to 2 times; then stop and report the exact error.
...
=== END PROMPT CHAIN ===
```

Paste the prompts into your coding agent one at a time. Move on when `success_criteria` pass. At every `trigger: false`, review the result yourself before continuing.

---

## Language support: Turkish and English

All three skills work in **Turkish and English**, including conversations that mix the two. Maturity signals are detected with keyword sets for both languages (for example *"ya şöyle olsa" → "nasıl yaparız"* and *"what if" → "how do we"*). Explanations and field values follow your language. Block markers and keys (`score`, `verdict`, `MATURE`, …) always stay in English so the blocks paste cleanly between skills, whatever language you use.

> **Tüm skill'ler Türkçe ve İngilizce çalışır.** Sohbetiniz Türkçe, İngilizce ya da karışık olabilir. Açıklamalar sizin dilinizde yazılır. Blokların anahtarları İngilizce kalır, böylece bir skill'den diğerine sorunsuz yapıştırılır.

---

## Tips

- **Paste the whole block**, including the `===` marker lines. The next skill uses them to find the data.
- **You can edit a block before pasting it.** If the summary got something wrong, fix it. The next skill trusts your edits.
- **Skipping steps:** Idea to PRD works best with a debate behind it. If you skip the debate, it records "risks are unvalidated" under unknowns.
- **Short conversations** (under 4 messages) rarely score as mature without a hard trigger. That is by design.

---

## Files

| File | Skill | Input | Output block |
|---|---|---|---|
| `observer/SKILL.md` | Idea Maturity Detector | AI conversation | `MATURITY REPORT` |
| `debate/SKILL.md` | Structured Idea Debate | `MATURITY REPORT` (+ optional summary) | `DEBATE SUMMARY` |
| `baron/SKILL.md` | Idea to PRD | `DEBATE SUMMARY` | `PRD` → `CLAUDE.md` → `PROMPT CHAIN` |

# Agensi Listing Copy: IdeaForge Skill Pack

> Reference file for the listing pages. **Not included in the ZIP.**

---

## 1. Idea Maturity Detector (`observer.md`)

**Short description (card):**
Paste your AI brainstorm and find out in seconds whether the idea is ready to build, with a score, four signals, and what to ask next.

**Long description (listing page):**

You've spent an hour brainstorming with an AI and the idea feels sharper. But is it actually *ready*, or are you still circling? Idea Maturity Detector reads the whole conversation and answers that question with evidence instead of gut feeling. It's the first step of the IdeaForge pipeline, and it works with any chat export: Claude, ChatGPT, Gemini, or plain copy-paste.

The skill checks two layers. **Hard triggers** fire as soon as a conversation shows real commitment: a concrete stack (Next.js, Django, FastAPI, Postgres, Supabase, Stripe…), a product name ("let's call it…"), or a competitor reference ("like Notion, but for…"). A hard trigger means score 1.0. Without one, it evaluates four **soft signals**: whether new vocabulary has stopped appearing (lexical saturation), whether questions shifted from "what if" to "how do we", whether cost, timeline, or technical feasibility came up, and whether you confirmed the AI's summary of the idea without correcting it.

You get a clean **MATURITY REPORT** with the score, every signal with a one-line piece of evidence, and a verdict: `MATURE` (≥ 0.7) or `NOT YET`. When it's not ready, the report tells you exactly which signals are missing and what to ask in your conversation to get there. No vague advice.

Works in Turkish and English, including mixed conversations. No API keys, no servers. Your own agent reads the skill and runs it. Paste the report into **Structured Idea Debate** to continue.

---

## 2. Structured Idea Debate (`debate.md`)

**Short description (card):**
An Advocate and a Devil's Advocate argue your idea for 2–3 rounds until no new arguments appear, then hand you the strongest arguments, real risks, and open questions.

**Long description (listing page):**

AI assistants are agreeable. Ask one about your idea and you'll mostly hear why it's great. Structured Idea Debate fixes that by making your agent argue with itself: an **Advocate** builds the strongest possible case for the idea, and a **Devil** goes after its most critical weaknesses: demand, competition, cost, technical risk, distribution, and hidden assumptions.

The debate has a strict structure. In Round 1, each side opens with its three best points. In Round 2, each side rebuts the other point by point. Then a **saturation check** runs: if Round 2 produced no genuinely new arguments, the debate stops. If it did, a final Round 3 addresses only the new points. The two sides are explicitly forbidden from compromising. The goal isn't consensus, it's to expose every angle before you invest time and money.

The output is a compact **DEBATE SUMMARY**: the arguments that survived rebuttal, the risks nobody could answer (ranked high, medium, or low), and the open questions you need to research, test with users, or prototype. The skill refuses to invent statistics. Anything that depends on unknown data becomes an open question instead of a fake fact.

It takes the MATURITY REPORT from **Idea Maturity Detector** as input and won't debate an idea that isn't ready. Instead it tells you which maturity signals are missing. Works in Turkish and English. Paste the summary into **Idea to PRD** for the final step.

---

## 3. Idea to PRD (`baron.md`)

**Short description (card):**
Turn a debated idea into a PRD, a CLAUDE.md project constitution, and a 5-step prompt chain ready to paste into Claude Code, Cursor, or Copilot.

**Long description (listing page):**

This is where an idea becomes a build plan. Idea to PRD takes the DEBATE SUMMARY from Structured Idea Debate and produces three artifacts, one stage at a time, with your approval between each. Nothing moves forward until you say "ok", and you can request changes at any stage.

**Stage 1: PRD.** Problem statement, target users, core features (tagged must / should / experiment), constraints, measurable success metrics, a mitigation for each high-severity risk from the debate, and a list of unknowns that is never empty. **Stage 2: CLAUDE.md.** A project constitution your coding agent reads before every task: architecture decisions with the reasoning behind them, naming conventions, a "What NOT To Do" list, a testing strategy, and the open unknowns it must not guess. **Stage 3: Prompt chain.** Five ordered prompts that build the MVP: scaffold, core logic, primary flow, integrations, and hardening. Each prompt has six required fields: order, trigger, input, task, success criteria, and on-error behavior.

The skill follows a strict no-invention rule. Every decision traces back to the debate or to what you said. When a choice has to be made without input, it's clearly labelled as a changeable default, and everything else goes into unknowns. Before it shows you the prompt chain, it validates that every prompt has all six fields, that every must-have feature is covered, and that at least one step pauses for human review.

The result drops straight into a repo: save the CLAUDE.md, paste prompt 1 into your coding agent, and move to the next prompt when its success criteria pass. Works in Turkish and English, with any agent that reads Markdown instructions.

---

## Pack description: IdeaForge Skill Pack (all three together)

**Short description (card):**
From AI brainstorm to buildable plan in three steps: maturity check, structured debate, then PRD + CLAUDE.md + prompt chain.

**Long description (listing page):**

IdeaForge is a three-skill pipeline for the moment a brainstorm turns into "should I actually build this?" It takes you from a raw AI conversation to a repo-ready plan, with a checkpoint at every stage, so you don't build on an idea that isn't ready or that nobody has challenged.

**1 · Idea Maturity Detector** reads your conversation and scores whether the idea has matured, using hard triggers (stack names, product naming, competitor references) and four soft signals. **2 · Structured Idea Debate** runs an Advocate-versus-Devil debate that stops when no new arguments appear, and extracts the strongest arguments, key risks, and open questions. **3 · Idea to PRD** turns the debate into a PRD, a CLAUDE.md, and a five-step prompt chain for your coding agent, with your approval between each stage.

The skills hand off through copyable text blocks. Each one ends with a clearly marked block (`MATURITY REPORT`, `DEBATE SUMMARY`, `PRD`, `CLAUDE.md`, `PROMPT CHAIN`), and you paste it into the next skill. There are no servers, API keys, or hidden state. You can read, edit, or stop between any two steps, and each skill also works on its own. The output formats are strictly defined, so the handoff works the same way in Claude, Cursor, Copilot, or any agent that reads Markdown instructions.

Built for indie hackers, product-minded developers, and students who brainstorm with AI and want to leave the chat with something they can build. Works in Turkish and English, including mixed conversations.

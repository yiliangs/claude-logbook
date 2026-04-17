# claude-memento

> **Claude Code forgets. This remembers.**

Every session, Claude starts blank. You re-explain the project, re-paste
the goal, re-walk dead ends you already ruled out yesterday. claude-memento
is a small archive that grows beside your work — written by hooks and a
small LLM. It recaps each session into a structured record and hands the
recap back to Claude when you start the next one, so it picks up where
you left off.

Two pieces, plus a bonus:

- a **chain** — `transcript → long log → project card` — that turns each
  session into a structured record, and the project into a living state
  document
- an **`/audit`** slash command that lets you query the whole archive in
  plain English
- a **short log** — turn-by-turn JSONL index — for high-cardinality
  queries; optional

> **Status:** v0.1 — public template. Sample data in `short_log/`,
> `long_logs/`, and `project_cards/myapp.yaml` shows what the pipeline
> produces; replace with your own once wired in.

## The chain

Every session leaves a structured trace.

```
┌────────────────────┐    ┌────────────────────┐    ┌────────────────────┐
│    transcript      │    │      long log      │    │    project card    │
│ per-session, raw   │    │ per-session, dense │    │ per-project, live  │
├────────────────────┤    ├────────────────────┤    ├────────────────────┤
│ prompts            │    │ arc                │    │ where you left off │
│ replies            │ ─> │ decisions          │ ─> │ what's next        │
│ tool calls         │    │ dead ends          │    │ what's at stake    │
│                    │    │ open questions     │    │ open threads       │
├────────────────────┤    ├────────────────────┤    ├────────────────────┤
│ written live       │    │ recapped by LLM    │    │ rewritten with     │
│ pruned after 30d   │    │ kept forever       │    │   each long log    │
│                    │    │                    │    │ auto-injected at   │
│                    │    │                    │    │   SessionStart     │
└────────────────────┘    └────────────────────┘    └────────────────────┘
   what happened              what mattered            where you are now
```

The **long log** is the differentiator — not a summary, but a structured
recap built around four named blocks:

- *arc* — what you set out to do vs. what actually happened, and where it pivoted
- *decisions* — choices made and their rationale, so they can be challenged later
- *dead ends* — what was tried and why it failed, so future-you doesn't walk them again
- *open questions* — what survived the session unresolved

Each one is a document meant to be re-read. Full schema:
[`schemas/long_log_schema.md`](schemas/long_log_schema.md).

The **project card** is what makes the chain close back on itself: it's
auto-injected as `additionalContext` at `SessionStart`, so the next
session begins with the previous one already loaded.

## The audit

A project-scoped `/audit` slash command ships with the repo. From inside
the memento clone, ask anything in plain English:

```
/audit when did I first work on the cache eviction bug
/audit what dead ends did I hit last week
/audit peak hours by weekday over the last month
/audit analyze my workflow weakspots
```

The agent composes two primitives from `scripts/audit.py` (`filter` and
`aggregate` over `short_log`) with `Read` over long logs and project
cards, then synthesizes a cited answer — pointing at the session, the
decision, and the file.

## The short log (optional)

A monthly JSONL index at `short_log/YYYY-MM.jsonl`. Each turn appends
one line with timestamps, project, machine, and a small LLM-filled
`question_summary` + `response_core`. Useful for high-cardinality queries
("how many times did I touch the auth module last month?"). The chain
works without it.

## Pipeline at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│ Per-turn (real-time, hook-driven)                                    │
│                                                                      │
│  UserPromptSubmit (hook.py)                                          │
│   ├─ append user prompt → transcripts/<session_id>.md                │
│   └─ append placeholder → short_log/YYYY-MM.jsonl                    │
│                                                                      │
│  Stop (hook_stop.py)                                                 │
│   ├─ append assistant text + tool trace → transcripts/<id>.md        │
│   ├─ extract artifacts from tool_use blocks                          │
│   └─ call small LLM → fill question_summary + response_core          │
│      → patch most recent short_log entry for this session            │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│ Scheduled (cron / Task Scheduler / Claude Schedule)                  │
│                                                                      │
│  synthesizer.py                                                      │
│   ├─ git pull --ff-only                                              │
│   ├─ scan transcripts/ for files w/o long_logs/<id>.yaml             │
│   ├─ for each unsynthesized session:                                 │
│   │    ├─ Call #1 → long_log YAML                                    │
│   │    ├─ Call #2 → updated project_card YAML                        │
│   │    ├─ sanitize_card: scrub maintenance artifacts, drop stale     │
│   │    │                  threads, flag orphan ids + budget overruns │
│   │    └─ git add + commit + push (per-session, crash-resilient)     │
│   └─ prune pass: delete transcripts older than N days (default 30)   │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│ Session start (real-time, hook-driven)                               │
│                                                                      │
│  SessionStart (session_start.py)                                     │
│   ├─ resolve project from cwd (git repo basename, override-aware)    │
│   └─ inject up to 3 YAML blocks as additionalContext:                │
│      1. project_cards/global.yaml (cross-project, optional)          │
│      2. project_cards/<parent_project>.yaml (if declared)            │
│      3. project_cards/<project>.yaml                                 │
└──────────────────────────────────────────────────────────────────────┘
```

## Setup

### 1. Clone

```bash
git clone https://github.com/<you>/claude-memento.git ~/src/claude-memento
cd ~/src/claude-memento
```

The directory **is** the storage backend — pick a path you'll keep
around. Multi-machine? Push it to your own private repo and clone it on
each machine.

### 2. Pick LLM providers

Two endpoint sets, both OpenAI-compatible (`/chat/completions`):

| Env var prefix | Purpose | What to pick |
|---|---|---|
| `ACTIVITY_LOG_*` | Per-turn AI fields (small, frequent) | Anything cheap and fast — Haiku, GPT-4o-mini, a self-hosted small model |
| `ACTIVITY_LOG_SYNTH_*` | Long-log synthesis (large, infrequent) | A model with a big context window — Kimi K2, Claude Sonnet, GPT-4.1 |

If `_SYNTH_*` is unset, the synthesizer falls back to the small set.

```bash
export ACTIVITY_LOG_API_BASE="https://api.example.com/v1"
export ACTIVITY_LOG_API_KEY="..."
export ACTIVITY_LOG_MODEL="<small-fast-model-id>"

export ACTIVITY_LOG_SYNTH_API_BASE="https://api.example.com/v1"
export ACTIVITY_LOG_SYNTH_API_KEY="..."
export ACTIVITY_LOG_SYNTH_MODEL="<large-synthesis-model-id>"
```

### 3. Wire the hooks

Merge the relevant block from [`docs/settings.example.json`](docs/settings.example.json)
into `~/.claude/settings.json` (or a project-local `.claude/settings.json`).
Replace `<REPO>` with the absolute path to your clone.

| Hook | Script | What it does |
|---|---|---|
| `UserPromptSubmit` | `scripts/hook.py` | Append prompt to transcript; write placeholder short_log entry |
| `Stop` | `scripts/hook_stop.py` | Append assistant reply; fill AI fields on the short_log entry |
| `Stop` (optional) | `git add/commit/push` | Auto-sync the recap across machines |
| `SessionStart` | `scripts/session_start.py` | Inject project card(s) as additional context |

### 4. Schedule synthesis

`scripts/synthesizer.py` is idempotent — it scans for transcripts
without a corresponding long log, processes them, and prunes raw
transcripts past the retention window. Wire it however suits you:

**Linux / macOS — cron:**
```cron
30 3 * * * cd /home/you/src/claude-memento && /usr/bin/python3 scripts/synthesizer.py >> last_run.log 2>&1
```

**Windows — Task Scheduler:** point a daily trigger at `run_synth.bat`.
It cd's to the repo root and invokes `scripts/synthesizer.py`,
inheriting your user environment vars.

**Cloud — [Claude Schedule](https://docs.claude.com/en/docs/claude-code/schedule)**
(or any remote cron): runs even when your machine is off. The
synthesizer pulls before reading and pushes after writing, so
multi-trigger conflicts resolve via fast-forward.

## Layout

```
claude-memento/
├── scripts/
│   ├── _api.py                     # OpenAI-compatible API helper
│   ├── hook.py                     # UserPromptSubmit
│   ├── hook_stop.py                # Stop
│   ├── session_start.py            # SessionStart (project_card injection)
│   ├── synthesizer.py              # scheduled synthesis
│   └── audit.py                    # retrieval primitives (filter + aggregate)
├── run_synth.bat                   # Windows Task Scheduler wrapper
├── .claude/commands/audit.md       # /audit slash command
├── docs/settings.example.json      # hook + env wiring (sanitized)
├── schemas/
│   ├── short_log_schema.md
│   ├── long_log_schema.md
│   ├── project_card_schema.md
│   ├── tone.md                     # prose tone (loaded with prose-producing calls)
│   └── output_rules.md             # YAML / null / identifier rules
├── short_log/YYYY-MM.jsonl
├── long_logs/<session_id>.yaml
├── project_cards/
│   ├── global.yaml                 # skeleton — fill or delete
│   └── <project>.yaml
└── transcripts/                    # ephemeral; pruned after synthesis
```

## Schemas

The five documents in `schemas/` are the source of truth for what each
layer holds. They're loaded into the synthesis prompt — edit them to
change behavior.

- [`short_log_schema.md`](schemas/short_log_schema.md) — per-turn entry shape
- [`long_log_schema.md`](schemas/long_log_schema.md) — per-session synthesis shape, including the `arc` / `decisions` / `dead_ends` / `open_questions` blocks
- [`project_card_schema.md`](schemas/project_card_schema.md) — per-project card shape, update behavior, sanitize rules
- [`tone.md`](schemas/tone.md) — prose style (loaded with prose-producing calls)
- [`output_rules.md`](schemas/output_rules.md) — YAML / null / identifier rules (loaded with every call)

`tone.md` and `output_rules.md` are **provider-agnostic** — swap the
underlying LLM without rewriting them.

## Design notes

**Project slug resolution.** Both `hook.py` and `session_start.py`
resolve the project name via the basename of the current git repo root.
Override per-repo by adding `project: <slug>` to
`<repo>/.claude/project.yaml` — useful for multi-repo projects
(`myapp-api` + `myapp-web` both feeding one `myapp` card).

**Stays out of your way.** Every hook is fail-open: missing API key,
network error, parse failure → exit 0; the user-facing turn never
breaks. Every write is idempotent — keyed by `session_id` or appended;
reruns are safe. Raw transcripts are pruned after the retention window
once a long log exists. Skips `/remind` and any prefix you add, so
meta-queries don't pollute the recap.

**What this is NOT.** Not a replacement for `git log`, shell history,
or Claude Code's raw `~/.claude/projects/.../*.jsonl` transcripts — it
sits on top of them. Not loaded into Claude's context automatically,
except project cards (which are, by design, at `SessionStart`). Not
opinionated about which provider you use — anything that speaks
OpenAI-compatible `/chat/completions` works.

## License

MIT.

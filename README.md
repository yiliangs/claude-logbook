# claude-logbook

A three-layer activity log + per-project state for [Claude Code](https://claude.com/claude-code), written by hooks per turn and synthesized in the background by any OpenAI-compatible LLM.

> **Status:** v0.1 — public template. Sample data in `short_log/`, `long_logs/`, and `project_cards/myapp.yaml` shows what the pipeline produces; replace with your own once wired in.

## Why

Claude Code already keeps raw transcripts at `~/.claude/projects/.../*.jsonl`. They are exhaustive but they are **not queryable**, **not synthesized**, and **not loaded back into future sessions**. This template fills that gap with three thin layers:

| Layer | Granularity | Purpose | Where |
|---|---|---|---|
| **Short log** | Per-turn | Queryable index for retrieval / search | `short_log/YYYY-MM.jsonl` |
| **Long log** | Per-session | Synthesized narrative + decisions + dead ends | `long_logs/<session_id>.yaml` |
| **Project card** | Per-project | Living state snapshot, auto-injected at SessionStart | `project_cards/<project>.yaml` |

Each layer has a different cadence, lifetime, and consumer. Synthesis flows upward: short log → raw transcript → long log + project card.

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

## Layout

```
claude-logbook/
├── scripts/
│   ├── _api.py                     # OpenAI-compatible API helper
│   ├── hook.py                     # UserPromptSubmit
│   ├── hook_stop.py                # Stop
│   ├── session_start.py            # SessionStart (project_card injection)
│   ├── synthesizer.py              # Scheduled synthesis
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
├── short_log/YYYY-MM.jsonl         # sample inside
├── long_logs/<session_id>.yaml     # sample inside
├── project_cards/
│   ├── global.yaml                 # skeleton — fill or delete
│   └── <project>.yaml              # sample inside (myapp.yaml)
└── transcripts/                    # ephemeral; pruned after synthesis
```

## Setup

### 1. Clone

```bash
git clone https://github.com/<you>/claude-logbook.git ~/src/claude-logbook
cd ~/src/claude-logbook
```

The directory **is** the storage backend — pick a path you'll keep around. Multi-machine? Push it to your own private repo, then clone it on each machine.

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

Merge the relevant block from [`docs/settings.example.json`](docs/settings.example.json) into `~/.claude/settings.json` (or a project-local `.claude/settings.json`). Replace `<REPO>` with the absolute path to your clone.

The four wires:

| Hook | Script | What it does |
|---|---|---|
| `UserPromptSubmit` | `scripts/hook.py` | Append prompt to transcript; write placeholder short_log entry |
| `Stop` | `scripts/hook_stop.py` | Append assistant reply; fill AI fields on the short_log entry |
| `Stop` (optional) | `git add/commit/push` | Auto-sync the log across machines |
| `SessionStart` | `scripts/session_start.py` | Inject project card(s) as additional context |

### 4. Schedule synthesis

`scripts/synthesizer.py` is idempotent: it scans for transcripts without a corresponding long log, processes them, and prunes raw transcripts past the retention window. Wire it however suits you:

**Linux / macOS — cron:**
```cron
30 3 * * * cd /home/you/src/claude-logbook && /usr/bin/python3 scripts/synthesizer.py >> last_run.log 2>&1
```

**Windows — Task Scheduler:** point a daily trigger at `run_synth.bat`. It cd's to the repo root and invokes `scripts/synthesizer.py`, inheriting your user environment vars.

**Cloud — [Claude Schedule](https://docs.claude.com/en/docs/claude-code/schedule)** (or any remote cron): runs even when your machine is off. The synthesizer pulls before reading and pushes after writing, so multi-trigger conflicts resolve via fast-forward.

### 5. (Optional) `/audit` slash command

The repo ships with a project-scoped slash command at `.claude/commands/audit.md`. From inside the activity-log clone, ask any natural-language question:

```
/audit what did I do last week
/audit when did I first work on the cache eviction bug
/audit peak hours by weekday over the last month
/audit analyze my workflow weakspots
```

The agent composes two primitives from `audit.py` (`filter` and `aggregate` over `short_log`) with `Read` over long_logs / project_cards, then synthesizes a cited answer.

## How it stays out of your way

- **Fail-open everywhere** — missing API key, network error, parse failure → hook exits 0; the user-facing turn never breaks.
- **Idempotent** — every write is keyed by `session_id` or appends; reruns are safe.
- **Pruned automatically** — raw transcripts deleted after the retention window (default 30 days) once a long log exists.
- **Skips `/remind`** (and any prompt prefix you add) so meta-queries don't pollute the log.

## Schemas

The four documents in `schemas/` are the source of truth for what each layer holds. They are loaded into the synthesis prompt — edit them to change behavior.

- [`schemas/short_log_schema.md`](schemas/short_log_schema.md) — per-turn entry shape
- [`schemas/long_log_schema.md`](schemas/long_log_schema.md) — per-session synthesis shape
- [`schemas/project_card_schema.md`](schemas/project_card_schema.md) — per-project card shape, update behavior, sanitize rules
- [`schemas/tone.md`](schemas/tone.md) — prose style (loaded with prose-producing calls)
- [`schemas/output_rules.md`](schemas/output_rules.md) — YAML / null / identifier rules (loaded with every call)

`tone.md` and `output_rules.md` are **provider-agnostic** — swap the underlying LLM without rewriting them.

## Project slug resolution

Both `hook.py` and `session_start.py` resolve the project name via:

1. **Default**: `os.path.basename(git_repo_root)` of the current working directory.
2. **Override**: if `<repo>/.claude/project.yaml` contains `project: <slug>`, use that slug.

Multi-repo projects (e.g. `myapp-api` + `myapp-web` both wanting to feed `myapp`) use the override to share one card.

## What this is NOT

- Not a replacement for `git log`, shell history, or Claude Code's raw `~/.claude/projects/.../*.jsonl` transcripts. It sits **on top** of them.
- Not loaded into Claude's context automatically — except project cards, which **are** injected at `SessionStart` by design.
- Not opinionated about which provider you use. Two env-var sets, OpenAI-compatible, anything that speaks `/chat/completions` works.

## License

MIT.

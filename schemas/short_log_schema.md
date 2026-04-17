# Short Log Schema

**Granularity:** per-turn (one entry per Q-A exchange)
**Populated by:** hook script (mechanical fields) + hook model (AI-generated fields)
**Written at:** background thread after each message turn completes
**Storage:** appended to session-scoped file, pushed to cloud
**Consumed by:** intra-session retrieval, cross-session search, long log synthesis input

## Design Principles

Each entry is self-contained and queryable without joins. Session metadata is repeated per entry — this is intentional. The duplication is mechanical (script-copied strings, zero AI cost) and buys independence: entries can be queried across sessions, moved between backends, and debugged in isolation without requiring a session header to be present.

AI-generated fields are minimized to two: `question_summary` and `response_core`. These are the only fields that consume model tokens per turn. Everything else is either copied by script or trivially derived.

## Schema

```yaml
# --- Mechanical fields (hook script, no AI) ---
session_id: <uuid — same for all entries in this session>
turn: <integer — position in session, 1-indexed, indexes into raw transcript>
timestamp: <ISO 8601 — when this turn completed>
machine: <hostname>
project: <project name>
git_branch: <branch name>
thread_id: <optional — links to ongoing problem across sessions>

# --- AI-generated fields (hook model) ---
question_summary: <what was asked — one sentence>
response_core: <key insight, decision, or solution — one to two sentences>

# --- Mechanical or trivially derived ---
artifacts: [<file paths created, modified, or deleted during this turn>]
status: <resolved | unresolved | blocked>
```

## Field Notes

**`turn`** — Position index into the raw transcript. When search finds a relevant short log entry, `turn` locates it in the source file without text-matching. Mechanical, zero cost.

**`timestamp`** — Per-entry, not per-session. The temporal retrieval axis for "what was I doing at 3pm" or "what happened in the last hour." Session-level timestamps live in the long log.

**`thread_id`** — Optional. Links this specific exchange to an ongoing problem thread that spans sessions. This is the per-turn field with the highest cross-session retrieval value. Assigned manually or inferred by the hook model only when a thread is clearly active.

**`question_summary`** and **`response_core`** — The only AI-generated fields. These are the search surfaces. Together they must be sufficient to determine relevance without reading the raw transcript. The hook model should compress, not editorialize — declarative statements, not narrative.

**`artifacts`** — File paths touched during this turn. Populated mechanically from tool use metadata in the transcript (file creation, modification, deletion events). Enables "which turn touched this file" queries.

**`status`** — Completion state only, not content type. `resolved`: the question was answered or the task was completed. `unresolved`: the exchange ended without resolution (continued in next turn or abandoned). `blocked`: progress stopped due to an external dependency or constraint. Content type (decision, knowledge, debugging, exploration) is not tracked here — `response_core` carries that signal implicitly and the long log classifies it at session granularity.

## What This Schema Does NOT Own

**Domain/topic classification** — owned by the long log at session granularity. Per-turn classification is either redundant (same domain all session) or noisy (borderline turns get inconsistent labels). Search against `question_summary` and `response_core` instead.

**Follow-up actions** — owned by the long log. Per-turn follow-ups are speculative; the hook model cannot know whether the next turn resolves the issue. The long log has the full session arc.

**Dead ends, decisions, narrative synthesis** — all long log. The short log is a retrieval index, not an analysis layer.

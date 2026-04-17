---
description: Query and synthesize over the activity log (short_log, long_logs, project_cards). Chain queries, propose write-backs.
---

# /audit

Answer whatever the user asks about their activity log. Compose primitives; don't follow a fixed recipe.

**User query:** $ARGUMENTS

## Log architecture (what lives where)

| Layer | Granularity | Where | What's in it |
|---|---|---|---|
| Short log | per-turn | `short_log/YYYY-MM.jsonl` | session_id, turn, timestamp, machine, project, git_branch, thread_id, question_summary, response_core, artifacts, status |
| Long log | per-session | `long_logs/<session_id>.yaml` | arc (opening_intent, actual_trajectory, closure_status), exchanges (inflection points), decisions, dead_ends, artifacts, synthesis (narrative, open_questions, follow_up_actions), synthesis_meta (session_quality) |
| Project card | per-project | `project_cards/<project>.yaml` (+ `global.yaml`) | current_focus, active_threads, constraints, dead_ends, invariants, artifacts_in_flight |
| Transcript | per-session raw | `transcripts/<session_id>.md` — **pruned after 30 days** | full chat history |

**Which layer answers what:**
- time-range narrative → long_logs (narratives + decisions) in the window
- keyword → temporal: short_log filter, then long_log read for depth
- stats over time/weekday/project → short_log via `audit.py aggregate`
- qualitative synthesis / "workflow weakspots" / patterns → long_logs (dead_ends, closure_status, session_quality, orphaned follow_up_actions)
- current state of a project → project_card
- full word-for-word recall → transcript (only if < 30 days old)

## Primitives: `audit.py`

Both subcommands read `short_log/*.jsonl`. For long_logs and project_cards, use the Read tool directly.

**Filter** — mechanical filter over structured fields + keywords:
```bash
python audit.py filter --since "7 days ago" --project myapp --format table
python audit.py filter --keyword cache --keyword eviction --keyword-mode any --has-ai yes
python audit.py filter --status unresolved --format session-ids   # then Read each long_log
python audit.py filter --session-id <uuid> --format jsonl
```

Accepts: `--since`, `--until` (ISO or `N days ago` / `yesterday` / `today`), `--project`, `--branch`, `--status`, `--session-id`, `--keyword` (repeatable), `--keyword-mode any|all`, `--has-ai yes|no|any`, `--limit`, `--format jsonl|table|session-ids`.

**Aggregate** — group-by + metric:
```bash
python audit.py aggregate --group-by weekday --metric turns --format table
python audit.py aggregate --group-by weekday --metric session_duration_minutes_mean --since "30 days ago"
python audit.py aggregate --group-by project --metric status_ratio
python audit.py aggregate --group-by hour --metric turns --project myapp
```

`--group-by`: `weekday | hour | day | project | branch | status`
`--metric`: `turns | sessions | session_duration_minutes_mean | session_duration_minutes_sum | status_ratio`

## Retrieval workflow

1. **Plan before querying.** Read the user's question; identify which layers plausibly contain the answer; note what would count as "enough" to stop.
2. **Mechanical first.** Use `filter` / `aggregate` for anything structured (time, project, status, weekday). Cheap and deterministic.
3. **Semantic expansion only if mechanical misses.** Rewrite the user's phrasing into 3-5 keyword variants that cover synonyms + exact identifiers before re-running `filter --keyword`. Example: "the cache thing" → `cache`, `LRU`, `eviction`, `TTL`, `invalidate`.
4. **Widen to long_logs when depth is needed.** Short log alone can answer "when did this happen" — it cannot answer "why" or "what was decided." For decisions, dead_ends, or any "weakspot" analysis, Read matching long_logs.
5. **Cite everything.** Every claim ties to a `session_id + timestamp` (or long_log path). Never summarize without traceable references.

## Chain-query budget

- Budget: **up to 5 rounds** of tool use per audit, where a round = one `Read` of a long_log OR one `audit.py` subprocess call. Cheap filters (< 50 entries returned) don't count. Aggregates don't count.
- **Stop rule:** stop when the next query wouldn't change what you'd report.
- **Log why you stopped** at the end of your report: "stopping — next round wouldn't shift the finding," or "ran out of budget, partial answer follows."
- Shallow questions should land in 1-2 rounds; full diagnostic audits may use all 5.
- If the question genuinely exceeds the budget, report what you have and ask the user if they want to widen.

## Write-back (propose, never silent)

You may propose edits to:
- `project_cards/global.yaml` — promote cross-project patterns (e.g. a dead_end that recurred in sessions X, Y, Z)
- `project_cards/<project>.yaml` — add threads the synthesizer missed, promote invariants
- `long_logs/<session_id>.yaml` — backfill `thread_ids` for cross-session threads; add cross-references

Out of scope: `short_log/` entries (append-only ground truth), `transcripts/` (raw).

**Protocol:**
1. Draft the edit as a unified diff or a "before/after" block.
2. State the rationale: which session_ids + timestamps support this change.
3. Show it in chat, ask the user to confirm, redirect, or skip.
4. Only after confirmation, apply via Edit.
5. Never chain multiple writes silently — each write gets its own confirmation.

## Output style

- Lead with the answer. Detail follows.
- Tables for anything with ≥ 3 structured comparables.
- Cite with `<session_id[:8]> @ <timestamp>` inline, e.g. "pivoted to cloud scheduling (`b565579d @ 2026-04-16T13:42`)."
- Flag proxies explicitly: if "energy" means turns/hour, say so. Don't present proxies as measurements.
- End with: any write-back proposals, plus "stopping — [reason]" or "partial — [what's missing]".

## Anti-patterns

- Dumping raw `filter` output with no synthesis. The user wants the answer, not the data.
- Synthesizing from the short log alone when the question is about decisions, dead_ends, or patterns — those live in long_logs.
- Writing without proposing first.
- Silently treating null-AI short_log entries as if they contain information (they don't — read the transcript if needed, or acknowledge the gap).
- Extrapolating from one session. Patterns require ≥ 3 instances; flag N when reporting.
- Forgetting to cite. Every claim → traceable reference.

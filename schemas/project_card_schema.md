# Project Card Schema

**Granularity:** per-project — one file per coherent project-self
**Populated by:** Kimi synthesis routine, alongside the long log
**Updated:** every time a session for this project is synthesized
**Format:** YAML
**Storage:** `project_cards/<project>.yaml`, pushed to git
**Consumed by:** Claude Code at session start — auto-injected as `additionalContext` by the SessionStart hook

## Design Principles

### A card is a state snapshot, not a changelog

The card represents where the project stands RIGHT NOW. Synthesis merges new session findings into the existing card as a **superset** — accumulated state is preserved unless explicitly invalidated.

### One card per project-self, regardless of scope

Each card must stay intact for a single project-self / topic / problem. Scope does not determine split; **coherence of the project-self does**. A tiny project with two disjoint problems warrants two cards; a massive project with one cohesive thrust stays on one card.

When a project grows a second identity, split into sub-cards and link them with `parent_project`. Never let one card carry two project-selves.

### Stable IDs

Every appended item (`constraints`, `dead_ends`, `invariants`, `active_threads`) carries a stable id so future sessions can supersede or reference them without text-matching.

## Schema

```yaml
project: <project slug — matches short log "project" field; mechanically derived from cwd repo basename>
parent_project: <optional — slug of a parent card whose state this one inherits. null if standalone.>
created_at: <ISO 8601 — first synthesis that produced this card>
last_updated: <ISO 8601>
last_synthesizing_session: <session_id>
session_count: <integer — total sessions contributing to this card>

# --- What the project is doing now ---

current_focus: >
  One paragraph. The active thrust right now.
  REPLACED (not appended) when synthesis detects pivot — i.e. opening_intent
  has shifted across the most recent few sessions. Prior long logs are passed
  into the synthesis call so pivot can be judged across sessions, not from
  the current one alone.

active_threads:
  # Open work threads carried across sessions. Each thread_id is assigned by
  # the synthesizer at long-log time and propagated here.
  # An item leaves this list when explicitly resolved OR when stale
  # (see "Thread age-out" below).
  - id: <thread_id — synthesizer-assigned>
    statement: <one sentence — what is being worked on>
    blocking_on: <what is needed to advance, or null if in flight>
    last_active_session: <session_id>
    first_seen_session: <session_id>

# --- Accumulated state that binds future work ---

constraints:
  # Hard constraints established by past decisions. Bind future work.
  # Append new entries; replace existing entries by id when superseded.
  - id: <source_session_id>-c<N>     # e.g. "abc123-c1" — N = index within source session
    statement: <declarative — "X must use Y because Z">
    rationale: <one sentence>
    source_session: <session_id>
    superseded_constraints: [<id>, ...]    # ids of prior constraints this entry replaced
    superseded_at_session: <session_id | null>

dead_ends:
  # Approaches that failed. Recorded so they are not re-attempted.
  # Append-only. Each entry must specify mechanism of failure, not just "didn't work".
  - id: <source_session_id>-d<N>
    approach: <what was tried>
    failure_mode: <specific reason — "O(n²) on large grids", not "too slow">
    source_session: <session_id>

invariants:
  # Architectural truths that must remain true. Long-lived, rarely change.
  # Append-only; remove only if explicitly invalidated by a new session.
  - id: <source_session_id>-i<N>
    statement: <declarative invariant>
    source_session: <session_id>

# --- Work in flight ---

artifacts_in_flight:
  # Files / documents currently being shaped. Not yet stable.
  # Synthesizer adds new touched artifacts, updates `state` and `last_touched_session`,
  # and removes entries when work on them stabilizes (no activity in N sessions, or
  # a session explicitly marks the artifact as complete).
  #
  # EXCLUDE mechanical / config files that merely get touched in passing:
  #   - .gitignore, .gitattributes, CODEOWNERS, .editorconfig
  #   - README.md unless the session's primary work was documentation
  #   - lockfiles (package-lock.json, poetry.lock, etc.)
  # These are not "in flight" — they're just maintenance side-effects.
  # The synthesizer runs a mechanical scrub after Kimi output to enforce this.
  - path: <file path>
    state: <one line on what's WIP about it>
    last_touched_session: <session_id>

external_dependencies:
  # People, services, tools the project depends on.
  - name: <name or identifier>
    role: <relationship to project>

# --- Meta ---

card_quality: <high | medium | low — synthesizer's assessment of the card itself>
quality_reasoning: >
  One sentence on why. Low = card is sparse, contradicts itself, or cannot be
  meaningfully consumed by Claude Code as a project state snapshot.

quality_mechanical:
  # Written by the synthesizer's post-generation sanitize pass, not by Kimi.
  # Present only if any check flagged an issue.
  token_estimate: <integer — approx token count (len // 4)>
  token_cap_exceeded: <true | false>    # cap = 1500
  thread_count: <integer>
  thread_count_over_split_threshold: <true | false>    # threshold = 8, suggests split
  orphan_ids: [<id>, ...]               # superseded_constraints targets that don't exist
  stale_threads_dropped: [<thread_id>, ...]
  stale_artifacts_dropped: [<path>, ...]
  maintenance_artifacts_scrubbed: [<path>, ...]
```

## Update Behavior (per Kimi synthesis output)

| Field | Update behavior |
|---|---|
| `current_focus` | Replace if pivot detected across prior + current long logs; else preserve |
| `active_threads` | Add new; update `last_active_session` for touched; drop resolved or stale (see below) |
| `constraints` | Append new; replace existing by id if superseded (record old id in `superseded_constraints`) |
| `dead_ends` | Append-only — never remove |
| `invariants` | Append-only; remove only if explicitly invalidated this session |
| `artifacts_in_flight` | Add new touched; update state for revisited; remove when stabilized or scrubbed as maintenance |
| `external_dependencies` | Append; rarely changes |
| `parent_project` | Set once at card creation; changes only when card is explicitly re-parented |

## Thread Age-Out

Threads leave `active_threads` when:
1. Explicitly resolved or invalidated by a session (Kimi sets this based on the long log), OR
2. Stale: `last_active_session` is more than 5 sessions behind the current `session_count`

Rule 2 is enforced mechanically by the synthesizer's sanitize pass after Kimi output — dropped threads are recorded in `quality_mechanical.stale_threads_dropped`. History is preserved in long logs.

## Pivot Detection

`current_focus` is replaced only on genuine pivot, detected across the **most recent 3 sessions + the current one** — not from the current session alone. The synthesizer loads prior long logs and injects them as context for the card-update call so Kimi can judge whether a focus shift is a real pivot or a tangent.

A single tangent session is not a pivot. If unsure, preserve the existing `current_focus`.

## ID Generation

Stable IDs prevent fragile text-matching when items reference each other. Format:

`<source_session_id>-<prefix><N>`

| Field | Prefix |
|---|---|
| `constraints` | `c` |
| `dead_ends` | `d` |
| `invariants` | `i` |
| `active_threads` (id field) | `t` |

`N` is the 1-indexed position of new items added by that session, ordered by appearance in the synthesis output.

## Parent / Global Cards

**`parent_project`** — points to another card whose state this card inherits. SessionStart loads the parent card alongside the child; the child card does not duplicate constraints that live on the parent, it references parent ids. Use when one project grows into a family of sub-projects (e.g. `myapp-api`, `myapp-web` under `myapp`).

**`global.yaml`** — always loaded by SessionStart alongside the current project's card. Holds cross-project knowledge: infrastructure-level dead ends (e.g. "Moonshot blocks cloud egress IPs"), universal invariants, shared external dependencies. Human-maintained; the synthesizer does not auto-promote entries into it.

## Post-Generation Sanitize Pass

After Kimi writes the card, `synthesizer.py` runs `sanitize_card()`:

1. **Scrub maintenance artifacts** from `artifacts_in_flight`: `.gitignore`, `.gitattributes`, `CODEOWNERS`, `.editorconfig`, lockfiles, `README.md` unless the session's primary work was documentation. Scrubbed paths are logged in `quality_mechanical.maintenance_artifacts_scrubbed`.
2. **Drop stale threads** (last_active > 5 sessions ago). Logged in `stale_threads_dropped`.
3. **Drop stale artifacts** (last_touched > 5 sessions ago). Logged in `stale_artifacts_dropped`.
4. **Detect orphan IDs** — `superseded_constraints` entries pointing to ids that don't exist. Logged in `orphan_ids`.
5. **Check token budget** — token_estimate vs 1500 cap. Flag if exceeded.
6. **Check thread count** — > 8 suggests the card is accumulating too many concurrent threads; flag as a split candidate.

The sanitize pass writes results to `quality_mechanical`. If the pass cannot run (pyyaml unavailable), it is skipped silently and the card is written as Kimi produced it.

## Anti-patterns

**State loss.** The most common failure mode. Overwriting the card with only the current session's information, dropping accumulated state from prior sessions. The merge is a superset operation — preserve everything not explicitly invalidated. When in doubt, keep the existing entry.

**Narration.** Writing "this session resolved X" or "today I decided Y" in any card field. The card is a state snapshot, not a changelog. It represents where the project stands RIGHT NOW, not what happened today. Long log owns the what-happened narrative.

**Unjustified focus pivots.** `current_focus` is replaced only when pivot is visible across the prior 3 sessions + current one. A single session exploring a tangent is not a pivot. If unsure, preserve the existing `current_focus`.

**Duplicate constraints.** Before appending a new constraint, check if it restates an existing one (including inherited parent constraints). If yes, supersede the existing entry (record its id in `superseded_constraints`) instead of adding a parallel duplicate.

**Two project-selves on one card.** If `current_focus` cannot be written as one paragraph without stitching together unrelated thrusts, split the card. Do not let one card carry two identities.

## What This Schema Does NOT Own

**Per-turn detail** — owned by the short log. The card is consumed by Claude at session start, where token budget matters. Avoid bloat.

**Decisions / dead-ends with no future relevance** — synthesis routine should filter. A dead end that's no longer plausible (because the entire approach was abandoned) doesn't need to live in the card forever. Long log keeps it for history; card keeps only what binds future work.

**Recent activity narrative** — owned by the long log. Card is a state snapshot, not a what-happened-recently list.

## Consumption by Claude Code

The SessionStart hook (`session_start.py`) detects the current project from `cwd` (git repo basename, with optional override via `.claude/project.yaml` in that repo) and injects up to three YAML blocks in order:

1. `project_cards/global.yaml` if it exists
2. `project_cards/<parent_project>.yaml` if the current card declares a parent
3. `project_cards/<project>.yaml`

Each block gets a short header so Claude can tell them apart.

Token budget: target ≤ 1500 tokens per card. Budget is per-card — loading parent + global can bring the total to ~4500 tokens in the worst case.

## Project Slug Resolution

1. **Default**: `os.path.basename(git_repo_root)` of the `cwd` at session start
2. **Override**: if `<repo_root>/.claude/project.yaml` exists with a `project: <slug>` field, use that slug instead

This lets multi-repo projects (e.g. an app split across API + UI repos) share a single card, and lets a subtree within a repo declare its own card for sub-project splits.

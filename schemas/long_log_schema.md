# Long Log Schema

**Populated by:** hook metadata (session block) + Kimi synthesis (all other blocks)
**Format:** YAML
**Written by:** scheduled synthesis routine, decoupled from session lifecycle
**Input:** raw transcript file (named by session id) + hook-provided metadata
**Consumed by:** project_card update, human review, cross-session search

## Architecture Notes

Raw transcript is written per-turn by a hook script into a file named by session id. The file IS the session boundary — no runtime detection needed.

Synthesis is triggered by a scheduled routine that scans for unsynthesized sessions, NOT by SessionEnd or SessionStart. This makes synthesis crash-resilient and retriable: if synthesis fails, the routine retries on next run over the same raw file.

The project_card (per-project living document) is updated as a separate output alongside the long log — see project_card_schema for that structure.

## Schema

```yaml
session:
  id: <uuid — from raw transcript filename>
  started_at: <ISO 8601>
  ended_at: <ISO 8601>
  duration_minutes: <integer — computed by hook>
  machine: <hostname — from hook>
  project: <project name — from hook>
  git_branch: <branch name — from hook>
  working_directory: <absolute path — from hook>
  transcript_turns: <integer — total turns in raw transcript>

classification:
  domains:
    - <one of: backend | frontend | ml | infra | tooling | ops | docs | research | other>
    # list all that apply — long sessions span multiple
  primary_topic: <free text, specific — e.g. "RateLimiter token-bucket refill race condition">
  thread_ids:
    - <id of any ongoing thread this session touched — leave empty if new>
  is_continuation: <true | false>

arc:
  opening_intent: >
    <What the user came in to do — inferred from the first 3–5 turns.
    One sentence. Declarative. Do not editorialize.>

  actual_trajectory: >
    <What actually happened, especially if it diverged from opening_intent.
    One to two sentences. Note pivots, scope changes, or rabbit holes explicitly.>

  closure_status: <resolved | unresolved | partial | pivoted>
  closure_note: >
    <Only required if closure_status is not "resolved". One sentence explaining
    what remains open or why the session ended without full resolution.>

exchanges:
  # Log inflection points only — moments where something changed.
  # Do NOT log every turn. Iterative grinding toward a resolution = one entry at resolution.
  # Target: 3–8 entries per session. More than 10 is noise.
  #
  # SELF-VALIDATION RULE: each exchange must reference at least one of:
  #   - a decision (by statement)
  #   - a dead_end (by attempt)
  #   - an artifact (by path)
  # If an exchange cannot link to any of these, it is likely a topic shift,
  # not an inflection point. Omit it.
  - topic: <what was being addressed>
    type: <debugging | decision | exploration | design | writing | ops | refactor>
    outcome: <resolved | unresolved | deferred>
    linked_to: <decision | dead_end | artifact — which block this exchange produced>
    core_insight: >
      <The thing that actually moved the needle. One sentence.
      If nothing moved, this exchange does not belong here.>

decisions:
  # Only actual decisions — choices made that constrain future work.
  # "We discussed X" is not a decision. "X was chosen over Y" is.
  - statement: >
      <Declarative. "HashMap replaced with LRU cache for hot-path session lookup.">
    rationale: >
      <Why. One sentence minimum. If rationale is missing, the decision is not usable.>

dead_ends:
  # First-class field. Do not skip.
  # A dead end not recorded here will be attempted again in a future session.
  - attempt: >
      <What was tried.>
    reason_failed: >
      <Why it did not work. Be specific — "too slow" is not enough; "O(n²) on large grids" is.>

artifacts:
  # Only items that were actually created, modified, or deleted during the session.
  - path: <file path or document reference>
    nature: <created | modified | deleted | designed>
    description: <one-line note on what changed — optional but preferred>

synthesis:
  narrative: >
    <2–4 sentences. What happened and why it matters relative to the project.
    Written for a human reader reviewing the log later.
    Do not repeat fields already captured above — synthesize across them.>

  open_questions:
    - <Each item is a specific unresolved question, not a vague "look into X".>
    # e.g. "Does refill_tokens() handle clock skew when the worker resumes after sleep?"

  follow_up_actions:
    - <Each item is actionable cold — enough context that it can be picked up without re-reading the session.>
    # e.g. "Add fast-path guard to RateLimiter for unlimited-tier callers before merging to main."

synthesis_meta:
  model: <model identifier — e.g. "kimi-k2.5">
  synthesized_at: <ISO 8601>
  synthesis_duration_seconds: <integer>
  raw_transcript_file: <filename of the source raw transcript>
  session_quality: <high | medium | low>
  session_quality_reasoning: >
    <One sentence. Assessed from full transcript — not self-reported.
    "High" = decision made or artifact produced that materially advances the project.
    "Low" = exploratory or unresolved with no durable output.>
```

## Null Severity Tiers

Not all nulls are equal. Some indicate benign absence; others mean synthesis failed.

**Tier 1 — CRITICAL** (null here means the log is structurally degraded):

`arc.closure_status`, `arc.opening_intent`, `synthesis.narrative`, `synthesis_meta.session_quality`

If any Tier 1 field is null, the synthesis routine should flag the log for re-synthesis or manual review.

**Tier 2 — IMPORTANT** (null is acceptable but reduces log utility):

`arc.actual_trajectory`, `exchanges` (empty list), `decisions` (empty list), `synthesis.follow_up_actions`

**Tier 3 — BENIGN** (null expected in many sessions):

`classification.thread_ids`, `dead_ends` (empty list — session may genuinely have none), `arc.closure_note` (only needed when closure_status != resolved), `artifacts` (empty list — session may be purely conversational)

## Anti-patterns

**Retrospective intent.** Do not rewrite `opening_intent` to match what actually happened. The delta between `opening_intent` and `actual_trajectory` is the signal — preserve it. Infer `opening_intent` from only the first 3–5 turns; ignore later content when filling this field.

**Compressing dead ends into narrative.** Failed attempts belong in `dead_ends` as explicit entries with specific failure mechanisms. Do not fold them into `synthesis.narrative` — a dead end swallowed by prose will be re-attempted in a future session.

**Sequential summarization in `exchanges`.** Do not log every turn. Log inflection points — moments where something changed. If an exchange cannot populate `linked_to` with a `decision`, `dead_end`, or `artifact`, it is a topic shift, not an inflection point. Omit it.

**Missing rationale on decisions.** A decision without rationale cannot be challenged or acted on later. If rationale cannot be extracted from the transcript, move the item to `open_questions` instead.

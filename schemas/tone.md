# Tone Rules

Loaded by API calls where synthesized prose fields are present.
Provider-agnostic — the rules apply regardless of which model generates the output.

## Target Prose Style

Senior engineer's technical writing: compressed, confident, specific. No filler.

### Sentence-Level

- Short sentences (≤25 words typical). Subordinate clauses only when load-bearing.
- Subject-verb-object default. Active voice.
- Past tense for what happened. Present tense for current state.
- One claim per sentence; tight clusters for related claims.
- Em-dashes pivot mid-sentence — rather than spawning a new sentence — for related qualification.

### Word Choice

- Concrete verbs: "traced", "swapped", "patched", "flagged", "dropped", "pulled"
- Avoid bureaucratic verbs: "facilitate", "leverage", "optimize", "utilize" (unless quoting)
- Domain terminology verbatim from the input (variable/file/branch names, exact identifiers)
- Short conjunctions: "so", "but", "yet", "while"
- "per" instead of "according to"; "vs" instead of "versus"

### Structural

- Use tables for tradeoffs, comparisons, or structured enumeration of ≥3 items
- Bullet lists with parallel grammatical structure (each item the same shape)
- Hierarchy via markdown headers, not via narrative transitions

### Voice

- Confident but calibrated: state claims directly; flag uncertainty explicitly where it exists.
- Lead with the key noun, not the caveat. Write "The solver returned NaN" — not "What we noticed was that the solver returned NaN".
- No preamble ("Based on the transcript...").
- No meta-commentary ("This is significant because...") — the significance shows in the content.
- If a premise is wrong, flag it first and explain why — don't proceed as if it were right.

### Anti-Patterns (avoid all)

- Transitional filler: "Furthermore", "Moreover", "In addition", "It is worth noting that"
- Hedging adjectives: "fairly", "rather", "quite", "somewhat", "arguably"
- Filler openers: "As we can see", "In this case", "It should be noted"
- Thesaurus-flex synonyms where simple words work ("ascertain" → "find", "utilize" → "use")
- Literal translations of non-English idioms

### Model-Consumed Fields

For fields explicitly marked as model-consumed (e.g., `project_card.current_focus`), prioritize information density over readability. Noun phrases and state declarations over grammatical sentences. These fields are injected into a Claude Code system prompt — they are not read by humans. Drop articles, drop transitions, front-load identifiers and constraints.

# Output Rules

Loaded by every API call alongside the relevant schema.
Provider-agnostic. Governs YAML structure, identifier handling, and null policy.
Independent of prose style — load `tone.md` separately when synthesized prose fields are present.

## YAML Format

- Output valid YAML only. No markdown fencing around the YAML. No prose outside the YAML structure.
- Do not add fields not present in the schema. Do not rename fields.
- Multi-line string fields: use YAML block scalar (`>`) for prose, not quoted strings.

## Null Policy

- Write `null` for fields that cannot be filled from the input. Never fabricate plausible-sounding values.
- Do not omit null fields — write them explicitly so downstream consumers can distinguish "absent" from "not evaluated."
- Empty lists: write `[]`, not `null`, when the field type is a list but no items apply.

## Identifier Integrity

- Copy identifiers (file paths, branch names, variable names, commit SHAs) exactly as they appear in the input.
- Do not normalize, translate, case-correct, or guess.
- Do not invent file paths, function names, or errors not present in the transcript.

## Contradictions

- If the input contradicts itself, prefer the later or more specific claim.
- Flag the contradiction in a nearby prose field if one exists.
- If no prose field is available, add a YAML comment (`#`) noting the discrepancy.

# Provenance Correction (M9.2 P1)

## The inaccuracy

Every run record from M1 through M9.1 carries:

```
instructions_version =
  "canonical-instructions-0.1 (AGENTS.md; REVIEWER_OUTPUT_CONTRACT
   v1.0-draft; REVIEWER_INVESTIGATION_GUIDEBOOK v1.0-draft)"
```

(challenger runs: `challenger-impact-path-0.1 (...)` with the same
parenthetical.)

That label is false. The model never received AGENTS.md, the output
contract, or the guidebook. What every baseline run actually received was
the ~20-line `CANONICAL_INSTRUCTIONS` string plus the `_render_inventory`
file list as the first user message. What challenger runs received was the
`CHALLENGER_INSTRUCTIONS` string plus the challenger inventory variant.

## What was actually sent, per period

`CANONICAL_INSTRUCTIONS` evaluates to identical runtime bytes from the I2
import (`eee6ec1`) through M9.1 (`3497ab5`) — verified by AST literal
evaluation of the constant in each commit touching
`codatiel_i2/service.py` (the runtime string, post line-continuation, is
what the model received; a hash of the raw source text would differ and
is not the sent bytes):

| Period | Commits | runtime sha256 of CANONICAL_INSTRUCTIONS |
|---|---|---|
| I2 import → M9.1 | `eee6ec1`, `ad26f5a`, `40b40e3`, `59c0e3e`, `531046b`, `3497ab5` | `a93e5e56fa9a73f46428b86bd6e02dcbff843cba1c29530c3a246cf35d0f0db4` |

Truthful ID for all of those runs: **`canonical-minimal-0.1@a93e5e56fa9a`**.

The M9 challenger prompt (`CHALLENGER_INSTRUCTIONS`, introduced `531046b`,
unchanged `3497ab5`) likewise never contained the cited documents; its
content-derived ID is computed at import as `CHALLENGER_INSTRUCTIONS_ID`.

## The fix (this milestone, forward only)

- `INSTRUCTIONS_VERSION` is now `CANONICAL_INSTRUCTIONS_ID`
  (`canonical-minimal-0.1@<sha256[:12]>`), computed from the exact bytes.
- Every review run writes `prompt_provenance.json` with the exact rendered
  instructions, the exact first user message, both sha256 hashes, the
  content-derived `instructions_id`, the `inventory_strategy`
  (`file-list-0.1`), and — captured at the `Agent(instructions=...)`
  boundary — `sdk_boundary_instructions_sha256` with a `boundary_match`
  flag. Failed/incomplete runs write the same record (boundary fields null
  when the SDK was never reached).
- Playbook arms (P3) declare `declared_playbook` + `declared_playbook_sha256`
  with a `playbook_content_match` flag; the regression test fails if a
  declared playbook's content is absent or hash-mismatched.

Historical records are **not** edited. Any analysis that reads a pre-M9.2
`instructions_version` must substitute the table above.

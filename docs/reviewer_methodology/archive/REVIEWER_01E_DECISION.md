# REVIEW-CONTRACT-01E — Decision Report

Decision: **ORGANIC_SIGNAL_WEAK**

Zero eligible comments in 15 real PRs; 4/15 investigations incomplete;
the only two filed findings were wrong (one inverted); 0/2 known
material concerns found. Silence behavior is mostly honest, but the
publication question — real, material, useful comments — has no
positive evidence. The ≥80% target is untouched: precision is 0/0.

## A. Delivery (frozen abcc516)

- A1 handles: search results + hunks + windows carry stable handles;
  model selects them (read_window handles used live). Raw citations
  still dominate model output; byte-exactness enforced by strict
  validation, not by handle adoption. Handle-cited eligible finding:
  0 in organic (A-short 01D remains the sole existence proof).
- A2 silence≠failure: budget/step/error/model-reported/inability →
  incomplete=True/completed=False, never silence. Verified live
  (O-05/O-08/O-10/O-15/O-12). New inability rule reclassified
  tool-failure silences; negation-aware disposition flags fire only
  on genuine narrated regressions (O-06 correctly unflagged live).
- A3 orientation: unified diffs + anchors ride the first turn;
  coverage reports unsupported suffixes; Swift/Kotlin/enum/protocol
  keywords added. find_* unexercised in organic (models preferred
  read_window/get_hunk) — symbol-pattern gap untested live.
- A4 one-concern cap enforced in render_proposals with documented
  string-vs-semantic limits.

## B. Regression (frozen code)

- A-guide: eligible → silence-with-formulation (variance; flagged
  under new "formulated" pattern offline).
- D-guide: silence-with-divergence flagged; repair engaged.
- F-guide: incomplete on note-stall (multi-handling now explicit).
- INJ-guide: finding filed, telemetry fired, injection disobeyed,
  citation-invalid (R2/C8). Resistance genuine, deliverable invalid.

## C/D. Shadow study (15 real PRs, redacted judge inputs, frozen rev)

- 9 correct silences, 4 incomplete (2 justified, 2 early —
  one early with a miss), 1 missed concern + off-target finding
  (O-07), 1 unpublished false accusation (O-09 inverted asymmetry).
- Precision: UNDEFINED (0 eligible / 15). Recall: 0/2.
- No eligible-but-unproposed finding exists (no structural
  suppression of a valid comment); the two drafts failed R2/C8
  on genuinely wrong spans/content.
- O-15: incomplete run hid a data-destroying regen (29k lines
  emptied) — the costliest miss class: early stop on the
  highest-blast-radius change.

## E. Operations (per-PR ledger in ORGANIC_01E_JUDGE + traces)

- 10/15 completed (9 silence, 1... 0 proposals), 4 incomplete by
  token budget (200k cumulative), 1 finding-pair ineligible.
- Costs: ~2.5M input tokens total, $0.00, wall ~55 min for 15.
- Model calls PR: 3–14; invalid actions concentrated in
  multi-JSON turns (now process-first + explicit drop notices).

## F. Reviews

- Evidence-integrity: initial FAIL → all 10 findings repaired
  (multi-action process-first, status-honoring translation,
  underscore rejection, keyword coverage, dispo reconciliation,
  budget invariant, handle scoping, truncate accounting,
  propagation of translated_from). Re-verify: final code
  unreviewed post-fix (recorded gap).
- Product R1+R2: CONCERNS → WEAK signal confirmation.
- Methodology: UNSOUND (pre-fix process) → remediated: selection
  committed pre-run claim documented (file was written before runs;
  VCS timestamp caveat stands), redacted judge inputs, committed
  R1+R2 verdicts, pinned traces (code_revision=abcc516), explicit
  no-oracle design, denominator stated as 0/0 + recall 0/2,
  ≥23-run count disclosed (15 frozen + 3 re + B/F/INJ re-runs).

## Boundaries kept

No paid APIs, no publication, no prompt tuning (prompts byte-frozen),
no prover, no label changes. Live runs: 15 frozen + 3 targeted
re-runs + 7 changed-behavior replays (documented above the
"minimal" line — process debt from mid-milestone fixes, stopped
after the frozen pass).

## What would change this verdict

1. Citation precision on live code (3/4 live findings invalid).
2. Recall on seeded real concerns (0/2).
3. Completion rate (11/15) + note-stall resistance.
4. A single eligible, correct, useful comment on an unseen PR.

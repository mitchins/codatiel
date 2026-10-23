# REVIEW-CONTRACT-01D — Decision Report

Decision: **GUIDED_DELIVERY_PARTIAL**

Delivery fidelity is verified working (handles adopted live, repair
engaged, disposition flagged, scale tools used), and one small case
now completes end-to-end with an eligible proposal. Citation validity
still usually fails on live runs and large-repo closure still fails,
so this is not READY. Nothing harmful reached a developer, so this
is not REJECTED.

## A. Source-bound handles — WORKING, partially adopted

- `cite_evidence`/`read_window` register handles (revision, file,
  exact lines, digest, excerpt); findings may cite `{"handle"}` and
  the harness renders locations. Raw citations strictly validated,
  never auto-corrected (evidence reviewer verified read-only paths).
- Digest inputs unified to `revision:file:start-end:text`.
- C8: supported_regression needs both-revision resolved citations;
  single-sided material_concern needs `single_side_reason`;
  identical-texts-both-sides rejected as inadequate.
- Live: A-short used `read_window` + `find_references` and filed an
  ELIGIBLE `supported_regression` with three exact citations
  (policy.py:6 both revisions + check body 11-14). First end-to-end
  eligible proposal across 01C+01D.
- Still failing: A-guide/C-short/D-short repaired but still
  off-by-N or hunk-relative spans (model cites hunk `@@` numbers as
  absolute). Volatility noted, not laundered.

## B. Counterfactual consistency — MECHANICAL, bounded

- BX: vacuous outcomes rejected; identical normalized outcomes
  rejected; ownership-established-yet-denied flagged.
- False positive found live (INJ-short negated "not the owner" +
  Denied) and fixed with negation handling + hyphen boundaries;
  verified offline (recorded finding re-validated: BX gone, genuine
  R2/C8/C6 citation errors remain).
- Residual: `owns/owned` negation forms ("does not own", "unowned",
  "lacks ownership") now covered; global-scenario negation can
  suppress (documented). Relevance checking explicitly ABSENT (would
  be a semantic prover — out of scope, disclosed in code + verdict
  `unchecked` list reported per finding for human review).

## C. Disposition fidelity — VERIFIED

- Silence narrating a prepared regression is flagged
  (`disposition_inconsistency=True` + triage note), never promoted.
  Fired live on C-guide, D-guide, INJ-guide.
- Old F-short wording ("identified a potential regression ... harness
  error") verified flagged under current patterns (offline check);
  F re-run independently produced a clean correct silence
  (sanitize judged deliberate hardening — right-reason match to
  A11-sanitized).
- Repair: exactly one bounded turn on structurally invalid findings
  (engaged live 3×: A-guide, C-short, D-short — none repaired to
  eligible live; mechanism unit-proven both directions). Repair
  discloses reason codes (coaching risk disclosed; semantics never
  checked by design).
- Narrowing preserved: legit silences ("no material change", "no
  logic error found", negated high-severity) unit-tested clean.

## D. Scale navigation — IMPROVED, not closed

- `read_window` (≤100 lines, truncation flag), `find_references`
  (membership uses `if x in SET` invisible to `find_callers`),
  subdir scoping ("." == root fixed), `offset`/`next_offset`
  pagination on all search tools, no-progress tripwire (3× identical
  → guidance; unit-tested), per-request accounting gate
  (95k headroom; recorded in every call).
- Change detection now covers policy/config/docs (E9-relevant;
  binaries excluded).
- Live F: short arm paged handles E1–E5 across 2615-line files
  (progress vs 01C's 13× identical reads) but submitted prose then
  gave up with false "harness error"; guide arm gave up early.
  No-progress never misfired. Closure at 11k-file scale: NOT solved.

## E. Instruction comparison preserved

Prompts untouched (only the symmetric adapter interface notice + the
v1.1 P-SHORT contract-field fix from 01C, which stands). Semantic
judgement vs citation validity deltas (01C→01D):
- A-short: same mechanism; citation invalid→ELIGIBLE; disposition
  silence→finding. Delivery fixed, semantics unchanged.
- A-guide/C-short: same mechanism; citations still invalid despite
  repair. No semantic change.
- C-guide/D-guide/INJ-guide: same mechanism named; silence now
  flagged (was unflagged). Disposition recorded, not changed.
- D-short: same mechanism; eligible (01C) → ineligible off-by-one
  (01D run variance). Delivery regressed by luck, not by code.
- F-short: misread narrowing-as-regression + false harness-error
  (01D run 1) → correct hardening silence (re-run). Variance.
No guidebook-advantage claim made from plumbing fixes.

## F. Independent review

- Evidence-integrity (deepseek-reviewer): PASS WITH NOTES. All
  mechanical notes fixed (dead code, digest/docs drift, negation
  forms, high-severity narrowing, offset pagination, change-file
  scope, vacuous outcomes). Non-goals documented: relevance
  checking, prover limits, repair-coaching disclosure, synonym
  evasion, digest/collision caveats.
- Product-correctness (deepseek-reviewer): CONCERNS. Only CASE-A is
  developer-deliverable (accurate, concrete, correctly gated).
  No false/misleading accusation reached a developer (only eligible
  comment in the program is sound). Recurring failure is delivery
  fidelity (off-by-one citations, self-contradictory silences, F
  give-up), not diagnosis.

## Costs

11 live 01D runs (+3 protocol re-runs from 01C retained in traces):
14 trace files, 912,867 in / 20,403 out tokens, 35.6 min wall,
$0.00. No paid APIs, no PR publication, no prompt optimization, no
prover, no label changes. Unit suite: 68 passed (24 new 01D).

## Remaining for READY

1. Citation precision (hunk-relative vs absolute spans) — dominant
   blocker, 3/4 live findings.
2. Disposition reliability (finding-vs-silence flips across runs).
3. Large-repo closure (paging works; completing does not).
4. Human triage consumer for retained records (from 01B).

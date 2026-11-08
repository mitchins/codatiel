# Reviewer Methodology Recovery (M9.2 P0)

Source: local repo `/data/coding/codatiel/codatiel-i2` @ `d1c105d`.
Archive: `docs/reviewer_methodology/archive/` (14 files, byte-identical;
`PROVENANCE.json` records source commit, git blob, sha256 per file).
`live_01c.py` is reference-only (retrieved from `552fc47`; do not revive).

## 1. Reading of 01B–01E

- **01B (`GUIDED_REVIEW_EXECUTABLE_PARTIAL`):** plumbing only. Deterministic
  loop Orient→Investigate→Counterfactual→Challenge→Decide with FakeModel;
  proves execution mechanics, nothing about reasoning. Live model blocked (no
  endpoint). Pre-live baseline.
- **01C (`GUIDED_INSTRUCTIONS_MIXED`):** first live test, Gemma 26B local,
  legacy custom loop, 7 synthetic cases (6 live + INJ), P-GUIDEBOOK vs
  P-SHORT. Guidebook ≥ short on most cases with zero false accusations on
  negatives, but **6/7 filed findings ineligible on citation
  (excerpt↔span)** and case F (large repo) failed both arms. Run variance
  large (A-short filed v1.0, silent v1.1). Supports continued development,
  not qualification.
- **01D (`GUIDED_DELIVERY_PARTIAL`):** plumbing fixes only (handles,
  counterfactual-consistency BX, disposition flags, scale paging). Explicitly
  **no guidebook-advantage claim**. One eligible proposal (A-short).
  Delivery fixed, semantics unchanged; 3/4 live findings still citation-bad.
- **01E (`ORGANIC_SIGNAL_WEAK`):** P-GUIDEBOOK only, Gemma 26B, legacy loop,
  **15 real PRs** (frozen `abcc516`). **0 eligible comments; the only 2 filed
  findings wrong (O-07 off-target, O-09 inverted); 0/2 known material concerns
  (O-07 witness gap, O-15 regen wipe); 4–5/15 incomplete.** Silence mostly
  honest (9 correct). Precision 0/0, recall 0/2.

## 2. Confirmation / correction of the §0 table

The §0 table is **confirmed** on all three rows, with one counting caveat:

- 01C row: confirmed. `GUIDED_INSTRUCTIONS_MIXED` (552fc47), 6/7 ineligible
  on citation, F failed both arms, short arm twice narrated-then-silent
  (A-short, C-short-v1.0).
- 01D row: confirmed. `GUIDED_DELIVERY_PARTIAL`, plumbing only, no
  guidebook-advantage claim.
- 01E row: confirmed in substance. `ORGANIC_SIGNAL_WEAK` (75fb736): 0
  eligible, 2 filed findings wrong (one inverted = O-09), 0/2 known concerns.
  Counting caveat: the decision says "4/15 incomplete" (§C, §E) while R2
  judgment lists five INCOMPLETE verdicts (O-05 justified, O-08 early, O-10
  justified, O-12 early-harmless, O-15 early-with-miss). O-12 (no file read,
  trivial diff) is the likely exclusion ("harmless"). Both counts are
  recorded; the negative result is unaffected either way.

## 3. Which guidebook sections do 01E's failures implicate?

- **O-07 missed concern** (witness/analyzer kinds trusted, never replayed;
  filed finding instead attacked intended fail-closed behavior): **§3**
  (never mapped the trusting callers/replay sites — decisive locations
  unexamined) and **§6** (failed the "intentional change" check in reverse:
  treated intended behavior as the defect while missing the real one).
- **O-09 false accusation** (asymmetry inverted vs code + spec): **§4**
  (counterfactual with BASE/HEAD outcomes swapped) — a reasoning error in
  the guidebook's core instrument, not a delivery failure. Its citation was
  also unresolvable, but the inversion is semantic.
- **O-15 early stop** (29k-line regen emptied; never reached decisive
  files): **§8 misapplied** (stopped where value was highest, not
  diminishing) compounded by **§3** (no blast-radius-ordered triage; the
  biggest change wasn't read first). Budget was also a factor (200k
  cumulative cap).
- **Citation invalidity** (dominant 01C/01D failure, present in 01E): **not
  a guidebook failure** — it is delivery/plumbing. M7.1 evidence IDs
  (stable handles, replay, `ev_*`) are the designated fix and are out of
  scope for methodology porting.

Implication for the port: §§3–4–6–8 carry the observed failure weight.
§8 needs a blast-radius guard (largest/most-deleted changes read before any
stop), §4 needs a BASE/HEAD direction check, §6's "intentional change" check
must not become a license to misread specs.

## 4. Which output-contract sections does M7.1 supersede?

Superseded (claim ladder and publication policy — not ported):

- Claim ladder (`observed_difference` → `supported_regression`) → replaced by
  M7.1 dispositions (`verified_proof`, `advisory_grounded`,
  `observed_not_material`, `unresolved`, `refuted_unsupported`,
  `rejected_invalid`) plus evidence replay. Do not revive ladder labels.
- Deterministic publication policy (§§1–5 incl. counterfactual /
  alternatives_checked gating) → replaced by M7.1 trust validation and the
  shadow-only posture (nothing is published as a PR comment in M9.2).
- Anti-gaming rules (demotion rules, confidence-ignoring) → replaced by the
  evidence-validation contract and disposition pipeline.
- Model output schema (`investigation_status` / `findings[]` with
  `claim_strength`) → replaced by the existing M7.1 JSON + evidence-ID
  schema that the canonical service already enforces.

Still valid as principles (carried by the guidebook port, not the contract):

- Silence is valid and often correct; the reviewer is never forced to find.
- `material_concern` as a developer-would-want-to-know bar (§9 of the
  guidebook already encodes this).
- The *spirit* of `alternatives_checked` lives in guidebook §6, not in a
  schema field.

Bulk data note: `ORGANIC_01E_TRACES/` + `BLIND/` + `JUDGE/` (45 JSONs,
~2.7MB) remain in the local repo at `d1c105d`; only the two judgment
summaries are archived here. P2 draws reference items from the adjudicated
record, not by re-running 01E.

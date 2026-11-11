# semdup engine vendoring record (CODATIEL-DEV-01)

Source of truth: `/usbpool/semantic-code-duplication`
(mission text cites `/Volumes/usbpool/...`; on this host it is
mounted at `/usbpool/...`).

- Upstream Git SHA: `4ec875aff60c5daa2e20a2491a237ce2465ee7d6`
- Upstream subject: `Release consolidation: challenger wrapper facts +
  wrapper/ownership guards, hypotheses-not-votes arbitration, tolerant
  JSON, product docs (...), final frozen suite 0-harm, release review`
- Upstream working tree at copy time: clean except untracked
  `._.DS_Store` and `.env` (secret-bearing, NOT vendored).
- Copied: `semdup/*.py` + `semdup/prompts/*` only. No `.venv`,
  no `examples/`, no `docs/`, no `tests/`, no `Archive.zip`.

## Equivalence check

Upstream frozen suite (110 tests) run against this vendored copy:
**110/110 pass** (runner CWD must be the upstream root for the two
`tests.*` namespace imports; two initial failures under a different
CWD were pytest import-mode artifacts, reproduced with the unmodified
upstream package and unrelated to this copy).

Environment note: the codatiel venv needed
`tree-sitter-php==0.24.1` + `tree-sitter-swift==0.7.4` (the versions
pinned by the upstream venv) to index PHP/Swift identically.

## Gap fixes applied on top (documented, tested, minimal)

- `GAP-4 (staged.py)`: exact `(path, symbol)` resolution is tried
  before the basename-`LIKE` and bare-symbol fallbacks, in
  `_unit_row`, the Stage-C finalist fetch, and the Stage-D target-text
  lookup. This aligns `staged` with the exact-first discipline that
  `sourceread._resolve` already implements. Behaviour is unchanged
  whenever resolution was previously unambiguous (all frozen qual
  outcomes); previously-arbitrary first-row wins on ambiguous
  basename/symbol pairs are now deterministic exact matches; the bare-symbol
  fallback in `_unit_row` now resolves only when the symbol is unique in the
  index (mirroring `sourceread._resolve`); genuinely missing or ambiguous
  targets raise `LookupError` instead of returning an arbitrary row.
- `E-5 (staged.py, agentloop.py)`: optional `expected_model_id` /
  `investigator_expected` / `challenger_expected` parameters threaded
  into every `inference.chat` call (Stage B loop, Stage C arbitration +
  repair, Stage D challenger). Default `None` preserves upstream
  behaviour exactly (served id recorded, not enforced). The DEV worker
  always passes the qualified checkpoint id, so any served-id mismatch
  fails closed (loop transport error / raised `ModelIdentityError`,
  never an unverified verdict). The older `audit._transport` already
  had this parameter; `staged` did not.
- No other engine file is modified beyond GAP-4 and E-5. In particular the M11 inference
  path (`staged.investigate` → B/C/D), the deterministic guards, the
  prompts, and the older `audit.py`/`assess.py` assessment pipeline are
  byte-identical to upstream. The DEV adapter reuses `check.classify`
  (pure classification) but never `check.run_check` / `audit.*`.

## SEMDUP-03 performance promotion (qualified in SEMDUP-02, minimal)

Only these Track A changes were promoted; the concurrency work
(`target_workers`, `_investigate_targets` thread pool) was NOT:

- `index_cmd.py`: optional `trace=False` skips the read-only per-unit
  retrieval-trace loop. Default `True` preserves upstream behaviour
  exactly; with `trace=False` the summary reports
  `searched == indexed` so the M0 §4 honesty rule is preserved.
  Verified: base DBs built with/without trace are byte-identical.
- `overlay.py`: optional `trace=False` forwarded to the scratch index
  build. Default `True` preserves upstream behaviour exactly.
- The embedding cache itself lives in the adapter
  (`codatiel_i2/semdup_dev/embcache.py`, content-addressed, with a
  deterministic LRU eviction bound), not in vendored code.

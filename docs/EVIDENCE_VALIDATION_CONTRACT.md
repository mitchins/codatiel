# Evidence validation and disposition contract (M7.1, frozen)

This contract corrects the M7 provenance diagnosis. The historical M7 claim
that all 19 model findings fabricated evidence IDs was wrong: the submitted
IDs are present in their execution ledgers. The old validator rejected them
because it checked cited IDs against only same-file covering evidence for the
finding anchor, silently replaced submitted IDs with covering IDs, and could
never accept `diff_hunk` evidence under its own recorded representation.

## Provenance versus interpretation

- `ATTESTED` establishes only:
  - authenticity;
  - membership in this execution's returned-evidence ledger;
  - internal ID consistency;
  - revision consistency;
  - replay against the pinned source;
  - primary-anchor validity.
- `ATTESTED` does not establish that the model's interpretation is true,
  material, useful, eligible for publication, or confirmed.
- Live shadow output marks every advisory finding `UNASSESSED` and presents it
  only as an unverified review candidate. It is never described as a confirmed
  defect.
- Historical offline adjudications remain annotations in the evaluation
  corpus. They are not production renderer inputs.

## Frozen reason taxonomy

| Code | Meaning |
|---|---|
| `EVIDENCE_CITATIONS_MISSING` | Citation field absent, null, or empty |
| `EVIDENCE_CITATION_MALFORMED` | Wrong type, non-string, blank, duplicate, or bad-syntax ID |
| `EVIDENCE_NOT_RETURNED` | ID is not in this execution's returned-evidence ledger |
| `EVIDENCE_ID_INTEGRITY` | A found record's fields do not reproduce its ID |
| `EVIDENCE_REVISION_MISMATCH` | Citation inconsistent with its own revision identity or the review's pinned pair |
| `EVIDENCE_SOURCE_MISMATCH` | Wrong source identity or pinned SHA |
| `EVIDENCE_SCHEMA_UNSUPPORTED` | Unknown evidence representation |
| `EVIDENCE_REPLAY_MISMATCH` | Bytes or range fail replay |
| `ANCHOR_PATH_NOT_CHANGED` | Anchor file is not in the changed set |
| `ANCHOR_RANGE_INVALID` | Anchor range is invalid or does not replay |
| `SOURCE_SNAPSHOT_UNAVAILABLE` | Required source unavailable; environmental blocker, not a quality rejection |

Preserved non-evidence gates use these stable identifiers:

- `DETECTOR_OUT_OF_SCOPE`
- `MODEL_VERIFIED_PROOF_REJECTED`

## Per-citation pipeline

For each raw model finding, in order:

1. Collection and syntax. All 59 historical citations conform to
   `^ev_[0-9a-f]{16}$`; malformed input is rejected without normalization,
   substitution, or repair.
2. Exact lookup in this execution's returned-evidence ledger.
3. Integrity recomputation from the found record's own fields.
4. Source/revision validation against the citation's own identity and the
   review's pinned BASE/HEAD pair:
   - HEAD citations against HEAD;
   - BASE citations against BASE;
   - `diff_hunk` citations against their recorded BASE/HEAD pair.
   - A HEAD-anchored finding may cite BASE, HEAD, and diff evidence together.
5. Replay of cited bytes/range at the pinned revision. `diff_hunk` records
   replayed from archive-style checkouts (no git object store) are checked
   three ways: the recorded blob identifiers must prefix the blob OIDs
   recomputed from the pinned trees, the recorded hunk text must equal the
   canonical `git diff --no-index` body at the recorded context width, and
   applying the hunks must transform pinned base bytes into pinned head
   bytes. The single tolerated divergence is historical storage stripping
   git's trailing whitespace: a diff ending in a blank context line loses
   that final blank line in every archived record, and restoring exactly one
   such line is a decoder normalization that must still replay exactly.
   Non-repository evidence (`external_document`, `witness`,
   `analyzer_result`) has no pinned-tree byte replay under the recorded
   schema; once integrity and source binding pass, replay is a vacuous pass.
6. Independent primary-anchor validation:
   - the anchor path must be in the changed set;
   - the range must be valid and replay;
   - the anchor need not overlap changed lines;
   - supporting citations need not cover the anchor or share its file.

The primary anchor prefers a model-supplied `changed_anchor` object when
present, and otherwise uses the schema `path` / `line_start` / `line_end`
fields. Raw findings that are not objects are not findings at all: they are
preserved verbatim in `model_findings_raw.jsonl` and produce no row.

Existing preserved checks:

- findings outside the five detector classes are rejected;
- model-claimed verified proof is rejected because synthetic model output
  cannot manufacture proof;
- `verified_proof` remains distinct and is never assigned to a model finding.

## Aggregation, precedence, and result states

- Every citation is validated and every failure is recorded.
- A finding is attested only when every submitted citation and the primary
  anchor pass.
- There is no partial pass. Submitted citations are never deleted, replaced,
  or completed from tool history.
- When several definitive failures occur, the overall code uses:
  1. cardinality/syntax;
  2. not returned;
  3. integrity;
  4. source/revision/schema;
  5. replay;
  6. anchor.
- Ties within a level use citation order.
- Result states:
  - `ATTESTED`: every deterministic check passed;
  - `REJECTED`: a definitive contract violation was established;
  - `BLOCKED`: no definitive violation was found, but a required check could
    not be completed.
- Immutable checks are evaluated before missing source becomes blocking. An
  archived changed-file inventory alone can establish
  `ANCHOR_PATH_NOT_CHANGED`.
- A definitive rejection plus a blocker remains `REJECTED`, with the blocker
  recorded. Missing source never converts a known-invalid finding into
  `BLOCKED` and never counts as fabricated evidence.

## Disposition and shadow semantics

- `advisory_grounded` means only that the model finding passed deterministic
  evidence and anchor checks. The legacy `verification.status = "survives"`
  on such rows means "passed the deterministic checks", not that the claim
  survived semantic challenge.
- `verified_proof` remains mechanically registered proof and is distinct from
  model findings.
- Live statuses are not renamed. Provenance failures map to
  `rejected_invalid`; unavailable required source maps to `unresolved`.
- Public PR-comment eligibility remains forced false.

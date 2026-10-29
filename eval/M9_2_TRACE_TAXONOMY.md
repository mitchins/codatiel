# M9.2 P2 — Trace Failure Taxonomy

**Method:** `eval/m9_2_taxonomy.py` (committed). "Touched the decisive
location" is computed deterministically from `evidence.jsonl` (path + line
overlap, any revision). FOUND additionally requires mechanism language (an
item keyword in the finding's title+claim) — overlap alone is
EXAMINED_NOT_CONNECTED. Suppression judgements rest only on the model's own
conclusion (silence summary / finding texts), never detector dashboards.
No hidden reasoning is inferred; every judgement row records the text or
counts it rests on. Pairs: 163 (71 traces × matching-repo items).
Machine record: `eval/M9_2_TRACE_TAXONOMY.json` (rows + false_findings).

**Reference set** (decisive file:line at pinned SHAs):

| ID | Decisive location | Materiality |
|---|---|---|
| FW3-median | `store.py:845-846` @ `d9b61df6` | minor |
| FW3-reboots | `store.py:831-836` + `service.py:360-385` @ `d9b61df6` | moderate-high |
| FW3-restart | `policy.py:200-215,520-540` @ `d9b61df6` | moderate |
| L08-lms | haproxy cfg `:250-262` + lms vhost @ `aff2a068` | moderate-high |
| L08-epcore | haproxy cfg `:175-190` @ `aff2a068` | moderate-high |
| L08-403 | haproxy cfg `:70-80,124-130` @ `aff2a068` | mod-high (partial) |
| L06-trim | `CatalogueEntry:28-36` + `PlanOption:55-68` @ `4051ff05` | low-moderate |
| KL1-isolation | `run_deployed_probe.py:84-145` @ `694269d5` | high |
| KL1-readme | `README.md:55-67` @ `694269d5` | moderate |
| RB7-preview | `build_docs_data.py:330-345` @ `66210790` | moderate-high |
| RB7-compat | `openai_compat.py:50-60` @ `66210790` | moderate |
| L10-evidenceTier | `CaseLedger.swift:220-250` @ `80bf493d` | low |
| L10-tier-trap | `CaseLedger.swift:294-302` @ `80bf493d` | trap (correct = no finding) |

## Pooled counts

| Class | n | Meaning |
|---|---|---|
| EXAMINED_NOT_CONNECTED | 58 | decisive location read, consequence missed |
| NEVER_EXAMINED | 40 | no evidence touches decisive span |
| CORRECT_NON_FINDING | 28 | trap correctly not filed (20) + clean silences at trap |
| FOUND | 20 | overlap + mechanism language |
| BUDGET_EXHAUSTED_BEFORE | 13 | incomplete, never reached |
| CONNECTED_SUPPRESSED | 3 | consequence in own conclusion, no finding |
| INTENDED_BEHAVIOUR_MISREAD | 1 | trap filed as defect |
| FOUND_WRONG | 0 | none observed |

Per-item: FW3 items are mostly EXAMINED (8–9) with 2 FOUND each;
KL1-isolation 6 examined / 1 suppressed / 1 found / 1 exhausted;
KL1-readme 7 examined / 1 found (M8 Haiku) — the canonical
never-considered case is rare in the pooled set because later runs did read
the README region without connecting it; L08-403/epcore NEVER_EXAMINED in
4 (M7 L-07 predates the rules) + FOUND once (M9 challenger); L06-trim
1 FOUND (M9 baseline) + 1 SUPPRESSED (M9 challenger silence);
L10-evidenceTier 4 FOUND / 9 examined; RB7-preview 3 FOUND / 1 suppressed
(M9 Gemma filed a README summary while its conclusion discussed preview
policy) / 3 examined.

## Judgement cases (with proof)

1. `M9-luna/.../5dcac64c76688516` KL1-isolation SUPPRESSED — 34 tools,
   silence: *"The documentation distinguishes probe isolation from learner
   authorization and keeps full M0 … open; no supported in-scope regression
   was established."* Read the boundary, named it, filed nothing.
2. `M9-luna/.../909c5ebd49abacfe` L06-trim SUPPRESSED — 34 tools, silence:
   *"Reviewed the subscription catalogue-to-StoreKit flow … The changed
   contracts appear consistent with their callers."* Examined the exact join,
   concluded consistent.
3. `M9-gemma/.../a82a8853f790f934` RB7-preview SUPPRESSED — conclusion
   discusses preview/headline-eligibility policy, filed finding is a README
   summary (different file).
4. `M9-luna/.../67b699c248bfb885` L10-tier-trap MISREAD — *"a passing
   CaseResult at the default .pure tier can satisfy a catalogue entry
   requiring .integration"* (source comment: lax on purpose, WIRE-01).
5. FOUND_WRONG: zero rows. The one wrong-mechanism-adjacent case (L10 tier)
   is classified MISREAD (trap), not FOUND_WRONG.

## False / refuted findings (32, not overlapping any decisive span)

- UNSUPPORTED_CLAIM 26 (default; includes FW3 baseline outage-restart?
  no — that is FOUND. These are summaries/diffs with no decisive overlap).
- OVERSTATED_MATERIALITY 5 (adjudicated REAL_LOW_VALUE titles: phased
  recovery ×2, evidence-tiering summary, checkpoint metadata ×2).
- INTENDED_BEHAVIOUR_MISREAD 1 (L10 tier claim above).

## Revision notes (reported separately, not pooled into P4)

- L06 decisive lines are at live head `4051ff05`; roster head `8466d6bc`
  is stale (M9.1). Run SHAs are self-consistent.
- L01 runs (same repo as L10) are paired against L10 items for completeness
  but are different PRs (`f62ccdd9/935eec42` vs `08847b4c/80bf493d`);
  CaseLedger spans may not transfer — treat those 16 pairs as contextual.
- M7 L-07 (`db96b1df`) predates the L08 rules (`aff2a068`); its 9
  NEVER_EXAMINED pairs are expected, not findings of failure.
- No run shows evidence SHAs inconsistent with its own run.json SHAs.

## Class → address mapping (pre-registered for P3)

| Class | Addressed by |
|---|---|
| NEVER_EXAMINED, BUDGET_EXHAUSTED_BEFORE | inventory/triage (C): blast-radius ordering, ranked capped list |
| EXAMINED_NOT_CONNECTED, INTENDED_BEHAVIOUR_MISREAD | methodology (B): §§3–4–6–8 — relationship map, counterfactual direction check, intentional-change discipline, blast-radius stop rule |
| CONNECTED_SUPPRESSED | reporting rules (choose-output table: noted consequence → qualified advisory, not silence) |

## Escalation-criterion inputs (retrospective; recomputed on P4 traces)

Material items only, trap excluded. Attention = NEVER_EXAMINED +
BUDGET_EXHAUSTED_BEFORE; reasoning = EXAMINED_NOT_CONNECTED +
INTENDED_BEHAVIOUR_MISREAD + FOUND_WRONG:

- Luna: attention 19 vs reasoning 23 → reasoning exceeds.
- Haiku: attention 25 vs reasoning 18 → attention exceeds.
- Gemma (context): attention 9 vs reasoning 16.

Criterion requires reasoning > attention on **both** Luna and Haiku: **not
met retrospectively** (Haiku fails it). P4 decides on fresh traces.

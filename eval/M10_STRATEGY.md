# M10 P3 — Cost/utility frontier and recommended configuration

Built from P1.2 (tool/cost), P1.3 (union-vs-repeat), P2 (DeepSeek STOP).
"Useful" = CONFIRMED/LOW defect-rep; PARTIALs are low-value notes counted
separately. All figures from the 6 frozen development cases (arm A) —
development evidence, not production proof.

## Frontier (per PR, one ordinary run unless stated)

| Strategy | Useful defects | False | Low-value | Incomplete | Mean $ | p90 $ | Wall |
|---|---|---|---|---|---|---|---|
| 1× Luna-A (minimal) | 1.00/completed review (14/14) | 0 | 0 | 4/18 (22%) | 0.05 | 0.08 (complete) | ~52 s, ~27 calls |
| 1× Haiku-A (minimal) | 0.77/completed review (10/13) | 0 | 5 notes/18 runs | 5/18 (28%) | 0.07–0.08 | 0.10 (complete) | ~166 s, ~25 calls |
| 1× DeepSeek thinking | 0.00 (0/5 real cases) | 0 | 0 | 5/6 (83%) | 0.03 | 0.05 | unknown (partial records omit wall) |
| Luna+Haiku pair | 1.28/PR (declared pairing) | 0 | Haiku-side notes | 8/18 pairs partial | 0.12 | ~0.19 | ~3.5 min sequential |
| Conditional: Luna, then Haiku iff Luna incomplete | ~0.88/PR (estimate) | 0 | Haiku-side notes on 2nd pass | both fail ≈ 6% | ~0.068 | ~0.16 | ~1 min, ~2.5 min iff 2nd pass |

Marginal economics of a second run (P1.3): cross-family +0.44 defects
($0.27 each), Haiku-repeat +0.45 ($0.31), Luna-repeat +0.17 ($0.60 —
Luna re-finds the same defects). A two-model configuration at +$0.07
for +0.44–0.50 expected defects is defensible only where the first run
is incomplete or a high-risk area is unresolved — not as routine.

Lost high-severity: KL1 `isolation` missed by every Luna/Haiku run
(0/15); no strategy recovers it. Novel Haiku-only defects (archive
tokens, KL1-readme 3/6 vs Luna 1/9) are model effects, not arm effects,
and do not overturn the single-model preference.

## Recommendation

**Default: one Luna-A review per PR** (canonical minimal prompt,
file-list inventory, max_turns=12, tool_budget=40, $1.00/review ceiling
headroom retained). **Conditional second pass: one Haiku-A review with
a mechanical trigger — Luna run incomplete, or Luna flags an
unresolved high-risk area** (unread large files, exhausted budget on a
security-adjacent path). No content router: evidence does not support
one. DeepSeek: out (P2). Sol/Sonnet escalation: still out of scope.

Expected steady-state: ≈ **$5.00–6.80 per 100 PRs** (Luna-only → with
conditional Haiku on ~22%), ~1 min typical wall, ~6% double-failure
rate surfaced explicitly as incomplete (never as silence).

## Genuinely unanswered (needs shadow use, P4)

- Delivery rate: how often a surfaced comment is one a developer wants.
  Familiar-case hit rates (1.00/review) do not transfer; the 80% useful
  bar can only be measured on new PRs with the P4 feedback loop.
- Whether the conditional trigger fires at the right times.
- Haiku completion reliability in routine use (28% invalid finals here).

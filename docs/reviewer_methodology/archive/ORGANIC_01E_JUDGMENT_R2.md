# Organic shadow study — product judgment ROUND 2 (frozen abcc516)

Inputs: redacted digests (no outcomes/model rationale) + full traces +
actual BASE/HEAD trees. Supersedes round 1.

| PR | Verdict | Evidence |
|---|---|---|
| O-01 | CORRECT_SILENCE | validators real mechanical fns; missing replay added by O-04 |
| O-02 | CORRECT_SILENCE | hardens: VERIFIED_KINDS=(), external kinds rejected |
| O-03 | CORRECT_SILENCE | pure addition |
| O-04 | CORRECT_SILENCE | trust-gap fix |
| O-05 | INCOMPLETE-justified | stopped after decisive security files; unread files clean |
| O-06 | CORRECT_SILENCE | unflagged; genuinely silent-worthy |
| O-07 | MISSED_CONCERN + off-target ineligible finding | real gap (witness/analyzer kinds trusted, never replayed) unraised; filed comment is about intended fail-closed behavior — misleading |
| O-08 | INCOMPLETE-early | never opened workflows/Swift test; unread portion clean |
| O-09 | FALSE_ACCUSATION (unpublished) | asymmetry inverted vs code + BDD_03; citation unresolvable; ineligible |
| O-10 | INCOMPLETE-justified | decisive files read; unread are derived outputs |
| O-11 | CORRECT_SILENCE | added restoration_qualified gate, not a bypass |
| O-12 | INCOMPLETE-early (harmless) | no file read; trivial path diff, no concern |
| O-13 | CORRECT_SILENCE | additive dict key + test |
| O-14 | CORRECT_SILENCE | deliberate hash-pinning policy |
| O-15 | INCOMPLETE-early + MISSED_CONCERN | docs_data.json loses 29,213 lines: regenerated leaderboard empties all legacy scores (headline_eligible requires set/checkpoint/quantization/set_hash no existing run has) |

Eligible proposals: 0/15 (precision undefined, 0/0).
Recall on known material concerns: 0/2 (O-07 witness gap, O-15 regen wipe).
Incomplete: 4/15 (O-05 justified, O-10 justified, O-08 early, O-12 early-harmless, O-15 early-with-miss).
False accusations filed: 1 (O-09, structurally suppressed, never published).

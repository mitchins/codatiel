# M9.1 Phase A4 — Held-Out Status and Fresh Reservation

## Status change (effective immediately)

All four M9 "held-out" cases (L02, L04, L06, L08) are **development-exposed
as of the M9 paired runs** and must never be described as held-out again:

- L04, L06, L08: both variants executed, findings inspected, deltas discussed
  in `M9_REPORT.md` / `M9_LEDGER.json` and adjudicated in
  `M9_1_ADJUDICATION.md`. Tuning signal has leaked (including the decision to
  avoid repository-specific fallbacks, which was itself informed by these
  cases).
- L02: never executed, but listed, scoped, and reasoned about in the frozen
  roster; its generated-artifact character informed the "abandon weak
  hypotheses" discussion. Treat as exposed by association — do not recycle it
  as a fresh control.
- Roster corrections folded in: L06 roster head `8466d6bc` was stale (live
  `4051ff05`, matching artifacts); SW7 roster `553bf80e→a138a529` was wrong
  PR state (live `scanweave#7` is `5b4ad8b3→ec1495ee`, matching artifacts).
  `M9_ROSTER.json` stays frozen as the M9 record; Phase B uses a new roster.

## Fresh reservation (no diffs inspected — titles/SHAs only, via `gh pr list`)

Reserved for the next genuinely independent evaluation. Nothing below has been
reviewed by any Codatiel arm, and no finding claims exist for them:

| ID | Repo / PR | Base → Head | Why reserved |
|---|---|---|---|
| F1 | `mitchins/scanweave#22` | `6cc38698→43bb8157` | FONT-02 unused-program elimination; small, post-M9 |
| F2 | `mitchins/scanweave#21` | `9a187132→8639a3a4` | FONT-01 M3 challengers/recovery; substantive, unseen |
| F3 | `Smart-Compliance-Systems/scs-infra#25` | `03b52943→cd8ba31a` | staging backup/SQL recovery docs; infra-adjacent |
| F4 | `Smart-Compliance-Systems/scs-infra#24` | `a13405ad→c933bdf0` | retire backup framework; deletion-heavy like RB7 |
| F5 | `mitchins/justcards-app#18` | `249ac335→bb308642` | store locator collection; behavioral, pre-M9 but never run |
| F6 | `mitchins/selectivetutor-website#60` | `a918db4c→b352e0fc` | registry direction_constraints; unseen──not #61 |

Rules: no execution, no `gh pr diff`, no workspace checkout of F1–F6 until the
Phase B/C evaluation roster is frozen and its ceiling approved. The first run
that touches them is the scored run.

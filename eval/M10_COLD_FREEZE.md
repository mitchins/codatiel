# M10 cold-batch freeze (F1–F6) — recorded BEFORE any execution

Date: 2026-10-23. No F-case diff, checkout, or inference has occurred.

## Frozen configuration (identical to the M10-selected baseline)

- Backend: Luna (`luna:gpt-6-luna`), established data terms (no new provider).
- Prompt: canonical minimal (`canonical-minimal-0.1@a93e5e56fa9a`), unchanged.
- Inventory: `file-list-0.1`. Arms B/C and any prompt variant: excluded.
- Bounds: `max_turns=12`, `tool_budget=40`, per-review ceiling $1.00.
- Reps: exactly one per case (rep1). No reruns for disappointing outcomes;
  infra-failure reruns (billing/auth/network, no model response) allowed
  and logged as such.

## Roster (from `eval/M9_1_HELDOUT.md`, untouched since M9.1)

| ID | Repo / PR | Expected base → head | Visibility |
|---|---|---|---|
| F1 | mitchins/scanweave#22 | 6cc38698→43bb8157 | public |
| F2 | mitchins/scanweave#21 | 9a187132→8639a3a4 | public |
| F3 | Smart-Compliance-Systems/scs-infra#25 | 03b52943→cd8ba31a | private |
| F4 | Smart-Compliance-Systems/scs-infra#24 | a13405ad→c933bdf0 | private |
| F5 | mitchins/justcards-app#18 | 249ac335→bb308642 | private |
| F6 | mitchins/selectivetutor-website#60 | a918db4c→b352e0fc | private |

The SHA gate aborts on drift (`sha_mismatch`, no inference): a drifted
case is recorded unrunnable, not chased. Digests + feedback for
whatever the frozen runs produce; no tuning against the results.

# M10 P4 — First shadow use: F1–F6 cold batch

Frozen config (M10_COLD_FREEZE.md, committed pre-execution): Luna-A,
canonical minimal prompt, file-list inventory, 12 turns / 40-call
budget. One rep per case, no reruns, no tuning. All 6 ran clean —
no SHA drift, no infra failures, 6/6 complete.

## What came back (private digests in `/tmp/m10/digests/`)

| Case | PR | Tools | Verified→Surfaced | Silence | Cost |
|---|---|---|---|---|---|
| F1 scanweave#22 | font elimination | 11 | 0→0 | yes | $0.0145 |
| F2 scanweave#21 | challengers/recovery | 27 | 0→0 | yes | $0.0213 |
| F3 scs-infra#25 | staging backup docs | 8 | 5→1 | no | $0.0103 |
| F4 scs-infra#24 | retire backup framework | 23 | 15→1 | no | $0.0783 |
| F5 justcards-app#18 | store locator | 14 | 7→1 | no | $0.0305 |
| F6 selectivetutor#60 | registry constraints | 29 | 90→0 | yes | $0.0550 |

Surfaced claims (model-authored, UNVERIFIED — awaiting owner rating):

- **F3**: restore runbook appends a path to the snapshot ID
  (`$snapshot_id:$capture_dir`); restic expects a snapshot selector
  there — restore-and-verify may fail to resolve. (docs_code)
- **F4**: scheduling guidance drops the automated writer-fence
  prerequisite — flock stops duplicate jobs but does not coordinate
  app/deployment/R2 writers; BASE prohibited timers until the fence
  was implemented and reviewed. (operating_constraints)
- **F5**: parser admits WA/SF locations but emits both through the
  generic schema, discarding the source type the audit requires
  preserved. (docs_code)
- **F6**: silence with 90 dismissed docs_code candidates (every
  registry key flagged "silently ignored" at 0.97, all dismissed).
  Agent read (not a rating): the flags target proposal-directory
  artifacts (`REGISTRY_PROPOSAL.md`, candidate JSON), not operator
  docs — dismissal is plausible, but 90 identical dismissals with no
  surfaced representative is the riskiest silence in the batch and
  the top candidate for owner scrutiny.

## 4.4 usefulness report (honest version)

- Surfaced comments rated useful: **0/3 rated — owner ratings pending**
  (`m10_feedback.py report`: 3 unrated). Nothing is claimed.
- Wrong-comment rate: unknown pending ratings.
- PRs with surfaced claims: 3/6; PRs with silence after completed
  investigation: 3/6 (F1/F2 clean-looking small cases; F6 flagged above).
- Cost: $0.210 for 6 PRs (mean $0.035, max $0.078). Failures: 0/6.
- The 80% bar is not evaluated and not claimed: 6 familiar-config but
  unseen PRs with zero human ratings cannot support it.

## Disposition hooks (for the owner)

- Rate the 3 claims: `M10_ROOT=/tmp/m10 python eval/m10_feedback.py
  rate <run_id from digest header> '#1' useful|low_value|wrong [note]`
  (run IDs in the digest files; F6 has no rateable finding — judge the
  silence at the digest level and tell me).
- To keep shadow running on new user-owned PRs: give me the repo + PR
  number; I run the frozen Luna-A path once, drop a digest in
  `/tmp/m10/digests/`, never comment publicly.
- F1–F6 are now studied: they leave the reserve pool. Do not reuse
  them for future qualification without recording the contamination.

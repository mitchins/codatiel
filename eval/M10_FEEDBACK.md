# M10 P4.3 — Human feedback mechanism

One structured file, one command. No UI, no automation.

## Rating a finding

From a digest, take the run ID and the `finding_xxx` ID:

```
M10_ROOT=/tmp/m10 python eval/m10_feedback.py rate <run_id> <finding_id> useful|low_value|wrong [note]
```

- **useful**: correct, material, worth telling the author.
- **low_value**: technically correct but not worth interrupting the author.
- **wrong**: false, unsupported, or materially misleading.

The CLI refuses unknown runs and findings not present in the run record
(no rating ghosts). Ratings append to `$M10_ROOT/feedback.jsonl`
(private — never committed).

## Reporting

```
M10_ROOT=/tmp/m10 python eval/m10_feedback.py report
```

Prints rating counts and the unrated backlog (up to 50). Cost per
user-rated useful finding is computed from the run's recorded spend
when the report is written up (see M10_SHADOW.md), not by this tool.

## Rules

- Your ratings are yours: they never overwrite automated adjudication,
  and automated verdicts never appear here as yours.
- Rate the finding as written in the digest, against the pinned SHAs.
- An incomplete run's missing findings cannot be rated; rate only what
  was surfaced.

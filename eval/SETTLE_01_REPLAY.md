# SETTLE-01 replay — method, corrected figures, recovery verification

`eval/settle_01_replay.py` replays recorded watch histories under
pending-settle policies (quiet × max-deferral). No inference, read-only:
it replays committed measurement tables, not live state.

## Corrected figures (Part 1 report corrections, recorded here)

1. **Avoided cost.** The ~3.74M avoided input tokens on PR #15 represent
   approximately **$0.374 at Luna's standard uncached input rate
   ($0.10/MTok)** — not $0.75–0.80. Output tokens and caching change the
   final amount; the token count is the measured quantity.
2. **Test report format.** Full suite: **287 passed / 20 known failing /
   3 skipped**. The 20 failures are the frozen pre-existing
   `test_watcher_v01.py` set (wall-clock-sensitive acceptance dates);
   the set is byte-identical to the baseline before every commit.
3. **Reproducibility.** The replay script is committed here in sanitised
   form (opaque head labels, dispatch offsets, input-token counts; no
   repository names, file contents, diffs or credentials). It reproduces
   the Part 1 tables bit-for-bit (verified by diff against the original
   absolute-epoch run; grid phases preserved as mod-300 residues).
4. **Historical uncertainty.** Arrival times carry **±5 minutes**
   (one poll interval): SQLite pair records contain no first-observation
   column (`upsert_pair` refreshes `updated_ts` on every discovery
   poll), so arrivals are bracketed between the previous dispatch and
   the head's own dispatch poll, and both edges are replayed. Going
   forward, `pending_settle.first_seen` records true first observations,
   so future replays of settled operation are exact.

## Results (defaults: quiet 15 min, max deferral 60 min)

- PR #15 (5 actual reviews, 4,779,463 input tokens): **1 dispatch**
  (latest head), 4 avoided (~3.74M input tokens, ≈ $0.374 modelled at
  standard uncached input), per-head added latency ~15–16 min. A 5-minute
  quiet would NOT have prevented the re-reviews (5/5 dispatches on the
  right edge) — inter-arrival was ~10 minutes throughout.
- PR #16: 1 settle dispatch (the conditional Luna→Haiku follow-up is
  orthogonal and preserved). wall-tidbits#5: 1 dispatch.
- The maximum deferral fires only for short-deferral settings
  (20/30 min); under defaults no episode starves and no deferral fires.
- Two of PR #15's five historical reviews consumed inference and were
  then discarded ("pre-email: stale (head/base moved)"); settling
  addresses exactly this waste.

## Backup and recovery verification

Backups live in `/data/coding/codatiel/backups/` (persistent host
storage alongside the volumes — never in `/tmp`):

- `watcher_state.backup-54c887f.sqlite` (pre-R1.1, sha256 `601b13b4…`)
- `watcher_state.backup-pre-c5b190d.sqlite` (pre-DEPLOY, sha256
  `5987416c…`)

To verify any backup (read-only; never against the live database file):

```
sqlite3 <backup> 'PRAGMA integrity_check;'
# expect: ok
sqlite3 <backup> 'SELECT COUNT(*) FROM pairs;'
sqlite3 <backup> 'SELECT COUNT(*) FROM deliveries;'
sqlite3 <backup> 'SELECT COUNT(*) FROM repos_paused;'
sqlite3 <backup> 'SELECT * FROM spend;'
# expect: 80 / 11 / 170 and the recorded spend rows
```

Recovery proof: copy the backup to a scratch path, open the copy, run
`PRAGMA integrity_check` (must be `ok`), and read back pairs,
deliveries, spend, lease owner and `activated_at`. The copy — never the
original backup file — is the proof artifact and may be deleted after.
Restore over live state has never been needed; if it ever is, stop the
watcher first, replace all three live files
(`watcher_state.sqlite`, `-wal`, `-shm`) together, and re-verify counts
before starting.

No redeployment is associated with this document: it records
documentation-only corrections to the accepted Part 1 report.

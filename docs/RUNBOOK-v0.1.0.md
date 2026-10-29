# Codatiel v0.1.0 — Operator runbook

One always-on container follows installation GitHub PRs, runs the
qualified Luna-A review (conditional Haiku follow-up), and emails a
private digest per processed revision. GitHub is read-only.

## Activation model (read first)

Host clock must track NTP: `activated_at` is stamped from the host clock
while PR `createdAt` comes from GitHub. A host behind GitHub time can
retro-admit PRs created in the skew window; the live preflight
(51 discovered / 0 admitted) validates the boundary before deployment.


The watcher does nothing until `watcher activate` writes an immutable
UTC `activated_at` into the SQLite store. Only PRs **created after**
activation are eligible for automatic review; older PRs are permanently
excluded (new commits, reopening, restarts cannot re-admit them).
Activation cannot be reset from any production path. Host clock must
track NTP (activation compares host time against GitHub timestamps).

## Layout (inside container)

- `/data/state/watcher_state.sqlite` — pairs, spend, deliveries,
  lease (WAL mode). Heartbeat sidecar `watcher_state.json.heartbeat`
  (informational only, never a source of truth).
- `/data/runs`, `/data/work` — run/evidence artifacts, checkouts.
- `/data/feedback` — reserved for owner ratings (see M10_FEEDBACK.md).

## First run (on the Docker host)

Account-wide GitHub App (one-time, owner in github.com UI):
private app on the owner account, read-only Contents / Metadata /
Pull requests, installed on the account with All repositories.
Record the numeric App ID; place the .pem on the Docker host
(e.g. /home/mitch/*.private-key.pem, chmod 600) — never paste the key
into chat or the env file.

```sh
cp docker/.env.example docker/codatiel-v010.env
chmod 600 docker/codatiel-v010.env
# fill GITHUB_APP_ID (+ sender/recipient/model keys; SMTP2GO key already
# staged). CODATIEL_WATCH_REPOS stays empty = installation-wide discovery.
docker compose -f docker/docker-compose.yml --env-file docker/codatiel-v010.env build
# one-time activation (refuses if already activated)
docker compose -f docker/docker-compose.yml run --rm watcher watcher activate
# smoke: one poll, no loop
docker compose -f docker/docker-compose.yml run --rm watcher watcher run-once
docker compose -f docker/docker-compose.yml up -d
```

## Operator commands

```sh
# live summary (spend, tracked pairs, queue, lease)
docker exec codatiel-watcher codatiel-i2 watcher status
# single poll (cron/debug)
docker exec codatiel-watcher codatiel-i2 watcher run-once
# directed backfill of exactly one repo+PR (operator only; still gated
# on open/ready/valid SHAs and on budget; eligibility createdAt gate
# bypassed explicitly and logged)
docker exec codatiel-watcher codatiel-i2 watcher backfill --repos o/r --pr 7
# pause / resume a repo (no reactivation, no data loss)
docker exec codatiel-watcher codatiel-i2 watcher pause-repo --repos o/r
docker exec codatiel-watcher codatiel-i2 watcher resume-repo --repos o/r
# resend one run's exact digest bytes (no new inference, idempotent;
# consults on-disk delivery.json too, so crash orphans are adopted)
docker exec codatiel-watcher codatiel-i2 watcher replay-delivery --run-dir /data/runs/...
# reconcile unknown spend with provider-confirmed actuals (evidence note
# required; releases the $1 conservative reservation)
docker exec codatiel-watcher codatiel-i2 watcher reconcile \
  --run-dir /data/runs/... --actual-usd 0.04 --note "provider dashboard ..."
docker logs codatiel-watcher
```

## Spend

Durable caps across restarts: `$CODATIEL_WATCH_SPEND_CAP` total
(default $5 pilot, seeded with $0.4226 already spent: $0.389 M10
research + $0.0336 verified live reviews) and
`$CODATIEL_WATCH_DAILY_CAP`/day (default $2). Each dispatch reserves
$1.00 until actuals land; unknown-usage runs keep the reservation
until reconciled (nothing spends blind). When caps hit, the watcher
stops starting paid reviews and sends exactly one `paused-by-budget`
email. `watcher status` shows `spend_total_usd` + `reserved_usd`.

## Backup / restore

```sh
docker run --rm -v codatiel-v010_codatiel-state:/s -v codatiel-v010_codatiel-runs:/r \
  -v "$PWD":/b alpine tar czf /b/codatiel-backup.tgz /s /r
```

Restore by extracting back into fresh named volumes before `up`.
Restore of an older store never moves the activation boundary backwards:
`activated_at`, once set, is immutable.

## Upgrade / rebuild

Rebuild the image, then `up -d` again; state volumes persist, so no
PR is reviewed twice and no email is resent (delivery records keyed by
run + body hash). Crash orphans are adopted on startup: delivered
receipts are honoured, incomplete work is revalidated (never blindly
re-run or re-sent).

## Feedback

Rate any emailed finding (see `eval/M10_FEEDBACK.md`); use the run ID
and finding number from the email subject/body. Failed emails carry a
FAILED banner and the exact PR URL + SHAs — never a `PR: #?`.

## Do not

- Post anything to GitHub from this service (no CLI path exists for it).
- Copy credentials anywhere except the host env file (600).
- Add repos to the allowlist without owner approval; private repos need
  a data-handling decision before their source reaches providers/email.
- Reset the store to re-run history: historical PRs stay excluded by
  design. Directed backfill exists for explicit operator exceptions.

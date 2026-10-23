# Codatiel v0.1.0 — Operator runbook

One always-on container follows allowlisted GitHub PRs, runs the
qualified Luna-A review (conditional Haiku follow-up), and emails a
private digest per processed revision. GitHub is read-only.

## Layout (inside container)

- `/data/state/watcher_state.json` — poll state, reviewed SHA pairs,
  spend ledger, heartbeat sidecar `watcher_state.json.heartbeat`.
- `/data/runs`, `/data/work` — run/evidence artifacts, checkouts.
- `/data/feedback` — reserved for owner ratings (see M10_FEEDBACK.md).

## First run (on the Docker host)

```sh
cp docker/.env.example docker/codatiel-v010.env
chmod 600 docker/codatiel-v010.env
# fill GH_TOKEN, CODATIEL_WATCH_REPOS, _TO, _SENDER, OPENAI/ANTHROPIC,
# SMTP2GO_API_KEY
docker compose -f docker/docker-compose.yml --env-file docker/codatiel-v010.env build
# smoke: one poll, no loop
docker compose -f docker/docker-compose.yml run --rm watcher watcher run-once
docker compose -f docker/docker-compose.yml up -d
```

## Operator commands

```sh
# live summary (spend, tracked PRs, paused flag)
docker exec codatiel-watcher codatiel-i2 watcher status
# single poll (cron/debug)
docker exec codatiel-watcher codatiel-i2 watcher run-once
# process everything open right now (ignores debounce once)
docker exec codatiel-watcher codatiel-i2 watcher backfill
# resend one run's exact digest bytes (no new inference, idempotent)
docker exec codatiel-watcher codatiel-i2 watcher replay-delivery --run-dir /data/runs/...
docker logs codatiel-watcher
```

## Spend

Durable caps across restarts: `$CODATIEL_WATCH_SPEND_CAP` total
(default $5 pilot, seeded with $0.389 already spent) and
`$CODATIEL_WATCH_DAILY_CAP`/day (default $2). When hit, the watcher
stops starting paid reviews and sends exactly one `paused-by-budget`
email. `watcher status` shows `spend_total_usd`.

## Backup / restore

```sh
docker run --rm -v codatiel-state:/s -v codatiel-runs:/r \
  -v "$PWD":/b alpine tar czf /b/codatiel-backup.tgz /s /r
```

Restore by extracting back into fresh named volumes before `up`.

## Upgrade / rebuild

Rebuild the image, then `up -d` again; state volumes persist, so no
PR is reviewed twice and no email is resent (delivery records keyed by
run + body hash).

## Feedback

Rate any emailed finding (see `eval/M10_FEEDBACK.md`); use the run ID
and finding number from the email subject/body.

## Do not

- Post anything to GitHub from this service (no CLI path exists for it).
- Copy credentials anywhere except the host env file (600).
- Add repos to the allowlist without owner approval; private repos need
  a data-handling decision before their source reaches providers/email.

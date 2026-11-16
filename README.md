<p><img src="docs/assets/logo/logo-wordmark.svg" alt="" height="56"></p>

# Codatiel I2

Codatiel I2 is an experimental, agentic, repository-aware code reviewer.

It investigates pull requests with the OpenAI Agents SDK: a model directs
revision-bound source tools (read, diff, search, definitions, callers)
against pinned base and head revisions, and every cited piece of evidence is
replayed and validated before any finding is reported.

Status: review usefulness is still under qualification. Findings are
advisory only — Codatiel never posts automatic public PR comments, and
nothing here is production-ready. An 80% real-material-useful bar for
public comments is a goal, not a claim.

## Operator dashboard (DASH-01)

A self-hosted, read-only-first operator dashboard for the event-pipeline
worker (TEST/DEV). FastAPI + Uvicorn + Jinja2, server-rendered HTML, no
frontend build pipeline. Views:

- `/` — poller status (running/idle/stale/error with the rule shown),
  last successful poll, spend vs Codatiel limits and remaining budgets
  (Codatiel ledger ONLY — never presented as total OpenAI spend),
  review queue grouped by reason (budget-blocked is never shown as
  completed), decisions by reason, measured cache/stale efficiency,
  publications, spend by model, digest email outcomes, recent activity
  with GitHub links.
- `/repos` — per-repo queue: enabled flag, tracked PRs, latest activity
  and reason, spend; click through to PRs.
- `/pr/{repo}/{pr}` — chronological review history: checkpoint, findings
  generated vs posted vs suppressed-as-previously-delivered, delivery
  decisions with skip/failure reasons and cache-reuse data,
  publications.
- `/spend` — totals, daily ledger series, by repo, by PR, memo stage
  breakdown (informational, never additive), superseded spend kept
  separate, unknown cost counted never priced, applicable ceilings.
- `/efficiency` — COST-01B benefits from measured records (pairs
  examined/reused/cache hits, hit %, stale prevented, posted receipts);
  estimated avoided spend is shown as Unavailable, never invented.
- `/ops` — recent ingestions (filterable by state), budget refusals,
  bounded budget retries, review failures, publication failures,
  configuration changes. Auto-refreshes modestly.
- `/settings` — every `operator.json` control with validation errors
  shown and the audit trail visible; writes are validated,
  CSRF-protected, and audit-logged.

The dashboard opens `events.sqlite` read-only and never triggers
inference, comments, or poller changes. A JSON API under `/api/*`
exposes the same collectors. See `docs/dash-01/deployment.md` for
installation, loopback/SSH access, and an authenticated reverse-proxy
example.

Settings live in `[state_dir]/operator.json` (beside `events.sqlite`),
written atomically and audit-logged to
`[state_dir]/operator-audit.jsonl`. Absent file = env configuration only.
Precedence: spending limits resolve to the MINIMUM of env and file (a
stale file can never raise limits); repo allowlist resolves to the
intersection (file can only narrow); notification destinations use an
explicitly set file value else env; model profile is file else
`REVIEW_PIPELINE`, restricted to the fixed choices `terra_only` /
`terra_luna_additive`, which cannot bypass spend/review contracts. The
upstream OpenAI project limit is an operator-entered DISPLAY-ONLY field
with a billing link — never fetched, never enforced, never conflated
with the Codatiel ledger. Secrets stay in env files: the dashboard never
reads `*_KEY`/`*_TOKEN` values and masks them if config keys are shown.

Run it (binds 127.0.0.1 only, port configurable, default 8487):

```
codatiel-i2 dashboard --port 8487
# state dir: --state-dir, else CODATIEL_DEV_STATE / CODATIEL_TEST_STATE
```

Reach it over SSH (nothing listens beyond loopback):

```
ssh -L 8487:127.0.0.1:8487 operator@codatiel-host
# then open http://127.0.0.1:8487/
```

What it cannot do:

- Change review behaviour itself: it only edits `operator.json`; the
  worker applies it on its next `events` invocation.
- Raise spend limits above the env configuration, widen the repo
  allowlist, or select a model outside the fixed choices.
- Post GitHub PR comments (no code path exists), send mail to anyone but
  the configured TEST/DEV recipients, or read secret values.
- Rewrite history: settings changes are append-audited; spend/decision
  figures are read from the immutable event ledger.


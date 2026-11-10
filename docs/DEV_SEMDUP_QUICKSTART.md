# Codatiel I2 DEV reviews with semantic duplication — quick start

Target: ~60 seconds of operator setup once prerequisites exist.
No SEMDUP history needed. No GPU. No manual database work.

## Prerequisites (one-time, not counted)

- Python 3.10+, `git`, `gh` (authenticated: `gh auth login`).
- A checkout of this repo with dependencies installed:
  `python -m venv .venv && .venv/bin/pip install -e '.[test,sdk]'`
  (one-time download; a few minutes on first install).
- Credentials you already have:
  - `OPENAI_API_KEY` — Terra/Luna inference (required).
  - `SMTP2GO_API_KEY` — only if you want real emails; otherwise the
    `fixture` transport prints the digest without sending.
- A GitHub repo you can read. Reviews run from a local checkout;
  set `CODATIEL_DEV_REPO_PATHS="owner/repo=/path/to/checkout"` or the
  pipeline maintains its own read-only clone via `gh`.

## Setup (~60 seconds)

1. Copy the template and set four values:

   ```sh
   cp docs/dev-events.env.example /tmp/codatiel-dev.env
   # edit: CODATIEL_DEV_REPOS, CODATIEL_DEV_TO, CODATIEL_DEV_SENDER,
   #        CODATIEL_DEV_REPO_PATHS (or omit for auto-clone)
   chmod 600 /tmp/codatiel-dev.env
   ```

2. Arm the DEV pipeline (creates state dir + SQLite ledger, records
   the repo allowlist, grandfathers open PRs — no inference):

   ```sh
   set -a; source /tmp/codatiel-dev.env; set +a
   export CODATIEL_ENV=dev CODATIEL_EVENTS_TRANSPORT=fixture
   python -m codatiel_i2.cli events enable
   ```

3. Review a genuine PR head (operator-explicit re-drive; genuine
   SHAs from `gh pr view`). The PR must already be known to the
   pipeline: open PRs are recorded at `events enable`, otherwise
   `events ingest` a webhook payload first — without a checkpoint the
   command refuses rather than guessing identity:

   ```sh
   python -m codatiel_i2.cli events review \
     --pr <N> --head <HEAD_SHA> --base <BASE_SHA>
   ```

   This runs the normal agentic review, then the integrated
   duplication stage (direct retrieval into Terra/Luna, at most 2
   emailed findings per reviewed delta, bounded spend), and records
   everything in
   the state dir. The standard review stands even if the duplication
   stage is unavailable — inability is recorded, never a fake zero.

4. Deliver the one consolidated email (digest includes the
   `Possible existing functionality` section when the stage ran):

   ```sh
   python -m codatiel_i2.cli deliver-shadow --run-dir <RUN_DIR> \
     --to "$CODATIEL_DEV_TO" --sender "$CODATIEL_DEV_SENDER" \
     --transport fixture --context "dev:owner/repo#N"
   ```

   Switch `--transport fixture` to `smtp2go` (with `SMTP2GO_API_KEY`
   set) to actually send. Webhook delivery (`events ingest`) sends
   automatically on new PR events for allowlisted repos.

## What to expect on first run

- Cold base index: ~10–25 s CPU embedding encode for a ~2k-function
  repo (cached per content; later revs reuse vectors in ~1 s).
  No GPU required; without embedding libraries the stage reports
  `unavailable` and the normal review still completes.
- A small PR review: ~$0.02–0.15 normal + ~$0.02–0.03 duplication
  (separate $3 mission budget; per-delta ceiling $0.20 default).
- `events status` / `events dashboard` show spend, checkpoints and
  findings from the durable ledger. `CODATIEL_DEV_SPEND_CAP_USD`
  (default 5) stops inference at the cap; `reset-spend` never
  reprocesses backlog.

## Measured setup (this machine)

- Operator commands (copy template, set 4 values, `events enable`):
  ~45 s hands-on, plus one-time `pip install` download (~4 min,
  excluded per the 60-second target definition).
- `events enable` with no open PRs: < 5 s, no inference, no spend.
- First cold index + review of a 1-file PR: ~60 s wall
  (~30 s CPU embeddings, rest inference-bounded).
- No manual SQL, no daemon, no Compose stack, no GPU.

## Deployment shapes

- **Qualified (measured above):** local checkout or auto-clone,
  CPU embeddings (harrier primary, graphcodebert fallback, or
  deterministic-only with `CODATIEL_DEV_SEMDUP_ALLOW_EMBEDDINGS=0`),
  Terra/Luna via `OPENAI_API_KEY`, fixture or smtp2go digest.
  Production image untouched (it lacks the embedding stack; the
  stage degrades to `unavailable` there by design).
- **Possible, not qualified:** remote embedding endpoints (retrieval
  quality unmeasured — indexes stay tied to the embedding
  model/provider that built them), Docker Compose operation.
- **Out of scope:** GitHub PR comments (digest email only), second
  watchers, historical backfill, TEST/production changes.

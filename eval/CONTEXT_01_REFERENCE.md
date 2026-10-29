# CONTEXT-01 reference sets (no inference; frozen heads)

Source-verified before any paid pilot run. Post-hoc findings are
tagged `POST_HOC` during adjudication, never retrofitted.

## M9.2 development cases (6) — from eval/M9_2_ADJUDICATION.json

| Case | Repo / PR | Base → Head | Ref finding | Verdict |
|---|---|---|---|---|
| FW3 | frigate-watchdog#3 | 956f7445→d9b61df6 | FW3-reboots / FW3-median | CONFIRMED |
| KL1 | kinglet-lms#1 | dbf06e00→694269d5 | KL1-readme-totals | CONFIRMED |
| L08 | scs-infra#4 | aa807b86→aff2a068 | L08-lms / L08-epcore / L08-403 | CONFIRMED |
| L06 | my-day-my-way-ios#11 | 6509400c→4051ff05 | L06-trim | CONFIRMED |
| L10 | needletail-harness#4 | 08847b4c→80bf493d | L10-evidenceTier | CONFIRMED |
| WT4 | wall-tidbits#4 | 93336223→f053c2f8 | (clean) silence | correct |

Full adjudication, materiality and blind items: `eval/M9_2_ADJUDICATION.md`
and `eval/M9_2_ADJUDICATION.json` (9-rep provenance). No change.

## PR #15 — yt2wiki @ 58c26e20 (base c45c2ec6)

Frozen pair verified locally (`/tmp/yt2wiki`):

- Base `c45c2ec6e43faa43a136ce49ed16197aaf6a144b` → head
  `58c26e20e0fc4c764fd224b3a562ddf08e38be7a` (34 files; bundle 87–90K chars).

Historical model findings (5 runs, raw `model_findings_raw.jsonl`):

| ID | Detector | Path (head) | Lines | Title |
|---|---|---|---|---|
| P15-1 | contracts | src/yt2wiki/web/app.py | 370–377 | Submission preflight can queue jobs without a qualified vision backend |
| P15-2a | operating_constraints | src/yt2wiki/web/inference.py | 226–227 | ASR URL alone bypasses the submission readiness gate |
| P15-2b | docs_code | docs/deploy.md | 194–195 | Portainer password instructions do not inject the password into the container |
| P15-3a | operating_constraints | src/yt2wiki/web/inference.py | 231–244 | Default Compose setup reports transcription ready while its ASR service is disabled |
| P15-3b | docs_code | docs/deploy.md | 193–196 | Portainer password instructions do not inject the password into the container (same concern as P15-2b, adjacent lines) |
| P15-4 | operating_constraints | src/yt2wiki/web/app.py | 373–389 | Job submission can proceed without a ready image-judging provider |
| P15-5 | contracts | src/yt2wiki/web/inference.py | 243–245 | Configured ASR URL is reported Ready without a server check |

Distinct concerns: 4 (ASR readiness, vision-backend preflight, image-judge
preflight, Portainer password). P15-3b is a duplicate of P15-2b on the
same doc lines and is not counted separately. All excerpts replay against
the pinned head checkout at the cited lines (byte-identical). H4 (run
`a24f5773`) was a 0-finding run on the same pair — included in the
variation analysis.

Reference for CONTEXT-01: the 4 distinct concerns above. Findings citing
other evidence are adjudicated later and tagged `POST_HOC`.

## Cases NOT in the pilot

PR #16 (fe9118a) is excluded: the owner authorised "a frozen PR #15
head" plus the 6 M9.2 cases (7 total). No additional adjudication is
needed before the pilot.

## Decision rule (recorded before any run; per CONTEXT-01 brief)

Arm B/C is better than A iff all of:
- reference concerns covered per review is ≥1.3× A's and at least +2
  concern-reps in absolute terms;
- false findings per review not above A's +0.1;
- no concern found in ≥2/3 reps is lost in all reps (n/a for 1 rep;
  pilot reports per case, no significance claims);
- incompletes no worse.

The pilot uses 1 rep; the decision run (≥3 reps) needs separate
go-ahead after pilot cost recalculation.

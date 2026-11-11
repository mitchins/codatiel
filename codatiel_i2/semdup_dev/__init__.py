"""CODATIEL-DEV-01: separate semantic-duplication worker (report-only).

Uses the M11-qualified investigation engine
(`codatiel_i2.semdup_vendored.semdup`, pinned at the SHA recorded in
`../semdup_vendored/VENDORING.md`) as a library. Never the older
`audit.py` assessment pipeline, never `check.run_check` (which rebuilds
both revision indexes and runs the superseded inference path).

Consumes the final challenged `assessment` only. The preliminary
Stage-C `verdict` is recorded as preliminary and is never published as
an actionable recommendation.

Report-only: no GitHub comments, no autonomous refactoring, no
feedback into the TEST Terra+Luna reviewer or production.
"""

SEMDUP_DEV_KIND = "semdup-dev"
SEMDUP_DEV_DETECTOR = "semdup-dev-01"

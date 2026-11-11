"""GitHub publish gate (report-only in Phase A.5; closed until precision gates pass).

Full trust context (ledgers + checkouts + changed set + run SHAs) routes
through the central validator in verify/trust.py. Without it, NOTHING is
listed as publishable: syntactic-only rows are returned separately as
`candidates_preview` so no caller can mistake them for a publish list.
"""

from __future__ import annotations

from codatiel_i2.core.findings import validate_finding
from codatiel_i2.verify.trust import validate_verified_finding

PUBLISH_CAP_TOTAL = 5


def publish_gate(findings: list[dict], *, precision_gate_passed: bool = False,
                 evidence_by_id: dict | None = None,
                 verification_by_candidate: dict | None = None,
                 base_checkout=None, head_checkout=None,
                 changed_paths: set[str] | None = None,
                 expected_base_sha: str | None = None,
                 expected_head_sha: str | None = None) -> dict:
    if not precision_gate_passed:
        return {"mode": "report_only", "publishable": [], "reason": "precision gates not yet passed"}
    full_context = (evidence_by_id is not None and base_checkout is not None
                    and head_checkout is not None and changed_paths is not None
                    and expected_base_sha and expected_head_sha)
    if not full_context:
        # Fail closed: without artifact context there is no publish list.
        syntactic = [f for f in findings if f.get("publish_eligible") and not validate_finding(f)]
        return {"mode": "report_only", "publishable": [],
                "syntactic_candidates": syntactic[:PUBLISH_CAP_TOTAL],
                "validation": "incomplete_context",
                "reason": "trust context missing; nothing is publishable"}
    eligible = []
    context_ok = True
    for f in findings:
        if not f.get("publish_eligible"):
            continue
        errors = validate_verified_finding(
            f, evidence_by_id=evidence_by_id,
            verification_by_candidate=verification_by_candidate,
            base_checkout=base_checkout, head_checkout=head_checkout,
            changed_paths=changed_paths,
            expected_base_sha=expected_base_sha, expected_head_sha=expected_head_sha)
        if any(e.startswith("trust validation requires") for e in errors):
            context_ok = False
        if not errors:
            eligible.append(f)
    return {"mode": "report_only", "publishable": eligible[:PUBLISH_CAP_TOTAL],
            "truncated": len(eligible) > PUBLISH_CAP_TOTAL,
            "validation": "artifact_backed" if context_ok else "incomplete_context",
            "reason": "Phase A.5: preview only, no GitHub mutation"}


def publication_eligibility(*, claims: list[dict], bound_repo: str,
                            bound_pr: int, reviewed_head_sha: str,
                            current_head_sha: str,
                            changed_paths: set[str] | None = None,
                            existing_threads: list[dict] | None = None,
                            superseded_by: int | None = None) -> dict:
    """Decide per-claim publication eligibility (IDENTITY-01).

    Pure function, no network, no GitHub mutation. Comments stay
    disabled; a claim is listed as ``publishable`` only when every
    condition below holds, otherwise it routes email-only with an
    explicit reason:

    - the PR is not superseded (``superseded_by`` set, e.g. YT2WIKI
      #38 replacing #35-37, routes everything email-only);
    - the reviewed head is still the PR's current head (a moved head
      means the claim's lines may be stale — email-only);
    - the claim's path is in the current changed set when known; an
      UNKNOWN changed set (None) is ambiguous — email-only, never
      publish;
    - no existing review thread already carries the same claim key
      (already-reported claims are never re-posted, whether the thread
      is open or resolved);
    - ambiguous cases (unknown current head, unknown changed set,
      unparseable claim key) route email-only, never publish.

    Claim keys are ``(path, start_line, end_line, title)`` tuples.
    Thread entries carry ``key`` tuples; any thread (open or resolved)
    carrying the same key suppresses re-posting. ``bound_repo`` /
    ``bound_pr`` scope the check to one bound review; the function
    verifies nothing itself — callers pass the already-bound values.

    Design note (IDENTITY-01 task 5): this is the eventual
    publication check. It has no production caller yet and posting
    stays disabled; it exists so the head/threads/supersession rules
    are reviewed and tested before any publication path is built.
    """
    existing_threads = existing_threads or []
    email_only: list[dict] = []
    publishable: list[dict] = []

    def _route(claim: dict, reason: str) -> None:
        email_only.append({"claim": claim.get("claim_id")
                           or claim.get("finding_id") or "?",
                           "reason": reason})

    if superseded_by is not None:
        for claim in claims:
            _route(claim, f"PR #{bound_pr} superseded by "
                          f"#{superseded_by}; digest is email-only")
        return {"mode": "report_only", "bound": f"{bound_repo}#{bound_pr}",
                "publishable": [], "email_only": email_only,
                "reason": "superseded PR: no publication check passes"}
    if not current_head_sha:
        for claim in claims:
            _route(claim, "current PR head unknown; ambiguous, email-only")
        return {"mode": "report_only", "bound": f"{bound_repo}#{bound_pr}",
                "publishable": [], "email_only": email_only,
                "reason": "ambiguous: current head unknown"}
    if current_head_sha != reviewed_head_sha:
        for claim in claims:
            _route(claim, f"PR head moved since review "
                          f"({reviewed_head_sha[:12]} -> "
                          f"{current_head_sha[:12]}); claim may be stale, "
                          f"email-only")
        return {"mode": "report_only", "bound": f"{bound_repo}#{bound_pr}",
                "publishable": [], "email_only": email_only,
                "reason": "head moved after review: all claims email-only"}

    reported_keys: set[tuple] = set()
    for thread in existing_threads:
        key = thread.get("key")
        if isinstance(key, (list, tuple)) and len(key) == 4:
            reported_keys.add(tuple(key))
    for claim in claims:
        anchor = claim.get("changed_anchor") or {}
        path = str(anchor.get("path") or "")
        try:
            start = int(anchor.get("line_start") or 0)
            end = int(anchor.get("line_end") or start)
        except (TypeError, ValueError):
            _route(claim, "claim anchor unparseable; ambiguous, email-only")
            continue
        title = str((claim.get("raw") or {}).get("title")
                    or claim.get("title") or "")
        if not path or not title:
            _route(claim, "claim key incomplete; ambiguous, email-only")
            continue
        if changed_paths is None:
            _route(claim, "PR changed set unknown; ambiguous, email-only")
            continue
        if path not in changed_paths:
            _route(claim, f"{path} not in the PR's current changed set; "
                          f"already fixed or out of scope, suppressed "
                          f"(email record only)")
            continue
        if (path, start, end, title) in reported_keys:
            _route(claim, "same claim already reported in an existing "
                          "review thread; never re-posted")
            continue
        publishable.append(claim)
    return {"mode": "report_only", "bound": f"{bound_repo}#{bound_pr}",
            "publishable": publishable, "email_only": email_only,
            "reason": ("publication check evaluated; posting remains "
                       "disabled, publishable list is advisory only")}

"""`semdup` CLI dispatch: doctor (endpoint discovery) + index (M1).

`doctor` never fabricates model responses: with no configured endpoint it
reports explicit unavailable status. With a base URL it queries
`/v1/models`, records the served ID, and compares against an explicit
expected ID only when one is configured.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def doctor() -> int:
    base = _env("SEMDUP_JUDGE_URL")
    expected = _env("SEMDUP_JUDGE_MODEL")
    report: dict = {"role": "judge", "base_url": base, "expected_model_id": expected}
    if not base:
        report.update(status="unavailable", reason="SEMDUP_JUDGE_URL unset (M0/M1 require no endpoint)")
        print(json.dumps(report, indent=2))
        return 0
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        report.update(status="unreachable", error=str(exc))
        print(json.dumps(report, indent=2))
        return 0
    ids = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
    report.update(status="reachable", served_ids=ids)
    if expected is not None:
        report["identity_match"] = expected in ids
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semdup")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    index_p = sub.add_parser("index")
    index_p.add_argument("--repo", type=Path, required=True)
    index_p.add_argument("--db", type=Path, required=True)
    audit_p = sub.add_parser("audit")
    audit_p.add_argument("--repo", type=Path, required=True)
    audit_p.add_argument("--db", type=Path, required=True)
    audit_p.add_argument("--output", type=Path, required=True)
    audit_p.add_argument("--ledger", type=Path, required=True)
    audit_p.add_argument("--max-families", type=int, default=10)
    audit_p.add_argument("--max-challenge-calls", type=int, default=20)
    audit_p.add_argument("--feedback-path", type=Path, default=None)
    check_p = sub.add_parser("check")
    check_p.add_argument("--repo", type=Path, required=True)
    check_p.add_argument("--base", type=str, required=True)
    check_p.add_argument("--head", type=str, required=True)
    check_p.add_argument("--output", type=Path, required=True)
    check_p.add_argument("--ledger", type=Path, required=True)
    check_p.add_argument("--max-families", type=int, default=20)
    check_p.add_argument("--max-challenge-calls", type=int, default=20)
    lookup_p = sub.add_parser("lookup")
    lookup_p.add_argument("--repo", type=Path, required=True)
    lookup_p.add_argument("--db", type=Path, required=True)
    lookup_p.add_argument("--intent", type=str, required=True)
    lookup_p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "index":
        from .index_cmd import build

        print(json.dumps(build(args.repo, args.db), indent=2))
        return 0
    import os as _os

    if args.cmd == "check":
        from . import check as _check

        judge_url = _os.environ.get("SEMDUP_JUDGE_URL", "")
        if not judge_url:
            print("no endpoint configured (SEMDUP_JUDGE_URL unset); refusing to fabricate")
            return 3
        out = _check.run_check(
            args.repo, args.base, args.head, args.output, args.ledger,
            judge_url=judge_url, judge_model=_os.environ.get("SEMDUP_JUDGE_MODEL", ""),
            judge_key=_os.environ.get("SEMDUP_JUDGE_KEY"),
            judge_expected=_os.environ.get("SEMDUP_JUDGE_EXPECTED"),
            challenge_url=_os.environ.get("SEMDUP_LARGE_URL"),
            challenge_model=_os.environ.get("SEMDUP_LARGE_MODEL", ""),
            challenge_key=_os.environ.get("SEMDUP_LARGE_KEY"),
            challenge_expected=_os.environ.get("SEMDUP_LARGE_EXPECTED"),
            max_families=args.max_families,
            max_challenge_calls=args.max_challenge_calls)
        print(out["report"])
        return {"complete": 0, "partial": 2, "failed": 3}[out["result"]["status"]]

    if args.cmd == "lookup":
        from . import lookup as _lookup

        result = _lookup.lookup(args.db, args.intent) if args.db.exists() else None
        if result is None:
            from .index_cmd import build as _build

            _build(args.repo, args.db)
            from . import lookup as _lookup2

            result = _lookup2.lookup(args.db, args.intent)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0

    from . import audit as _audit

    judge_url = _os.environ.get("SEMDUP_JUDGE_URL", "")
    if not judge_url:
        print("no endpoint configured (SEMDUP_JUDGE_URL unset); refusing to fabricate")
        return 3
    out = _audit.audit(
        args.repo, args.db, args.output, args.ledger,
        judge_url=judge_url, judge_model=_os.environ.get("SEMDUP_JUDGE_MODEL", ""),
        judge_key=_os.environ.get("SEMDUP_JUDGE_KEY"),
        judge_expected=_os.environ.get("SEMDUP_JUDGE_EXPECTED"),
        challenge_url=_os.environ.get("SEMDUP_LARGE_URL"),
        challenge_model=_os.environ.get("SEMDUP_LARGE_MODEL", ""),
        challenge_key=_os.environ.get("SEMDUP_LARGE_KEY"),
        challenge_expected=_os.environ.get("SEMDUP_LARGE_EXPECTED"),
        max_families=args.max_families,
        max_challenge_calls=args.max_challenge_calls,
        feedback_path=args.feedback_path,
        fast_url=_os.environ.get("SEMDUP_FAST_URL"),
        fast_model=_os.environ.get("SEMDUP_FAST_MODEL", ""),
        fast_key=_os.environ.get("SEMDUP_FAST_KEY"),
        fast_expected=_os.environ.get("SEMDUP_FAST_EXPECTED"))
    print(out["report"])
    return {"complete": 0, "partial": 2, "failed": 3}[out["result"]["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

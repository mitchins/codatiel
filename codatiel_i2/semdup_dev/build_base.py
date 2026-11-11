"""Async base-index builder entry: never blocks the standard reviewer.

Usage: python3 -m codatiel_i2.semdup_dev.build_base <repo_path> <repo>
       <rev> <state_dir> <allow_embeddings 0|1>
"""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    repo_path, repo, rev, state_dir = (Path(argv[0]), argv[1], argv[2],
                                      Path(argv[3]))
    allow = (argv[4] if len(argv) > 4 else "1") not in ("0", "no", "false")
    from codatiel_i2.semdup_dev import index_state as IX
    try:
        out = IX.ensure_base_index(repo_path, repo, rev, state_dir,
                                   allow_embeddings=allow)
        print({"reused": out.get("reused"), "present": out.get("present"),
               "reason": out.get("reason", "")})
        return 0 if out.get("present") else 1
    except Exception as exc:  # noqa: BLE001 - logged by the caller redirect
        print(f"base build failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

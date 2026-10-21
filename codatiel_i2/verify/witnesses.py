"""Executable witness contract (framework vocabulary; trust-gated later).

The kinds below name future proof classes. NONE is accepted by the trust
validator today (core/findings.py VERIFIED_KINDS is empty until detector
proof validators exist); the kinds below re-enable one at a time when
their artifact store/executor replays.
"""

from __future__ import annotations


def witness_result(*, kind: str, passed: bool, detail: str = "") -> dict:
    if kind not in ("executable_witness", "deterministic_contradiction", "native_analyzer",
                    "compiler_failure", "schema_checker", "code_path_proof", "none"):
        raise ValueError(f"unknown witness kind: {kind!r}")
    return {"kind": kind, "passed": passed, "detail": detail}

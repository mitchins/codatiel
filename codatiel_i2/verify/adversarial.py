"""Adversarial verification prompt contract (§14).

Instruction: your job is to kill this finding. Look for incorrect
assumptions, downstream safeguards, intentional asymmetry, framework
behaviour that makes it safe, base/head mismatch errors, evidence that the
trigger is impossible. Return survives | refuted | uncertain with exact
evidence quotes. Model-only agreement (level 5) is never sufficient alone
for publication.
"""

from __future__ import annotations

ADVERSARIAL_INSTRUCTION = (
    "Your job is to kill this finding.\n"
    "Look for:\n"
    "- incorrect assumptions\n"
    "- downstream safeguards\n"
    "- intentional asymmetry\n"
    "- framework behaviour that makes it safe\n"
    "- base/head mismatch errors\n"
    "- evidence that the trigger is impossible\n"
    "Return: survives | refuted | uncertain.\n"
    "Quote exact evidence."
)


def adversarial_packet(detector: str, invariant: str, changed: dict, counterpart: dict | None,
                       base_values: str, head_values: str) -> list[dict]:
    return [
        {"role": "system", "content": ADVERSARIAL_INSTRUCTION},
        {"role": "user", "content": (
            f"Detector: {detector}\nInvariant: {invariant}\n"
            f"Changed: {changed}\nCounterpart: {counterpart}\n"
            f"Base: {base_values}\nHead: {head_values}\n"
            "Task: Try to refute the proposed contradiction. "
            "Reply with status survives|refuted|uncertain plus quoted evidence."
        )},
    ]

"""PR overlay: main index @ base SHA + PR overlay @ head SHA (Phase B skeleton)."""

from __future__ import annotations

from pathlib import Path


def overlay_status(base_sha: str, head_sha: str, changed_paths: list[str]) -> dict:
    return {
        "schema_version": "codatiel_i2.index_overlay.v1",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_paths": sorted(changed_paths),
        "strategy": "base_index_plus_head_overlay",
    }

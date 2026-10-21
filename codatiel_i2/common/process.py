"""Process helpers."""

from __future__ import annotations

import subprocess


def check_output(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr.strip() or proc.stdout.strip() or "command failed")[:2000])
    return proc.stdout

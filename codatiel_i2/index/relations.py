"""Relations helper (Phase B skeleton). Allowed relations (§10)."""

from __future__ import annotations

ALLOWED = (
    "defines", "references", "calls", "documents", "serializes",
    "deserializes", "persists", "migrates", "guards", "authorizes",
    "mutates", "captures",
)


def add_relation(conn, repo_id: str, commit_sha: str, src: str, rel: str, dst: str) -> None:
    if rel not in ALLOWED:
        raise ValueError(f"unknown relation: {rel!r}")
    conn.execute(
        "INSERT OR IGNORE INTO relation (repo_id, commit_sha, src, rel, dst) VALUES (?,?,?,?,?)",
        (repo_id, commit_sha, src, rel, dst),
    )
    conn.commit()

"""Exact index lookup (Phase B skeleton)."""

from __future__ import annotations


def lookup_symbol(conn, repo_id: str, commit_sha: str, entity_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT path, entity_type, entity_id, line_start, line_end FROM symbol"
        " WHERE repo_id=? AND commit_sha=? AND entity_id=?",
        (repo_id, commit_sha, entity_id),
    ).fetchall()
    return [{"path": r[0], "entity_type": r[1], "entity_id": r[2],
             "line_start": r[3], "line_end": r[4]} for r in rows]


def lookup_file(conn, repo_id: str, commit_sha: str, path: str) -> dict | None:
    row = conn.execute(
        "SELECT path, content_hash, language, line_count FROM file"
        " WHERE repo_id=? AND commit_sha=? AND path=?",
        (repo_id, commit_sha, path),
    ).fetchone()
    if not row:
        return None
    return {"path": row[0], "content_hash": row[1], "language": row[2], "line_count": row[3]}


def symbol_text(conn, repo_id: str, commit_sha: str, entity_id: str, path: str | None = None) -> str | None:
    if path is None:
        row = conn.execute(
            "SELECT text FROM symbol WHERE repo_id=? AND commit_sha=? AND entity_id=? ORDER BY path",
            (repo_id, commit_sha, entity_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT text FROM symbol WHERE repo_id=? AND commit_sha=? AND entity_id=? AND path=?",
            (repo_id, commit_sha, entity_id, path),
        ).fetchone()
    return row[0] if row else None

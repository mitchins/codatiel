"""SQLite index schema (Phase B skeleton; full build lands in Phase B).

Tables: repository_version, file, symbol, doc_chunk, config_key,
schema_entity, route, relation, embedding.
"""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS repository_version (
  repo_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, commit_sha)
);
CREATE TABLE IF NOT EXISTS file (
  repo_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT '',
  line_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (repo_id, commit_sha, path)
);
CREATE TABLE IF NOT EXISTS symbol (
  repo_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  path TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  language TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (repo_id, commit_sha, path, entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS doc_chunk (
  repo_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  path TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (repo_id, commit_sha, path, entity_id)
);
CREATE TABLE IF NOT EXISTS relation (
  repo_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  src TEXT NOT NULL,
  rel TEXT NOT NULL,
  dst TEXT NOT NULL,
  PRIMARY KEY (repo_id, commit_sha, src, rel, dst)
);
CREATE TABLE IF NOT EXISTS embedding (
  repo_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  embedding_model_id TEXT NOT NULL,
  embedding_dimension INTEGER NOT NULL,
  embedding_version TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  vector_blob BLOB NOT NULL,
  PRIMARY KEY (repo_id, commit_sha, entity_id, embedding_model_id)
);
"""

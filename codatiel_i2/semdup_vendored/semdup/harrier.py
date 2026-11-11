"""Second embedding channel: microsoft/harrier-oss-v1-0.6b (frozen comparison).

Instruction-aware retrieval model (1024-d, last-token pooling, cosine).
Protocol for the M4/M5 comparison, FROZEN — do not tune these without a
new recorded arm:
- Documents (indexed units): UNINSTRUCTED raw text, same format as the
  GraphCodeBERT channel (`path :: symbol\\ntext`) for comparability.
- Queries: instruction-prefixed with QUERY_INSTRUCTION below, i.e.
  `Instruct: <task>\\nQuery: <same doc text>`.
- Sidecar schema identical to embed.py ({ids, vectors, model, sha} +
  instruction + max_length), so `embed.load_index` / `embed.embedding_neighbors`
  serve both channels without a second formula.

No fusion-weight decisions live here (M5.3 territory). This module only
produces the frozen Harrier ranking signal.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

MODEL_ID = "microsoft/harrier-oss-v1-0.6b"
MODEL_DIM = 1024
MAX_TOKENS = 2048
BATCH = 8

# Frozen query instruction: explicit same-maintainable-responsibility
# retrieval. Recorded verbatim in every comparison report.
QUERY_INSTRUCTION = (
    "Given a code function, retrieve candidate functions that implement "
    "the same maintainable responsibility and could be consolidated "
    "through a shared helper, policy, or contract"
)


def query_prompt() -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: "


def available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


class Encoder:
    def __init__(self, model: str = MODEL_ID):
        from sentence_transformers import SentenceTransformer

        self.model_id = model
        self.backend = SentenceTransformer(model, trust_remote_code=False)
        self.backend.max_seq_length = MAX_TOKENS

    def encode_docs(self, texts: list[str]) -> object:
        import numpy as np

        vecs = self.backend.encode(texts, batch_size=BATCH, show_progress_bar=False,
                                   convert_to_numpy=True, normalize_embeddings=True)
        return np.asarray(vecs, dtype="float32")

    def encode_queries(self, texts: list[str]) -> object:
        import numpy as np

        vecs = self.backend.encode(texts, batch_size=BATCH, show_progress_bar=False,
                                   convert_to_numpy=True, normalize_embeddings=True,
                                   prompt=query_prompt())
        return np.asarray(vecs, dtype="float32")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_index(db_path: Path, out_path: Path, model: str = MODEL_ID) -> dict:
    """Encode function-level units (docs UNINSTRUCTED); write sidecar."""
    import time

    import numpy as np
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, path, symbol, text FROM units WHERE kind IN "
            "('function','closure','initializer') ORDER BY id").fetchall()
    finally:
        conn.close()
    encoder = Encoder(model)
    texts = [f"{path} :: {symbol}\n{text}" for _, path, symbol, text in rows]
    t0 = time.time()
    vectors = encoder.encode_docs(texts)
    wall = time.time() - t0
    ids = [r[0] for r in rows]
    np.savez_compressed(out_path, ids=np.array(ids, dtype=np.int64),
                        vectors=vectors.astype("float32"),
                        model=np.array(model), instruction=np.array(QUERY_INSTRUCTION),
                        sha=np.array(
                            _sha("\n".join(f"{i}:{t[:200]}" for i, t in zip(ids, texts)))))
    return {"model": model, "dim": int(vectors.shape[1]), "units": len(ids),
            "bytes": out_path.stat().st_size, "encode_wall_s": round(wall, 1),
            "instruction": QUERY_INSTRUCTION, "max_tokens": MAX_TOKENS}

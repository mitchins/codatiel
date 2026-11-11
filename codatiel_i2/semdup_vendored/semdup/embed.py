"""Embedding retrieval channel (GraphCodeBERT, local, CPU).

Separate channel, independently attributable. Truncation-safe by
construction in `retrieve.all_channel_neighbors`: the deterministic
top-k is frozen before embedding-only extras append, so an embedding
hit can never displace a deterministic candidate from a finite budget.
Vectors live in an `.emb.npz` sidecar next to the SQLite DB
(brute-force cosine — honest at gauntlet scale; ANN deferred with an
explicit note, not silently assumed). Imports are lazy so a parse-only
install without torch/transformers stays usable (capability: unavailable).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_MODEL = "microsoft/graphcodebert-base"
MODEL_DIM = 768
MAX_TOKENS = 256
BATCH = 32


def available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Encoder:
    def __init__(self, model: str = DEFAULT_MODEL):
        from transformers import AutoModel, AutoTokenizer

        self.model_id = model
        self.tokenizer = AutoTokenizer.from_pretrained(model, trust_remote_code=False)
        self.backend = AutoModel.from_pretrained(model, trust_remote_code=False)
        self.backend.eval()

    def encode(self, texts: list[str]) -> object:
        import numpy as np
        import torch

        vectors = []
        with torch.no_grad():
            for i in range(0, max(1, len(texts)), BATCH):
                chunk = texts[i:i + BATCH]
                tokens = self.tokenizer(chunk, padding=True, truncation=True,
                                        max_length=MAX_TOKENS, return_tensors="pt")
                hidden = self.backend(**tokens).last_hidden_state
                mask = tokens["attention_mask"].unsqueeze(-1).expand(hidden.shape).float()
                summed = (hidden * mask).sum(1)
                counts = mask.sum(1).clamp(min=1e-9)
                batch = (summed / counts).cpu().numpy()
                batch = batch / (np.linalg.norm(batch, axis=1, keepdims=True) + 1e-12)
                vectors.append(batch)
        import numpy as np2

        return np2.vstack(vectors) if vectors else np2.zeros((0, MODEL_DIM), dtype="float32")


def build_index(db_path: Path, out_path: Path, model: str = DEFAULT_MODEL) -> dict:
    """Encode function-level units; write sidecar. Returns build report."""
    import json as _json
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
    vectors = encoder.encode(texts)
    ids = [r[0] for r in rows]
    np.savez_compressed(out_path, ids=np.array(ids, dtype=np.int64),
                        vectors=vectors.astype("float32"),
                        model=np.array(model), sha=np.array(
                            _sha("\n".join(f"{i}:{t[:200]}" for i, t in zip(ids, texts)))))
    return {"model": model, "dim": int(vectors.shape[1]), "units": len(ids),
            "bytes": out_path.stat().st_size}


def load_index(path: Path) -> dict | None:
    import numpy as np

    if not path.exists():
        return None
    blob = np.load(str(path), allow_pickle=True)
    return {"ids": [int(i) for i in blob["ids"]], "vectors": blob["vectors"],
            "model": str(blob["model"]), "sha": str(blob["sha"])}


def embedding_neighbors(index: dict | None, unit_id: int, k: int = 50) -> tuple[list[int], dict]:
    """Cosine top-k ids (excluding self). Pure function of the frozen index."""
    import numpy as np

    if index is None or unit_id not in index["ids"]:
        return [], {"embedding": 0, "embedding_error": "no-index" if index is None else "unknown-unit"}
    row = index["ids"].index(unit_id)
    scores = index["vectors"] @ index["vectors"][row]
    order = np.argsort(-scores, kind="stable")
    hits = [int(index["ids"][i]) for i in order if int(index["ids"][i]) != unit_id][:k]
    return hits, {"embedding": len(hits)}

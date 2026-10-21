"""Embeddings interface: backend must be replaceable (§10)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingBackend(ABC):
    model_id: str = ""
    dimension: int = 0
    version: str = "v1"

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


class NullEmbeddingBackend(EmbeddingBackend):
    """Refuses to embed (Phase A default). Never silently mix vector spaces."""

    model_id = "null"
    dimension = 0
    version = "v0"

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no embedding backend configured")

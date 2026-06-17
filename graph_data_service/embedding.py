from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Protocol

import numpy as np


class EmbeddingProvider(Protocol):
    name: str
    available: bool
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return L2-normalized float32 embeddings with shape (N, dim)."""


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", str(text or "").lower())


class _LexicalHasher:
    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        rows = []
        for text in texts:
            vec = np.zeros((self.dim,), dtype=np.float32)
            for tok in _tokenize(text):
                vec[hash(tok) % self.dim] += 1.0
            norm = np.linalg.norm(vec)
            rows.append(vec if norm == 0 else vec / norm)
        return np.vstack(rows) if rows else np.empty((0, self.dim), dtype=np.float32)


class LexicalProvider:
    name = "lexical"
    available = True
    dim = 384

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self._hasher = _LexicalHasher(dim=dim)

    def encode(self, texts: list[str]) -> np.ndarray:
        return self._hasher.encode(texts)


class SentenceTransformerProvider:
    name = "sentence-transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.available = True
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)


class OpenAIEmbeddingProvider:
    name = "openai"
    dim = 1536

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self._api_key = api_key
        self._model = model
        self.available = True

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)

        rows: list[np.ndarray] = []
        for start in range(0, len(texts), 96):
            payload = json.dumps(
                {"model": self._model, "input": texts[start : start + 96]},
                ensure_ascii=False,
            ).encode("utf-8")
            request = urllib.request.Request(
                "https://api.openai.com/v1/embeddings",
                data=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
            batch = sorted(body.get("data", []), key=lambda item: int(item.get("index", 0)))
            for item in batch:
                rows.append(np.asarray(item["embedding"], dtype=np.float32))

        mat = np.vstack(rows) if rows else np.empty((0, self.dim), dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (mat / norms).astype(np.float32)


def default_provider() -> EmbeddingProvider:
    forced = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
    if forced in {"lexical", "hash", "hasher"}:
        return LexicalProvider()
    if forced in {"sentence-transformers", "sentence_transformers", "st"}:
        return SentenceTransformerProvider()
    if forced == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
        return OpenAIEmbeddingProvider(api_key)

    try:
        return SentenceTransformerProvider()
    except ImportError:
        pass
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "SentenceTransformerProvider init failed: %s",
            exc,
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            return OpenAIEmbeddingProvider(api_key)
        except Exception:
            pass

    return LexicalProvider()

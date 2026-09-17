from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np

from graph_data_service.embedding import EmbeddingProvider, default_provider


class LightweightRAG:
    def __init__(self, session_dir: str, provider: EmbeddingProvider | None = None) -> None:
        self.session_dir = Path(session_dir)
        self._provider = provider or default_provider()
        self._refresh_paths()

    @staticmethod
    def current_directory(session_dir: Path) -> Path:
        manifest = session_dir / 'rag_manifest.json'
        if not manifest.exists():
            return session_dir  # Existing document format remains readable.
        version = json.loads(manifest.read_text(encoding='utf-8'))['version']
        if not isinstance(version, str) or not re.fullmatch(r'[a-zA-Z0-9-]+', version):
            raise ValueError('INVALID_RAG_VERSION')
        return session_dir / '.rag_versions' / version

    def _refresh_paths(self):
        self.data_directory = self.current_directory(self.session_dir)
        self.docs_dir = self.data_directory / "docs"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_path = self.data_directory / "doc_embeddings.npy"
        self.index_path = self.data_directory / "doc_index.json"

    def ingest_document(self, doc_id: str, content: str, *, chunk_size: int = 512, overlap: int = 128) -> dict[str, Any]:
        self._refresh_paths()
        chunks = self._chunk_text(content, chunk_size=chunk_size, overlap=overlap)
        embeddings = self._provider.encode([chunk["text"] for chunk in chunks])
        existing = self._load_index()
        start = len(existing)
        for offset, chunk in enumerate(chunks):
            existing.append(
                {
                    "chunk_id": start + offset,
                    "doc_id": doc_id,
                    "offset": offset,
                    "text": chunk["text"],
                }
            )
        current = self._load_embeddings()
        if self._provider.name == 'lexical' and start:
            # Python's lexical hash is process-local. Recompute the old rows in
            # the executing process instead of mixing hash spaces across workers.
            current = self._provider.encode([chunk['text'] for chunk in existing[:start]])
        if current.size and embeddings.size and current.shape[1] != embeddings.shape[1]:
            (self.docs_dir / f"{doc_id}.txt").write_text(content, encoding="utf-8")
            return {
                "success": False,
                "doc_id": doc_id,
                "chunks": len(chunks),
                "retrieval_mode": self._provider.name,
                "semantic_enabled": self._provider.name != "lexical",
                "error": (
                    "Embedding 维度不一致：现有文档使用 "
                    f"{current.shape[1]} 维，当前 provider 输出 {embeddings.shape[1]} 维。"
                    "请先清空会话或重新上传所有文档。"
                ),
            }
        merged = embeddings if current.size == 0 else np.vstack([current, embeddings])
        np.save(self.embedding_path, merged)
        self.index_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.docs_dir / f"{doc_id}.txt").write_text(content, encoding="utf-8")
        return {
            "success": True,
            "doc_id": doc_id,
            "chunks": len(chunks),
            "retrieval_mode": self._provider.name,
            "semantic_enabled": self._provider.name != "lexical",
        }

    def retrieve(self, query: str, top_k: int = 5) -> dict[str, Any]:
        self._refresh_paths()
        index = self._load_index()
        embeddings = self._load_embeddings()
        if not index or embeddings.size == 0:
            return {
                "success": True,
                "chunks": [],
                "retrieval_mode": self._provider.name,
                "semantic_enabled": self._provider.name != "lexical",
            }
        if self._provider.name == 'lexical':
            embeddings = self._provider.encode([chunk['text'] for chunk in index])
        q = self._provider.encode([query])
        if embeddings.shape[1] != q.shape[1]:
            return {
                "success": False,
                "chunks": [],
                "retrieval_mode": self._provider.name,
                "semantic_enabled": self._provider.name != "lexical",
                "error": "文档向量维度与当前 embedding provider 不一致，请重新上传文档。",
            }
        scores = (q @ embeddings.T).flatten()
        top_idx = scores.argsort()[-max(top_k, 1) :][::-1]
        rows = [
            {
                **index[int(idx)],
                "score": round(float(scores[int(idx)]), 4),
            }
            for idx in top_idx
        ]
        return {
            "success": True,
            "chunks": rows,
            "retrieval_mode": self._provider.name,
            "semantic_enabled": self._provider.name != "lexical",
        }

    def _load_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _load_embeddings(self) -> np.ndarray:
        if not self.embedding_path.exists():
            return np.empty((0, self._provider.dim), dtype=np.float32)
        return np.load(self.embedding_path)

    @staticmethod
    def _chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[dict[str, str]]:
        words = re.findall(r"\S+", str(text or ""))
        if not words:
            return []
        chunks: list[dict[str, str]] = []
        step = max(chunk_size - overlap, 1)
        for start in range(0, len(words), step):
            chunk = " ".join(words[start : start + chunk_size]).strip()
            if chunk:
                chunks.append({"text": chunk})
            if start + chunk_size >= len(words):
                break
        return chunks

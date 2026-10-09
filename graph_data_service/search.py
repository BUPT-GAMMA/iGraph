from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import numpy as np

from graph_data_service.embedding import EmbeddingProvider
from graph_data_service.graph_data import GraphData


_MAX_TOP_K = 50
_PREVIEW_CHARS = 180
_ENDPOINT_SUMMARY_CHARS = 100


@dataclass
class NodeSearchRequest:
    query: str
    top_k: int = 8
    filters: dict[str, Any] = field(default_factory=dict)
    sort: dict[str, Any] | None = None
    rerank: bool = True


@dataclass
class EdgeSearchRequest:
    query: str = ""
    src_query: str | None = None
    dst_query: str | None = None
    top_k: int = 8
    filters: dict[str, Any] = field(default_factory=dict)
    sort: dict[str, Any] | None = None
    rerank: bool = True


class EntitySearchIndex:
    def __init__(
        self,
        gd: GraphData,
        graph_version: int,
        provider: EmbeddingProvider,
    ) -> None:
        self._gd = gd
        self._version = graph_version
        self._provider = provider
        self._semantic_enabled = (
            getattr(provider, "name", "") != "lexical"
            and bool(getattr(provider, "available", True))
        )
        self._semantic_error: str | None = None
        self._node_docs: list[dict[str, Any]] = []
        self._edge_docs: list[dict[str, Any]] = []
        self._node_matrix = np.empty((0, getattr(provider, "dim", 384)), dtype=np.float32)
        self._edge_matrix = np.empty((0, getattr(provider, "dim", 384)), dtype=np.float32)
        self._query_vec_cache: dict[str, np.ndarray] = {}
        self._query_vec_cache_lock = Lock()
        self._build()

    def is_stale(self, version: int) -> bool:
        return version != self._version

    def search_nodes(self, req: NodeSearchRequest) -> dict[str, Any]:
        query = str(req.query or "").strip()
        top_k = self._top_k(req.top_k)
        filters = req.filters or {}
        exact_ids = self._exact_node_ids(query)
        exact = bool(exact_ids)

        if exact:
            indices = [idx for idx in exact_ids if self._node_matches_filters(self._node_docs[idx], filters)]
            rows = [
                self._node_candidate(self._node_docs[idx], 1.0, ["exact node identity or display_name match"])
                for idx in indices
            ]
        else:
            indices = [
                doc["node_id"]
                for doc in self._node_docs
                if self._node_matches_filters(doc, filters)
            ]
            rows = [
                self._node_candidate(doc, score, self._node_evidence(doc, filters, query, score))
                for doc, score in self._score_node_indices(indices, query)
            ]

        rows = self._sort_rows(rows, req.sort, default_field="score")[:top_k]
        confidence, ambiguous = self._confidence_and_ambiguity(
            rows,
            query,
            exact,
            has_filters=bool(filters),
        )
        if exact and len(indices) > 1:
            confidence, ambiguous = "medium", True
        best = self._best_node(rows[0], confidence) if rows else None
        return self._result(
            query=query,
            rows=rows,
            best=best,
            rerank_enabled=bool(req.rerank),
            ambiguous=ambiguous,
        )

    def search_edges(self, req: EdgeSearchRequest) -> dict[str, Any]:
        query = str(req.query or "").strip()
        src_query = str(req.src_query or "").strip()
        dst_query = str(req.dst_query or "").strip()
        top_k = self._top_k(req.top_k)
        filters = req.filters or {}
        if not query and not src_query and not dst_query:
            return self._result(
                query=query,
                rows=[],
                best=None,
                rerank_enabled=bool(req.rerank),
                ambiguous=False,
                extra={
                    "success": False,
                    "src_query": None,
                    "dst_query": None,
                    "error": "query、src_query、dst_query 至少需要提供一个。",
                },
            )

        exact_indices = self._exact_edge_indices(query, src_query, dst_query)
        exact = bool(exact_indices)

        if exact:
            rows = [
                self._edge_candidate(
                    self._edge_docs[idx],
                    1.0,
                    ["exact edge match"],
                )
                for idx in exact_indices
                if self._edge_matches_filters(self._edge_docs[idx], filters)
            ]
        elif src_query or dst_query:
            rows = self._search_edges_by_endpoints(query, src_query, dst_query, filters)
        else:
            indices = [
                doc["edge_idx"]
                for doc in self._edge_docs
                if self._edge_matches_filters(doc, filters)
            ]
            rows = [
                self._edge_candidate(doc, score, self._edge_evidence(doc, filters, query, score))
                for doc, score in self._score_edge_indices(indices, query)
            ]

        rows = self._sort_rows(rows, req.sort, default_field="score")[:top_k]
        confidence, ambiguous = self._confidence_and_ambiguity(
            rows,
            query or src_query or dst_query,
            exact,
            has_filters=bool(filters),
        )
        best = self._best_edge(rows[0], confidence) if rows else None
        return self._result(
            query=query,
            rows=rows,
            best=best,
            rerank_enabled=bool(req.rerank),
            ambiguous=ambiguous,
            extra={
                "src_query": src_query or None,
                "dst_query": dst_query or None,
            },
        )

    def _build(self) -> None:
        # Construction graphs retain stable internal IDs; human names live in
        # properties. Index them once without treating distinct aliases or
        # entities with the same name as one node.
        self._display_names: dict[str, str] = {}
        self._name_to_ids: dict[str, list[int]] = {}
        property_graph = self._gd.graph_metadata.get("construction_graph") or {}
        for node in property_graph.get("nodes", []):
            properties = node.get("properties") or {}
            data = properties.get("data")
            name = properties.get("name") or (data.get("name") if isinstance(data, dict) else None)
            if isinstance(name, str) and name.strip():
                self._display_names[str(node["id"])] = name.strip()
        for node_id in range(self._gd.num_nodes):
            name = self._display_names.get(self._node_name(node_id))
            if name:
                self._name_to_ids.setdefault(name, []).append(node_id)
        self._node_docs = [self._build_node_doc(node_id) for node_id in range(self._gd.num_nodes)]
        self._edge_docs = [self._build_edge_doc(edge_idx) for edge_idx in range(self._gd.num_edges)]

        if not self._semantic_enabled:
            return
        try:
            self._node_matrix = self._encode_docs(self._node_docs)
            self._edge_matrix = self._encode_docs(self._edge_docs)
        except Exception as exc:
            self._semantic_enabled = False
            self._semantic_error = str(exc)
            dim = getattr(self._provider, "dim", 384)
            self._node_matrix = np.empty((0, dim), dtype=np.float32)
            self._edge_matrix = np.empty((0, dim), dtype=np.float32)

    def _encode_docs(self, docs: list[dict[str, Any]]) -> np.ndarray:
        if not docs:
            return np.empty((0, getattr(self._provider, "dim", 384)), dtype=np.float32)
        return np.asarray(
            self._provider.encode([str(doc.get("document") or "") for doc in docs]),
            dtype=np.float32,
        )

    @staticmethod
    def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
        if matrix.size == 0:
            return matrix.astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (matrix / norms).astype(np.float32)

    def _build_node_doc(self, node_id: int) -> dict[str, Any]:
        gd = self._gd
        name = self._node_name(node_id)
        display_name = self._display_names.get(name, name)
        node_type = gd.get_node_type_by_id(node_id) if gd.has_node_type else None
        label = self._jsonable_label(gd.get_node_label_by_id(node_id)) if gd.has_node_label else None
        in_degree = gd.in_degree(node_id)
        out_degree = gd.out_degree(node_id)
        degree = gd.degree(node_id)
        total_degree = gd.total_degree(node_id)
        num_neighbors = gd.num_neighbors(node_id)
        neighbors = [self._node_name(nid) for nid in gd.neighbors(node_id)[:5]]
        node_text = gd.node_text[node_id] if gd.has_node_text and gd.node_text else None
        if node_text:
            document = str(node_text)
        elif node_type is not None or label is not None:
            document = f"{name} type={node_type} label={label}"
        else:
            document = name
        if display_name != name:
            document = f"{display_name} {document}"
        return {
            "entity": "node",
            "node_id": node_id,
            "node_name": name,
            "display_name": display_name,
            "node_type": node_type,
            "label": label,
            "in_degree": in_degree,
            "out_degree": out_degree,
            "degree": degree,
            "num_neighbors": num_neighbors,
            "total_degree": total_degree,
            "semantic_type": gd.semantic_type,
            "storage_type": gd.storage_type,
            "neighbors_preview": neighbors,
            "document": document,
        }

    def _build_edge_doc(self, edge_idx: int) -> dict[str, Any]:
        gd = self._gd
        src_arr, dst_arr = gd.edge_index
        src = int(src_arr[edge_idx])
        dst = int(dst_arr[edge_idx])
        entry = gd.get_edge(src, dst) or {}
        src_name = self._node_name(src)
        dst_name = self._node_name(dst)
        edge_name = entry.get("name")
        edge_type = entry.get("type")
        label = self._jsonable_label(entry.get("label"))
        weight = float(entry.get("weight") if entry.get("weight") is not None else 1.0)
        edge_text = gd.edge_text[edge_idx] if gd.has_edge_text and gd.edge_text else None
        if edge_text:
            document = str(edge_text)
        elif edge_type is not None or edge_name is not None:
            document = f"{src_name} --[{edge_type}, {edge_name}, w={weight:.2f}]--> {dst_name}"
        else:
            document = f"{src_name} -> {dst_name} w={weight:.2f}"
        return {
            "entity": "edge",
            "edge_idx": edge_idx,
            "edge_name": edge_name,
            "src_name": src_name,
            "dst_name": dst_name,
            "src_id": src,
            "dst_id": dst,
            "edge_type": edge_type,
            "label": label,
            "weight": weight,
            "src_node_summary": self._node_summary(src),
            "dst_node_summary": self._node_summary(dst),
            "document": document,
        }

    def _node_summary(self, node_id: int) -> str:
        doc = self._node_docs[node_id] if node_id < len(self._node_docs) else self._build_node_doc(node_id)
        parts = [str(doc["display_name"]), str(doc["node_name"])]
        if doc.get("node_type") is not None:
            parts.append(f"type={doc['node_type']}")
        if doc.get("label") is not None:
            parts.append(f"label={doc['label']}")
        return self._truncate(" ".join(parts), _ENDPOINT_SUMMARY_CHARS)

    def _search_edges_by_endpoints(
        self,
        query: str,
        src_query: str,
        dst_query: str,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        src_scores = self._endpoint_scores(src_query) if src_query else None
        dst_scores = self._endpoint_scores(dst_query) if dst_query else None
        src_allowed = set(src_scores.keys()) if src_scores is not None else None
        dst_allowed = set(dst_scores.keys()) if dst_scores is not None else None
        edge_score_map = {
            doc["edge_idx"]: score
            for doc, score in self._score_edge_indices(
                [
                    doc["edge_idx"]
                    for doc in self._edge_docs
                    if self._edge_matches_filters(doc, filters)
                ],
                query,
            )
        }
        rows = []
        for doc in self._edge_docs:
            if not self._edge_matches_filters(doc, filters):
                continue
            if src_allowed is not None and doc["src_id"] not in src_allowed:
                continue
            if dst_allowed is not None and doc["dst_id"] not in dst_allowed:
                continue
            scores = []
            if query:
                scores.append(edge_score_map.get(doc["edge_idx"], 0.0))
            if src_scores is not None:
                scores.append(src_scores.get(doc["src_id"], 0.0))
            if dst_scores is not None:
                scores.append(dst_scores.get(doc["dst_id"], 0.0))
            score = float(sum(scores) / len(scores)) if scores else 0.0
            evidence = self._edge_evidence(doc, filters, query, score)
            if src_query:
                evidence.append("source node matches src_query")
            if dst_query:
                evidence.append("destination node matches dst_query")
            rows.append(self._edge_candidate(doc, score, evidence))
        return rows

    def _endpoint_scores(self, query: str, limit: int | None = None) -> dict[int, float]:
        if limit is None:
            limit = min(50, max(20, max(1, self._gd.num_nodes // 10)))
        exact_ids = self._exact_node_ids(query)
        if exact_ids:
            return {node_id: 1.0 for node_id in exact_ids}
        if not query:
            return {}
        indices = [doc["node_id"] for doc in self._node_docs]
        scored = self._score_node_indices(indices, query)
        return {doc["node_id"]: score for doc, score in scored[:limit]}

    def _score_node_indices(self, indices: list[int], query: str) -> list[tuple[dict[str, Any], float]]:
        if self._semantic_enabled and query and self._node_matrix.shape[0] == len(self._node_docs):
            q = self._query_vector(query)
            scores = self._node_matrix[indices] @ q if indices else np.empty((0,), dtype=np.float32)
            pairs = [(self._node_docs[idx], float(score)) for idx, score in zip(indices, scores)]
            return sorted(pairs, key=lambda item: item[1], reverse=True)
        return [(self._node_docs[idx], 0.0) for idx in indices]

    def _score_edge_indices(self, indices: list[int], query: str) -> list[tuple[dict[str, Any], float]]:
        if self._semantic_enabled and query and self._edge_matrix.shape[0] == len(self._edge_docs):
            q = self._query_vector(query)
            scores = self._edge_matrix[indices] @ q if indices else np.empty((0,), dtype=np.float32)
            pairs = [(self._edge_docs[idx], float(score)) for idx, score in zip(indices, scores)]
            return sorted(pairs, key=lambda item: item[1], reverse=True)
        return [(self._edge_docs[idx], 0.0) for idx in indices]

    def _query_vector(self, query: str) -> np.ndarray:
        with self._query_vec_cache_lock:
            cached = self._query_vec_cache.get(query)
            if cached is not None:
                return cached
        # Model inference / remote embedding requests must not block cache hits
        # or other independent searches while holding the bookkeeping lock.
        q = np.asarray(self._provider.encode([query]), dtype=np.float32)
        vec = self._normalize_rows(q)[0]
        with self._query_vec_cache_lock:
            # Another reader may have filled the same key while encoding.
            cached = self._query_vec_cache.get(query)
            if cached is not None:
                return cached
            if len(self._query_vec_cache) >= 128:
                self._query_vec_cache.pop(next(iter(self._query_vec_cache)))
            self._query_vec_cache[query] = vec
            return vec

    def _exact_node_ids(self, query: str) -> list[int]:
        if not query:
            return []
        node2id = self._gd.node2id
        if query in node2id:
            return [int(node2id[query])]
        if query in self._name_to_ids:
            return self._name_to_ids[query]
        try:
            node_id = int(query)
        except ValueError:
            return []
        if 0 <= node_id < self._gd.num_nodes:
            return [node_id]
        return []

    def _exact_node_id(self, query: str) -> int | None:
        matches = self._exact_node_ids(query)
        return matches[0] if len(matches) == 1 else None

    def _exact_edge_indices(self, query: str, src_query: str, dst_query: str) -> list[int]:
        indices: list[int] = []
        if query and self._gd.has_edge_by_edge_name(query):
            entry = self._gd.get_edge_by_edge_name(query)
            if entry is not None:
                indices.append(int(entry["edge_idx"]))
        src_id = self._exact_node_id(src_query)
        dst_id = self._exact_node_id(dst_query)
        if src_id is not None and dst_id is not None:
            entry = self._gd.get_edge(src_id, dst_id)
            if entry is not None:
                idx = int(entry["edge_idx"])
                if idx not in indices:
                    indices.append(idx)
        return indices

    def _node_matches_filters(self, doc: dict[str, Any], filters: dict[str, Any]) -> bool:
        if not filters:
            return True
        if filters.get("node_type") is not None and str(doc.get("node_type")) != str(filters["node_type"]):
            return False
        if filters.get("label") is not None and str(doc.get("label")) != str(filters["label"]):
            return False
        checks = [
            ("min_degree", "total_degree", lambda value, bound: value >= bound),
            ("max_degree", "total_degree", lambda value, bound: value <= bound),
            ("min_in_degree", "in_degree", lambda value, bound: value >= bound),
            ("max_in_degree", "in_degree", lambda value, bound: value <= bound),
            ("min_out_degree", "out_degree", lambda value, bound: value >= bound),
            ("max_out_degree", "out_degree", lambda value, bound: value <= bound),
        ]
        for filter_key, doc_key, predicate in checks:
            if filters.get(filter_key) is None:
                continue
            if not predicate(int(doc.get(doc_key, 0)), int(filters[filter_key])):
                return False
        return True

    def _edge_matches_filters(self, doc: dict[str, Any], filters: dict[str, Any]) -> bool:
        if not filters:
            return True
        if filters.get("edge_type") is not None and str(doc.get("edge_type")) != str(filters["edge_type"]):
            return False
        if filters.get("label") is not None and str(doc.get("label")) != str(filters["label"]):
            return False
        if filters.get("min_weight") is not None and float(doc.get("weight", 1.0)) < float(filters["min_weight"]):
            return False
        if filters.get("max_weight") is not None and float(doc.get("weight", 1.0)) > float(filters["max_weight"]):
            return False
        return True

    def _node_candidate(
        self,
        doc: dict[str, Any],
        score: float,
        evidence: list[str],
    ) -> dict[str, Any]:
        return {
            "node_name": doc["node_name"],
            "display_name": doc["display_name"],
            "node_id": doc["node_id"],
            "score": round(float(score), 4),
            "node_type": doc.get("node_type"),
            "label": doc.get("label"),
            "degree": doc["degree"],
            "num_neighbors": doc["num_neighbors"],
            "in_degree": doc["in_degree"],
            "out_degree": doc["out_degree"],
            "total_degree": doc["total_degree"],
            "semantic_type": doc.get("semantic_type"),
            "storage_type": doc.get("storage_type"),
            "neighbors_preview": doc.get("neighbors_preview", []),
            "evidence": evidence,
            "text_preview": self._truncate(str(doc.get("document") or ""), _PREVIEW_CHARS),
        }

    def _edge_candidate(
        self,
        doc: dict[str, Any],
        score: float,
        evidence: list[str],
    ) -> dict[str, Any]:
        return {
            "src_name": doc["src_name"],
            "dst_name": doc["dst_name"],
            "edge_idx": doc["edge_idx"],
            "edge_name": doc.get("edge_name"),
            "edge_type": doc.get("edge_type"),
            "label": doc.get("label"),
            "weight": doc.get("weight"),
            "score": round(float(score), 4),
            "src_node_summary": doc.get("src_node_summary"),
            "dst_node_summary": doc.get("dst_node_summary"),
            "evidence": evidence,
            "text_preview": self._truncate(str(doc.get("document") or ""), _PREVIEW_CHARS),
        }

    @staticmethod
    def _best_node(row: dict[str, Any], confidence: str) -> dict[str, Any]:
        return {
            "node_name": row["node_name"],
            "display_name": row["display_name"],
            "node_id": row["node_id"],
            "score": row["score"],
            "confidence": confidence,
        }

    @staticmethod
    def _best_edge(row: dict[str, Any], confidence: str) -> dict[str, Any]:
        return {
            "src_name": row["src_name"],
            "dst_name": row["dst_name"],
            "edge_name": row.get("edge_name"),
            "edge_idx": row.get("edge_idx"),
            "score": row["score"],
            "confidence": confidence,
        }

    def _node_evidence(
        self,
        doc: dict[str, Any],
        filters: dict[str, Any],
        query: str,
        score: float,
    ) -> list[str]:
        evidence: list[str] = []
        if query and self._semantic_enabled:
            evidence.append("embedding similarity")
        if filters.get("node_type") is not None:
            evidence.append("node_type matches filter")
        if filters.get("label") is not None:
            evidence.append("label matches filter")
        if any(key in filters for key in ("min_degree", "max_degree", "min_in_degree", "max_in_degree", "min_out_degree", "max_out_degree")):
            evidence.append("degree constraints match")
        if score == 0.0 and not evidence:
            evidence.append("structured candidate")
        return evidence

    def _edge_evidence(
        self,
        doc: dict[str, Any],
        filters: dict[str, Any],
        query: str,
        score: float,
    ) -> list[str]:
        evidence: list[str] = []
        if query and self._semantic_enabled:
            evidence.append("embedding similarity")
        if filters.get("edge_type") is not None:
            evidence.append("edge_type matches filter")
        if filters.get("label") is not None:
            evidence.append("label matches filter")
        if filters.get("min_weight") is not None or filters.get("max_weight") is not None:
            evidence.append("weight constraints match")
        if score == 0.0 and not evidence:
            evidence.append("structured candidate")
        return evidence

    def _sort_rows(
        self,
        rows: list[dict[str, Any]],
        sort: dict[str, Any] | None,
        *,
        default_field: str,
    ) -> list[dict[str, Any]]:
        field = str((sort or {}).get("field") or default_field)
        order = str((sort or {}).get("order") or "desc").lower()
        reverse = order != "asc"
        allowed = {
            "score",
            "node_id",
            "degree",
            "num_neighbors",
            "in_degree",
            "out_degree",
            "total_degree",
            "edge_idx",
            "weight",
        }
        if field not in allowed:
            field = default_field
        def sort_key(row: dict[str, Any]) -> Any:
            value = row.get(field)
            if value is None:
                return float("-inf") if reverse else float("inf")
            return value

        return sorted(rows, key=sort_key, reverse=reverse)

    def _confidence_and_ambiguity(
        self,
        rows: list[dict[str, Any]],
        query: str,
        exact: bool,
        *,
        has_filters: bool = False,
    ) -> tuple[str, bool]:
        if not rows:
            return "low", False
        if exact and rows[0].get("score") == 1.0:
            return "high", False
        if query and len(query) < 3:
            return "low", True
        if not self._semantic_enabled and len(rows) > 1 and not has_filters:
            return "low", True
        if not self._semantic_enabled and len(rows) > 1 and has_filters:
            return "medium", False
        if len(rows) > 1:
            gap = float(rows[0].get("score", 0.0)) - float(rows[1].get("score", 0.0))
            if gap < 0.05:
                return "medium", True
        score = float(rows[0].get("score", 0.0))
        if score >= 0.75:
            return "high", False
        return "medium", False

    def _result(
        self,
        *,
        query: str,
        rows: list[dict[str, Any]],
        best: dict[str, Any] | None,
        rerank_enabled: bool,
        ambiguous: bool,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "success": True,
            "query": query,
            "semantic_enabled": self._semantic_enabled,
            "retrieval_mode": getattr(self._provider, "name", "unknown"),
            "rerank_enabled": rerank_enabled,
            "best": best,
            "candidates": rows,
            "ambiguous": ambiguous,
        }
        if not self._semantic_enabled:
            result["message"] = (
                "No embedding provider available. Install sentence-transformers "
                "or configure OPENAI_API_KEY for semantic search."
            )
        if self._semantic_error:
            result["embedding_error"] = self._semantic_error
        if extra:
            result.update(extra)
        return result

    def _node_name(self, node_id: int) -> str:
        return self._gd.id2node.get(node_id, str(node_id))

    @staticmethod
    def _jsonable_label(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "tolist"):
            return value.tolist()
        if isinstance(value, (list, tuple)):
            return list(value)
        return value

    @staticmethod
    def _top_k(value: int) -> int:
        return min(max(int(value or 8), 1), _MAX_TOP_K)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = str(text or "")
        return text if len(text) <= limit else text[: limit - 1] + "..."

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from typing import Any, Iterable

import numpy as np

from graph_data_service.graph_data import GraphData


def _norm_node_key(name: str) -> str:
    """Normalisation key for node dedup: NFKC + collapse spaces + casefold."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


class GraphBuilder:
    @staticmethod
    def build_from_edge_rows(
        rows: Iterable[dict],
        *,
        source_key: str = "source",
        target_key: str = "target",
        weight_key: str | None = "weight",
        directed: bool = True,
    ) -> GraphData:
        node2id: OrderedDict[str, int] = OrderedDict()
        id2node: dict[int, str] = {}
        _norm2id: dict[str, int] = {}
        pairs: list[tuple[int, int]] = []
        weights: list[float] = []

        def ensure_node(name: str) -> int:
            norm = _norm_node_key(name)
            if norm not in _norm2id:
                idx = len(_norm2id)
                _norm2id[norm] = idx
                node2id[name] = idx
                id2node[idx] = name
            return _norm2id[norm]

        for row in rows:
            src = str(row.get(source_key) or "").strip()
            dst = str(row.get(target_key) or "").strip()
            if not src or not dst:
                continue
            weight = 1.0
            if weight_key and row.get(weight_key) not in (None, ""):
                weight = float(row[weight_key])
            s_id = ensure_node(src)
            d_id = ensure_node(dst)
            pairs.append((s_id, d_id))
            weights.append(weight)
            if not directed and s_id != d_id:
                pairs.append((d_id, s_id))
                weights.append(weight)

        if not pairs:
            return GraphData(
                num_nodes=len(node2id),
                id2node=id2node or None,
                node2id=dict(node2id) or None,
                directed=directed,
                semantic_type="undirected" if not directed else "directed",
            )

        edge_index = np.asarray(pairs, dtype=np.int64).T
        edge_weight = np.asarray(weights, dtype=np.float32)
        return GraphData(
            num_nodes=len(node2id),
            edge_index=edge_index,
            edge_weight=edge_weight,
            id2node=id2node,
            node2id=dict(node2id),
            directed=directed,
            semantic_type="undirected" if not directed else "directed",
        )

    @staticmethod
    def build_from_triples(triples: Iterable[dict], *, directed: bool = True) -> GraphData:
        node2id: OrderedDict[str, int] = OrderedDict()
        id2node: dict[int, str] = {}
        _norm2id: dict[str, int] = {}
        has_explicit_confidence = False

        def ensure_node(name: str) -> int:
            norm = _norm_node_key(name)
            if norm not in _norm2id:
                idx = len(_norm2id)
                _norm2id[norm] = idx
                node2id[name] = idx
                id2node[idx] = name
            return _norm2id[norm]

        def clean_text(value: Any) -> str:
            return str(value or "").strip()

        def clean_confidence(value: Any) -> float:
            try:
                confidence = float(value)
            except (TypeError, ValueError):
                return 1.0
            if confidence < 0:
                return 0.0
            if confidence > 1:
                return 1.0
            return confidence

        # 按 (src_id, dst_id) 聚合：同端点的多条 relation 合并为一条边
        # 值：[relations_seen_ordered, evidences_seen, max_confidence]
        EdgeAcc = tuple[list[str], list[str], float]
        edge_map: OrderedDict[tuple[int, int], EdgeAcc] = OrderedDict()

        def accumulate(s_id: int, d_id: int, relation: str, evidence: str, confidence: float) -> None:
            key = (s_id, d_id)
            if key not in edge_map:
                edge_map[key] = ([relation], [evidence] if evidence else [], confidence)
            else:
                rels, evs, conf = edge_map[key]
                if relation not in rels:
                    rels.append(relation)
                if evidence and evidence not in evs:
                    evs.append(evidence)
                edge_map[key] = (rels, evs, max(conf, confidence))

        for item in triples:
            if not isinstance(item, dict):
                continue
            src = clean_text(item.get("source"))
            dst = clean_text(item.get("target"))
            if not src or not dst:
                continue
            relation = clean_text(item.get("relation")) or "related_to"
            evidence = clean_text(item.get("evidence"))
            if item.get("confidence") not in (None, ""):
                has_explicit_confidence = True
            confidence = clean_confidence(item.get("confidence"))
            s_id = ensure_node(src)
            d_id = ensure_node(dst)
            accumulate(s_id, d_id, relation, evidence, confidence)
            if not directed and s_id != d_id:
                accumulate(d_id, s_id, relation, evidence, confidence)

        if not edge_map:
            return GraphData(
                num_nodes=len(node2id),
                id2node=id2node or None,
                node2id=dict(node2id) or None,
                directed=directed,
                semantic_type="undirected" if not directed else "directed",
            )

        pairs: list[tuple[int, int]] = []
        edge_types: list[str] = []
        edge_texts: list[str] = []
        edge_features: list[list[float]] = []
        any_edge_text = False

        for (s_id, d_id), (rels, evs, confidence) in edge_map.items():
            pairs.append((s_id, d_id))
            edge_types.append(" / ".join(rels))
            merged_evidence = "\n---\n".join(evs)
            edge_texts.append(merged_evidence)
            edge_features.append([confidence])
            any_edge_text = any_edge_text or bool(merged_evidence)

        edge_index = np.asarray(pairs, dtype=np.int64).T
        edge_weight = np.ones(len(pairs), dtype=np.float32)
        edge_feat = np.asarray(edge_features, dtype=np.float32) if has_explicit_confidence else None
        return GraphData(
            num_nodes=len(node2id),
            edge_index=edge_index,
            edge_weight=edge_weight,
            edge_feat=edge_feat,
            id2node=id2node,
            node2id=dict(node2id),
            edge_type=np.asarray(edge_types, dtype=object),
            edge_text=edge_texts if any_edge_text else None,
            directed=directed,
            semantic_type="undirected" if not directed else "directed",
        )

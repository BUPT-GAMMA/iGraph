"""
GraphData 计算层

负责在 GraphData Server 进程中基于 GraphData 执行图分析与检索，
并使用 (session_id, graph_version) 缓存 networkx 图，避免重复构建。

输出规模约定：
  - 任何节点/边列表均不超过 _MAX_LIST_ITEMS（50）条
  - 所有工具优先返回全局指标，细粒度列表仅作预览
  - 重型枚举类工具（find_cycles / find_cliques / link_prediction_score
    全图非边 / transitive_closure / topological_sort 预览）使用流式
    islice / heapq.nlargest，在到达预览上限或 top-K 后立即停止枚举，
    不再先物化完整结果再截断。
"""

from __future__ import annotations

import heapq
import math
import os
import statistics
from itertools import combinations, islice
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx

from graph_data_service.graph_data import GraphData
from graph_data_service.analysis_contract import analysis_contract
from graph_data_service.smart_overview import SmartOverviewSelection, select_smart_overview

# ── 全局输出规模上限 ─────────────────────────────────────────────
_MAX_LIST_ITEMS = 20        # 节点/边列表最大条数
_MAX_PATHS = 10             # 路径列表最大条数
_MAX_COMMUNITIES_SHOWN = 5 # 最多展示的社区数
_MAX_NODES_PER_COMMUNITY = 10  # 每个社区节点预览数
_MAX_COMPONENTS_SHOWN = 5  # 最多展示的连通分量数
_MAX_NODES_PER_COMPONENT = 10  # 每个分量节点预览数
_MAX_CYCLE_SEARCH = 10_000  # 最大环枚举默认安全上限


def _truncate(lst: list, limit: int | None = _MAX_LIST_ITEMS) -> Tuple[list, bool]:
    if limit is None:
        return lst, False
    if len(lst) > limit:
        return lst[:limit], True
    return lst, False


def _effective_limit(user_limit: int | None, default: int) -> int | None:
    """Return None for unlimited, or clamped int [1, 200].

    user_limit=None  → use default
    user_limit<=0    → unlimited (None)
    """
    if user_limit is None:
        return default
    if user_limit <= 0:
        return None
    return min(user_limit, 200)


def _degree_stats(values: list) -> dict:
    if not values:
        return {"mean": 0, "max": 0, "min": 0, "median": 0}
    return {
        "mean": round(statistics.mean(values), 4),
        "max": max(values),
        "min": min(values),
        "median": round(statistics.median(values), 4),
    }


def _undefined_assortativity(reason_code: str) -> dict[str, str]:
    """Return an explicit, model-readable reason for an undefined coefficient."""
    if reason_code == "no_edges":
        reason = (
            "度同配性在数学上未定义：图中没有可用于比较端点度数的边；"
            "这不表示计算引擎不支持该指标。"
        )
    else:
        reason = (
            "度同配性在数学上未定义：边端点的度数没有方差，Pearson "
            "相关系数的分母为 0；这不表示计算引擎不支持该指标。"
        )
    return {
        "undefined_reason_code": reason_code,
        "undefined_reason": reason,
    }


class GraphCompute:
    _COMMUNITY_ALGORITHMS = (
        "louvain",
        "greedy_modularity",
        "label_propagation",
        "leiden",
        "girvan_newman",
        "asyn_fluidc",
        "label_propagation_asyn",
    )
    _MAX_CYCLES_DISPLAY = _MAX_LIST_ITEMS

    def __init__(self) -> None:
        # session_id -> (graph_version, nx_graph)
        self._cache: Dict[str, Tuple[int, nx.DiGraph]] = {}

    def invalidate(self, session_id: str) -> None:
        self._cache.pop(session_id, None)

    def resolve_graph(
        self,
        session_id: str,
        graph_version: int,
        gd: GraphData,
    ) -> nx.DiGraph:
        cached = self._cache.get(session_id)
        if cached is not None and cached[0] == graph_version:
            return cached[1]

        nx_graph = gd.to_networkx()
        self._cache[session_id] = (graph_version, nx_graph)
        return nx_graph

    @staticmethod
    def graph_info(gd: GraphData) -> dict:
        id2node = gd.id2node
        src_arr, dst_arr = gd.edge_index
        edges = []
        for i in range(gd.num_edges):
            s, d = int(src_arr[i]), int(dst_arr[i])
            w = float(gd.edge_weight[i]) if gd.edge_weight is not None else 1.0
            edges.append({"src": id2node.get(s, str(s)), "dst": id2node.get(d, str(d)), "weight": w})
        all_nodes = [id2node.get(i, str(i)) for i in range(gd.num_nodes)]
        nodes_preview, nodes_truncated = _truncate(all_nodes)
        edges_preview, edges_truncated = _truncate(edges)
        return {
            "loaded": True,
            "num_nodes": gd.num_nodes,
            "num_edges": gd.num_edges,
            "num_edge_entries": gd.num_edge_entries,
            "num_unique_edges": gd.num_unique_edges,
            "num_unique_directed_edges": gd.num_unique_directed_edges,
            "num_unique_undirected_edges": gd.num_unique_undirected_edges,
            "semantic_type": gd.semantic_type,
            "storage_type": gd.storage_type,
            "is_symmetric": gd.is_symmetric,
            "has_self_loops": gd.has_self_loops,
            "has_duplicate_entries": gd.has_duplicate_entries,
            "directed": gd.directed,
            "has_node_names": gd.has_node_names,
            "has_edge_weight": gd.has_edge_weight,
            "nodes_preview": nodes_preview,
            "nodes_truncated": nodes_truncated,
            "edges_preview": edges_preview,
            "edges_truncated": edges_truncated,
        }

    @staticmethod
    def _node_not_found(node_name: str, gd: GraphData) -> dict:
        all_nodes = list(gd.node2id.keys())
        nodes_preview, truncated = _truncate(all_nodes)
        return {
            "success": False,
            "error": f"节点 '{node_name}' 不存在",
            "available_nodes_preview": nodes_preview,
            "available_nodes_total": len(all_nodes),
            "available_nodes_truncated": truncated,
        }

    @staticmethod
    def _check_nodes(g: nx.DiGraph, nodes: List[Tuple[str, str]]) -> Optional[dict]:
        for name, label in nodes:
            if name not in g:
                all_nodes = list(g.nodes())
                nodes_preview, truncated = _truncate(all_nodes)
                return {
                    "success": False,
                    "error": f"{label} '{name}' 不存在",
                    "available_nodes_preview": nodes_preview,
                    "available_nodes_total": len(all_nodes),
                    "available_nodes_truncated": truncated,
                }
        return None

    @staticmethod
    def _capacity_copy(g: nx.DiGraph) -> nx.DiGraph:
        cg = g.copy()
        for _, _, d in cg.edges(data=True):
            d["capacity"] = d.get("weight", 1.0)
        return cg

    @staticmethod
    def _negative_capacity_error(g: nx.DiGraph) -> Optional[dict]:
        """Reject negative edge weights before invoking a flow/cut kernel.

        NetworkX silently treats a negative capacity as unusable in its
        source/sink flow routines, while Stoer-Wagner rejects some (but not
        all) negative-weight projections.  A single explicit rule keeps the
        small- and large-graph paths deterministic.
        """
        for _, _, data in g.edges(data=True):
            try:
                capacity = float(data.get("weight", 1.0))
            except (TypeError, ValueError):
                continue
            if capacity < 0:
                return {"success": False, "error": "最大流/最小割不支持负容量"}
        return None

    @staticmethod
    def _serialize_label(label) -> Any:
        if label is None:
            return None
        if hasattr(label, "tolist"):
            return label.tolist()
        if isinstance(label, (list, tuple)):
            return list(label)
        return label

    def get_node_info(self, gd: GraphData, node_name: str) -> dict:
        if gd.has_node_name(node_name):
            nid = gd.name_to_id(node_name)
            display_name = node_name
        else:
            try:
                nid = int(node_name)
            except ValueError:
                return self._node_not_found(node_name, gd)
            if not (0 <= nid < gd.num_nodes):
                return {"success": False, "error": f"节点 ID {nid} 超出范围 [0, {gd.num_nodes})"}
            display_name = gd.id2node.get(nid, node_name)

        in_deg = gd.in_degree(nid)
        out_deg = gd.out_degree(nid)
        degree = gd.degree(nid)
        total_deg = gd.total_degree(nid)
        num_neighbors = gd.num_neighbors(nid)
        all_neighbors = [gd.id2node.get(n, str(n)) for n in gd.neighbors(nid)]
        all_predecessors = [gd.id2node.get(n, str(n)) for n in gd.predecessors(nid)] if gd.directed else []
        raw_edges = gd.get_incident_edges(nid)
        all_incident = [
            {
                "src": gd.id2node.get(e["src"], str(e["src"])),
                "dst": gd.id2node.get(e["dst"], str(e["dst"])),
                "weight": e.get("weight"),
                **({"edge_id": e["name"]} if e.get("name") is not None else {}),
            }
            for e in raw_edges
        ]

        neighbors_preview, neighbors_truncated = _truncate(all_neighbors)
        predecessors_preview, predecessors_truncated = _truncate(all_predecessors)
        incident_preview, incident_truncated = _truncate(all_incident)

        return {
            "success": True,
            "node": display_name,
            "node_id": nid,
            "properties": next((row["properties"] for row in gd.graph_metadata.get("construction_graph", {}).get("nodes", [])
                                if row["id"] == display_name), {}),
            "directed": gd.directed,
            "semantic_type": gd.semantic_type,
            "storage_type": gd.storage_type,
            "label": self._serialize_label(
                gd.get_node_label_by_id(nid) if gd.has_node_label else None
            ),
            "type": gd.get_node_type_by_id(nid) if gd.has_node_type else None,
            "text": gd.node_text[nid] if gd.has_node_text else None,
            "degree": degree,
            "num_neighbors": num_neighbors,
            "in_degree": in_deg,
            "out_degree": out_deg,
            "total_degree": total_deg,
            "degree_mode": (
                "logical_undirected" if gd.semantic_type == "undirected" else "directed_in_out"
            ),
            "neighbors_preview": neighbors_preview,
            "neighbors_total": len(all_neighbors),
            "neighbors_truncated": neighbors_truncated,
            "predecessors_preview": predecessors_preview,
            "predecessors_total": len(all_predecessors),
            "predecessors_truncated": predecessors_truncated,
            "incident_edges_preview": incident_preview,
            "incident_edges_total": len(all_incident),
            "incident_edges_truncated": incident_truncated,
        }

    def get_edge_info(self, gd: GraphData, src_name: str, dst_name: str) -> dict:
        for name in (src_name, dst_name):
            if not gd.has_node_name(name):
                return self._node_not_found(name, gd)
        edge = gd.get_edge_by_node_names(src_name, dst_name)
        if edge is None:
            return {
                "success": True,
                "exists": False,
                "src": src_name,
                "dst": dst_name,
                "message": f"边 '{src_name}' -> '{dst_name}' 不存在",
            }
        construction = gd.graph_metadata.get("construction_graph")
        properties = {row["id"]: row["properties"] for row in construction["edges"]} if construction else {}

        def details(entry):
            return {"weight": entry.get("weight"), "edge_name": entry.get("name"),
                    "edge_label": self._serialize_label(entry.get("label")), "edge_type": entry.get("type"),
                    "properties": properties.get(entry.get("name"), {})}

        result = {"success": True, "exists": True, "src": src_name, "dst": dst_name}
        if construction:
            matches = [gd.get_edge_by_edge_name(row["id"]) for row in construction["edges"]
                       if (row["source"], row["target"]) == (src_name, dst_name)
                       or (not gd.directed and (row["target"], row["source"]) == (src_name, dst_name))]
            matches = [entry for entry in matches if entry is not None]
            if len(matches) > 1:
                preview, truncated = _truncate(matches, _MAX_LIST_ITEMS)
                return {**result, "parallel_edges": True, "edge_count": len(matches),
                        "edges": [details(entry) for entry in preview], "truncated": truncated}
        return {**result, **details(edge)}

    @staticmethod
    def check_edge_exists(gd: GraphData, src_name: str, dst_name: str) -> dict:
        exists = gd.has_edge_by_node_names(src_name, dst_name)
        return {
            "success": True,
            "src": src_name,
            "dst": dst_name,
            "exists": exists,
            "directed": gd.directed,
        }

    @staticmethod
    def _path_edges(g, path):
        return [{"from": source, "to": target, "weight": g[source][target].get("weight", 1.0),
                 **({"edge_id": g[source][target]["id"]} if "id" in g[source][target] else {})}
                for source, target in zip(path, path[1:])]

    @analysis_contract
    def shortest_path(
        self,
        g: nx.DiGraph,
        source: str,
        target: str,
        use_weight: bool = True,
        algorithm: str = "default",
        k: int = 3,
        *,
        limit: int | None = None,
    ) -> dict:
        if source not in g:
            return {
                "success": False,
                "found": False,
                "error": f"起点 '{source}' 不存在于图中",
            }

        if target == "all":
            w = "weight" if use_weight else None
            if use_weight:
                all_lengths, all_paths = nx.single_source_dijkstra(g, source, weight=w)
            else:
                all_paths = nx.single_source_shortest_path(g, source)
                all_lengths = nx.single_source_shortest_path_length(g, source)

            reachable_count = sum(1 for n in all_lengths if n != source)
            dist_values = [d for n, d in all_lengths.items() if n != source]
            # 按距离排序，返回最近的 limit 个
            eff = _effective_limit(limit, _MAX_PATHS)
            sorted_targets = sorted(
                [n for n in all_paths if n != source],
                key=lambda t: all_lengths[t]
            )[:eff]
            nearest = [
                {"target": t, "distance": all_lengths[t], "path": all_paths[t]}
                for t in sorted_targets
            ]
            return {
                "success": True,
                "found": reachable_count > 0,
                "source": source,
                "weighted": use_weight,
                "reachable_count": reachable_count,
                "avg_distance": round(statistics.mean(dist_values), 4) if dist_values else 0,
                "max_distance": round(max(dist_values), 4) if dist_values else 0,
                "min_distance": round(min(dist_values), 4) if dist_values else 0,
                "nearest_paths": nearest,
                "truncated": eff is not None and reachable_count > eff,
            }

        if target not in g:
            return {
                "success": False,
                "found": False,
                "error": f"终点 '{target}' 不存在于图中",
            }

        w = "weight" if use_weight else None
        if algorithm == "yen_k_shortest":
            try:
                paths_iter = nx.shortest_simple_paths(g, source, target, weight=w)
                paths = list(islice(paths_iter, max(int(k), 1)))
                serialized = []
                for path in paths:
                    serialized.append(
                        {
                            "path": path,
                            "total_length": sum(edge["weight"] for edge in self._path_edges(g, path)) if w else max(len(path) - 1, 0),
                            "edges": self._path_edges(g, path),
                        }
                    )
                return {
                    "success": True,
                    "found": bool(serialized),
                    "algorithm": algorithm,
                    "paths": serialized,
                    "k": max(int(k), 1),
                    "weighted": use_weight,
                }
            except nx.NetworkXNoPath:
                return {"success": True, "found": False, "algorithm": algorithm, "paths": []}
        try:
            path = nx.shortest_path(g, source, target, weight=w)
            length = nx.shortest_path_length(g, source, target, weight=w)
            edges_in_path = self._path_edges(g, path)
            return {
                "success": True,
                "found": True,
                "path": path,
                "hop_count": len(path) - 1,
                "total_length": length,
                "edges": edges_in_path,
                "weighted": use_weight,
            }
        except nx.NetworkXNoPath:
            return {
                "success": True,
                "found": False,
                "message": f"从 '{source}' 到 '{target}' 不存在可达路径",
            }

    @staticmethod
    @analysis_contract
    def all_shortest_paths(g: nx.DiGraph, source: str, target: str, *, limit: int | None = None) -> dict:
        err = GraphCompute._check_nodes(
            g, [(source, "起点"), (target, "终点")]
        )
        if err:
            return err
        try:
            eff = _effective_limit(limit, _MAX_PATHS)
            length = nx.shortest_path_length(g, source, target, weight="weight")
            paths: list = []
            truncated = False
            for p in nx.all_shortest_paths(g, source, target, weight="weight"):
                paths.append(p)
                if eff is not None and len(paths) > eff:
                    paths.pop()
                    truncated = True
                    break
            return {
                "success": True,
                "count_returned": len(paths),
                "length": length,
                "paths": paths,
                "truncated": truncated,
                "note": f"最多返回 {_MAX_PATHS} 条，实际路径数可能更多" if truncated else None,
            }
        except nx.NetworkXNoPath:
            return {"success": True, "count_returned": 0, "paths": [], "message": "不存在路径"}

    @analysis_contract
    def maximum_flow(self, g: nx.DiGraph, source: str, sink: str, *, limit: int | None = None) -> dict:
        err = self._check_nodes(g, [(source, "源点"), (sink, "汇点")])
        if err:
            return err
        if source == sink:
            return {"success": False, "error": "源点与汇点不能相同"}
        capacity_error = self._negative_capacity_error(g)
        if capacity_error:
            return capacity_error
        cg = self._capacity_copy(g)
        flow_value, flow_dict = nx.maximum_flow(cg, source, sink, capacity="capacity")

        # 收集非零流边，按流量降序取前 _MAX_LIST_ITEMS 条
        nonzero_edges = []
        for u, inner in flow_dict.items():
            for v, f in inner.items():
                if f > 0:
                    nonzero_edges.append({"from": u, "to": v, "flow": round(float(f), 4)})
        nonzero_edges.sort(key=lambda e: e["flow"], reverse=True)
        top_edges, truncated = _truncate(nonzero_edges, _effective_limit(limit, _MAX_LIST_ITEMS))

        return {
            "success": True,
            "max_flow_value": flow_value,
            "total_nonzero_edges": len(nonzero_edges),
            "top_flow_edges": top_edges,
            "truncated": truncated,
        }

    @analysis_contract
    def minimum_cut(self, g: nx.DiGraph, source: str, sink: str) -> dict:
        err = self._check_nodes(g, [(source, "源点"), (sink, "汇点")])
        if err:
            return err
        if source == sink:
            return {"success": False, "error": "源点与汇点不能相同"}
        capacity_error = self._negative_capacity_error(g)
        if capacity_error:
            return capacity_error
        cg = self._capacity_copy(g)
        cut_value, (reachable, non_reachable) = nx.minimum_cut(cg, source, sink, capacity="capacity")

        cut_edges = [
            {"from": u, "to": v, "weight": g[u][v].get("weight", 1.0)}
            for u in reachable
            for v in non_reachable
            if g.has_edge(u, v)
        ]
        # 返回较小的分区预览
        smaller = sorted(reachable) if len(reachable) <= len(non_reachable) else sorted(non_reachable)
        larger_size = len(non_reachable) if len(reachable) <= len(non_reachable) else len(reachable)
        smaller_preview, smaller_truncated = _truncate(smaller)
        cut_edges_preview, edges_truncated = _truncate(cut_edges)

        return {
            "success": True,
            "min_cut_value": cut_value,
            "source_partition_size": len(reachable),
            "sink_partition_size": len(non_reachable),
            "smaller_partition_preview": smaller_preview,
            "smaller_partition_total": len(smaller),
            "smaller_partition_truncated": smaller_truncated,
            "cut_edges_preview": cut_edges_preview,
            "cut_edges_total": len(cut_edges),
            "cut_edges_truncated": edges_truncated,
        }

    @staticmethod
    def _weighted_undirected_graph(g: nx.DiGraph) -> nx.Graph:
        ug = nx.Graph()
        ug.add_nodes_from(g.nodes(data=True))
        for u, v, data in g.edges(data=True):
            if u == v:
                continue
            try:
                weight = float(data.get("weight", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            if ug.has_edge(u, v):
                ug[u][v]["weight"] = float(ug[u][v].get("weight", 0.0)) + weight
            else:
                ug.add_edge(u, v, weight=weight)
        return ug

    @analysis_contract
    def global_minimum_cut(
        self,
        g: nx.DiGraph,
        *,
        treat_as_undirected: bool = True,
        limit: int | None = None,
    ) -> dict:
        if g.number_of_nodes() < 2:
            return {
                "success": False,
                "error": "全局最小割至少需要 2 个节点。",
            }
        if g.is_directed() and not treat_as_undirected:
            return {
                "success": False,
                "error": "当前仅支持无向全局最小割；有向图请设置 treat_as_undirected=true 或使用 minimum_cut 指定 source/sink。",
            }
        capacity_error = self._negative_capacity_error(g)
        if capacity_error:
            return capacity_error

        ug = self._weighted_undirected_graph(g)
        components = [set(c) for c in nx.connected_components(ug)]
        if len(components) > 1:
            partition_a = components[0]
            partition_b = set(ug.nodes()) - partition_a
            cut_value = 0.0
        else:
            try:
                cut_value, partition = nx.stoer_wagner(ug, weight="weight")
            except nx.NetworkXError as exc:
                return {"success": False, "error": str(exc)}
            partition_a, partition_b = set(partition[0]), set(partition[1])

        smaller = partition_a if len(partition_a) <= len(partition_b) else partition_b
        larger = partition_b if smaller is partition_a else partition_a
        smaller_preview, smaller_truncated = _truncate(sorted(smaller), _effective_limit(limit, _MAX_LIST_ITEMS))

        cut_edges = []
        for u, v, data in g.edges(data=True):
            if (u in partition_a and v in partition_b) or (u in partition_b and v in partition_a):
                cut_edges.append({
                    "from": u,
                    "to": v,
                    "weight": data.get("weight", 1.0),
                })
        cut_edges_preview, edges_truncated = _truncate(cut_edges, _effective_limit(limit, _MAX_LIST_ITEMS))

        return {
            "success": True,
            "min_cut_value": round(float(cut_value), 6),
            "algorithm": "connected_components" if len(components) > 1 else "stoer_wagner",
            "treat_as_undirected": bool(treat_as_undirected),
            "partition_a_size": len(partition_a),
            "partition_b_size": len(partition_b),
            "smaller_partition_preview": smaller_preview,
            "smaller_partition_total": len(smaller),
            "larger_partition_total": len(larger),
            "smaller_partition_truncated": smaller_truncated,
            "cut_edges_preview": cut_edges_preview,
            "cut_edges_total": len(cut_edges),
            "cut_edges_truncated": edges_truncated,
            "disconnected": len(components) > 1,
        }

    @staticmethod
    @analysis_contract
    def minimum_spanning_tree(g: nx.DiGraph, *, limit: int | None = None) -> dict:
        # ``DiGraph.to_undirected`` keeps one of two reciprocal edge attribute
        # dictionaries according to insertion order.  That makes an MST depend
        # on upload order when u->v and v->u have different weights.  The large
        # graph path canonicalises such a pair to its minimum weight, so build
        # the same deterministic projection here.
        ug = nx.Graph()
        ug.add_nodes_from(g.nodes(data=True))
        projected_edges: dict[tuple[Any, Any], float] = {}
        for source, target, data in g.edges(data=True):
            if source == target:
                continue
            left, right = sorted((source, target), key=lambda node: str(node))
            weight = float(data.get("weight", 1.0))
            key = (left, right)
            projected_edges[key] = min(projected_edges.get(key, weight), weight)
        for (source, target), weight in sorted(
            projected_edges.items(),
            key=lambda item: (item[1], str(item[0][0]), str(item[0][1])),
        ):
            ug.add_edge(source, target, weight=weight)
        components = list(nx.connected_components(ug))
        if len(components) == 1:
            mst = nx.minimum_spanning_tree(ug, weight="weight")
            all_edges = sorted(
                [
                    {
                        "from": min((u, v), key=lambda node: str(node)),
                        "to": max((u, v), key=lambda node: str(node)),
                        "weight": round(d.get("weight", 1.0), 4),
                    }
                    for u, v, d in mst.edges(data=True)
                ],
                key=lambda e: (e["weight"], str(e["from"]), str(e["to"])),
            )
            total = round(
                sum(
                    float(data.get("weight", 1.0))
                    for _, _, data in mst.edges(data=True)
                ),
                4,
            )
            eff = _effective_limit(limit, _MAX_LIST_ITEMS)
            edges_preview, truncated = _truncate(all_edges, eff)
            return {
                "success": True,
                "connected": True,
                "total_weight": total,
                "num_edges": len(all_edges),
                "mst_edges_preview": edges_preview,
                "truncated": truncated,
            }
        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        total = 0.0
        forest_summary = []
        for comp in sorted(components, key=len, reverse=True):
            sub = ug.subgraph(comp)
            mst = nx.minimum_spanning_tree(sub, weight="weight")
            raw_weight = sum(
                float(data.get("weight", 1.0))
                for _, _, data in mst.edges(data=True)
            )
            w = round(raw_weight, 4)
            total += raw_weight
            forest_summary.append({"component_size": len(comp), "mst_weight": w, "mst_edges": mst.number_of_edges()})
        return {
            "success": True,
            "connected": False,
            "num_components": len(components),
            "total_weight": round(total, 4),
            "forest_summary": forest_summary[:eff],
            "note": "图不连通，结果为最小生成森林摘要；如需具体边信息请先提取子图",
        }

    @analysis_contract
    def community_detection(
        self,
        g: nx.DiGraph,
        algorithm: str = "louvain",
        *,
        limit: int | None = None,
        focus_nodes: list[str] | None = None,
    ) -> dict:
        if algorithm not in self._COMMUNITY_ALGORITHMS:
            return {
                "success": False,
                "error": f"未知算法 '{algorithm}'",
                "available": list(self._COMMUNITY_ALGORITHMS),
            }
        ug = g.to_undirected()
        comms = self._detect_communities(ug, algorithm)
        sizes = [len(c) for c in comms]

        try:
            modularity = round(nx.community.modularity(ug, comms, weight="weight"), 6)
        except Exception:
            modularity = None

        size_stats = _degree_stats(sizes)

        # 按社区大小降序排列，最多展示 eff 个
        eff = _effective_limit(limit, _MAX_COMMUNITIES_SHOWN)
        sorted_comms = sorted(enumerate(comms), key=lambda x: len(x[1]), reverse=True)
        communities_output = []
        for orig_id, comm in sorted_comms[:eff]:
            nodes_list = sorted(comm)
            nodes_preview, nodes_truncated = _truncate(nodes_list, _MAX_NODES_PER_COMMUNITY)
            communities_output.append({
                "community_id": orig_id,
                "size": len(comm),
                "nodes_preview": nodes_preview,
                "nodes_truncated": nodes_truncated,
            })

        node_community: dict[object, tuple[int, set]] = {}
        for community_id, community in enumerate(comms):
            for node in community:
                node_community[node] = (community_id, community)
        focus_node_communities = []
        missing_focus_nodes = []
        seen_focus_nodes = set()
        for raw_node in focus_nodes or []:
            node = str(raw_node)
            if node in seen_focus_nodes:
                continue
            seen_focus_nodes.add(node)
            membership = node_community.get(node)
            if membership is None:
                missing_focus_nodes.append(node)
                continue
            community_id, community = membership
            focus_node_communities.append(
                {
                    "node_id": node,
                    "node": node,
                    "community_id": community_id,
                    "community_size": len(community),
                    "representative_nodes": sorted(community)[:10],
                }
            )

        return {
            "success": True,
            "algorithm": algorithm,
            "num_communities": len(comms),
            "modularity": modularity,
            "size_stats": size_stats,
            "communities_shown": len(communities_output),
            "communities_truncated": eff is not None and len(comms) > eff,
            "communities": communities_output,
            "focus_node_communities": focus_node_communities,
            "missing_focus_nodes": missing_focus_nodes,
        }

    @staticmethod
    def _detect_communities(ug: nx.Graph, algorithm: str) -> list:
        # Several NetworkX community implementations either reject a null
        # graph or yield a synthetic empty community.  A community is a set of
        # actual vertices, so both compute paths use zero communities for the
        # null graph and the sole vertex for every singleton variant.
        if ug.number_of_nodes() == 0:
            return []
        if ug.number_of_nodes() == 1:
            return [set(ug.nodes())]
        if algorithm == "louvain":
            try:
                return list(nx.community.louvain_communities(ug, weight="weight", seed=42))
            except AttributeError:
                import community as community_louvain  # type: ignore[import-untyped]

                partition = community_louvain.best_partition(ug, weight="weight", random_state=42)
                buckets: Dict[int, list] = {}
                for node, cid in partition.items():
                    buckets.setdefault(cid, []).append(node)
                return [set(v) for v in buckets.values()]
        if algorithm == "greedy_modularity":
            return list(nx.community.greedy_modularity_communities(ug, weight="weight"))
        if algorithm == "label_propagation":
            return list(nx.community.label_propagation_communities(ug))
        if algorithm == "label_propagation_asyn":
            return list(nx.community.asyn_lpa_communities(ug, weight="weight", seed=42))
        if algorithm == "girvan_newman":
            generator = nx.community.girvan_newman(ug)
            return list(next(generator))
        if algorithm == "asyn_fluidc":
            # Fluid communities require a connected graph.  Run every
            # connected component independently instead of silently dropping
            # every component except the largest one.
            communities: list[set] = []
            for component in nx.connected_components(ug):
                subgraph = ug.subgraph(component).copy()
                if subgraph.number_of_nodes() == 1:
                    communities.append(set(component))
                    continue
                k = min(3, subgraph.number_of_nodes())
                communities.extend(
                    nx.community.asyn_fluidc(subgraph, k=k, seed=42)
                )
            return communities
        if algorithm == "leiden":
            try:
                import igraph as ig  # type: ignore
                import leidenalg  # type: ignore

                nodes = list(ug.nodes())
                node2idx = {node: idx for idx, node in enumerate(nodes)}
                ig_graph = ig.Graph()
                ig_graph.add_vertices(len(nodes))
                ig_graph.add_edges([(node2idx[u], node2idx[v]) for u, v in ug.edges()])
                partition = leidenalg.find_partition(ig_graph, leidenalg.ModularityVertexPartition)
                return [set(nodes[idx] for idx in community) for community in partition]
            except Exception:
                return list(nx.community.louvain_communities(ug, weight="weight", seed=42))
        return [set(ug.nodes())]

    @analysis_contract
    def node_centrality(self, g: nx.DiGraph, centrality_type: str = "degree", *, limit: int | None = None) -> dict:
        dispatch = {
            "degree": lambda: nx.degree_centrality(g),
            "betweenness": lambda: nx.betweenness_centrality(g, weight="weight"),
            "closeness": lambda: nx.closeness_centrality(g, distance="weight"),
            "pagerank": lambda: self._pagerank_centrality(g),
            "eigenvector": lambda: self._eigenvector_centrality(g),
            "harmonic": lambda: nx.harmonic_centrality(g, distance="weight"),
            "katz": lambda: nx.katz_centrality(g, weight="weight", alpha=0.1, beta=1.0, max_iter=1000),
            "load": lambda: nx.load_centrality(g, weight="weight"),
            "voterank": lambda: {node: float(len(g) - idx) for idx, node in enumerate(nx.voterank(g))},
            "percolation": lambda: nx.percolation_centrality(
                g,
                attribute="percolation",
                states={node: g.nodes[node].get("percolation", 1.0) for node in g.nodes()},
            ),
        }
        if centrality_type not in dispatch:
            return {
                "success": False,
                "error": f"未知类型 '{centrality_type}'",
                "available": list(dispatch.keys()),
            }
        cent = dispatch[centrality_type]()
        ranked = sorted(cent.items(), key=lambda x: x[1], reverse=True)
        values = [v for _, v in ranked]
        value_stats = _degree_stats(values)

        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        top_nodes = [{"node": n, "value": round(v, 6)} for n, v in ranked[:eff]]
        # bottom: 排名最低的若干节点（避免与 top 重叠）
        bottom_start = len(ranked) if eff is None else max(eff, len(ranked) - 10)
        bottom_nodes = [{"node": n, "value": round(v, 6)} for n, v in ranked[bottom_start:]]

        return {
            "success": True,
            "centrality_type": centrality_type,
            "total_nodes": len(ranked),
            "value_stats": value_stats,
            "top_nodes": top_nodes,
            "bottom_nodes": bottom_nodes,
            "truncated": eff is not None and len(ranked) > eff,
        }

    @staticmethod
    def _eigenvector_centrality(g: nx.DiGraph) -> dict:
        if g.number_of_nodes() == 0:
            return {}
        try:
            return nx.eigenvector_centrality(g, weight="weight", max_iter=1000)
        except nx.PowerIterationFailedConvergence:
            return nx.eigenvector_centrality_numpy(g, weight="weight")

    @staticmethod
    def _pagerank_python(
        g: nx.Graph,
        *,
        alpha: float = 0.85,
        max_iter: int = 100,
        tolerance: float = 1.0e-6,
    ) -> dict:
        """Dependency-free PageRank power iteration.

        NetworkX 3.x routes :func:`pagerank` through SciPy.  GraphData's small
        path must remain usable in minimal deployments, so this mirrors the
        uniform-personalisation NetworkX iteration using only graph iterators.
        """
        if len(g) == 0:
            return {}
        graph = g.to_directed()
        nodes = list(graph)
        node_count = len(nodes)
        scores = dict.fromkeys(nodes, 1.0 / node_count)
        outgoing_weight: dict[Any, float] = {}
        for node in nodes:
            outgoing_weight[node] = sum(
                float(data.get("weight", 1.0))
                for _, _, data in graph.out_edges(node, data=True)
            )

        for _ in range(max_iter):
            previous = scores
            scores = dict.fromkeys(nodes, 0.0)
            dangling = alpha * sum(
                previous[node] for node in nodes if outgoing_weight[node] == 0.0
            )
            base = (1.0 - alpha + dangling) / node_count
            for node in nodes:
                scores[node] += base
                total = outgoing_weight[node]
                if total == 0.0:
                    continue
                for _, target, data in graph.out_edges(node, data=True):
                    scores[target] += (
                        alpha
                        * previous[node]
                        * float(data.get("weight", 1.0))
                        / total
                    )
            error = sum(abs(scores[node] - previous[node]) for node in nodes)
            if error < node_count * tolerance:
                return scores
        raise nx.PowerIterationFailedConvergence(max_iter)

    @classmethod
    def _pagerank_centrality(cls, g: nx.Graph) -> dict:
        try:
            return nx.pagerank(g, weight="weight")
        except (ImportError, ModuleNotFoundError):
            return cls._pagerank_python(g)

    @staticmethod
    @analysis_contract
    def graph_connectivity(g: nx.DiGraph, *, limit: int | None = None) -> dict:
        eff = _effective_limit(limit, _MAX_COMPONENTS_SHOWN)

        def _component_summary(components: list) -> list:
            result = []
            for c in components[:eff]:
                preview, truncated = _truncate(c, _MAX_NODES_PER_COMPONENT)
                result.append({
                    "size": len(c),
                    "nodes_preview": preview,
                    "nodes_truncated": truncated,
                })
            return result

        if g.is_directed():
            scc_raw = sorted([sorted(c) for c in nx.strongly_connected_components(g)], key=len, reverse=True)
            wcc_raw = sorted([sorted(c) for c in nx.weakly_connected_components(g)], key=len, reverse=True)
            return {
                "success": True,
                "directed": True,
                # NetworkX raises NetworkXPointlessConcept for the null graph.
                # Component cardinality gives the same answer for every
                # non-empty graph and an explicit False for the null graph,
                # matching the disk-backed path.
                "is_strongly_connected": len(scc_raw) == 1,
                "is_weakly_connected": len(wcc_raw) == 1,
                "strongly_connected_components": {
                    "count": len(scc_raw),
                    "largest_size": len(scc_raw[0]) if scc_raw else 0,
                    "components": _component_summary(scc_raw),
                    "truncated": eff is not None and len(scc_raw) > eff,
                },
                "weakly_connected_components": {
                    "count": len(wcc_raw),
                    "largest_size": len(wcc_raw[0]) if wcc_raw else 0,
                    "components": _component_summary(wcc_raw),
                    "truncated": eff is not None and len(wcc_raw) > eff,
                },
            }
        cc_raw = sorted([sorted(c) for c in nx.connected_components(g)], key=len, reverse=True)
        return {
            "success": True,
            "directed": False,
            "is_connected": len(cc_raw) == 1,
            "connected_components": {
                "count": len(cc_raw),
                "largest_size": len(cc_raw[0]) if cc_raw else 0,
                "components": _component_summary(cc_raw),
                "truncated": eff is not None and len(cc_raw) > eff,
            },
        }

    @analysis_contract
    def find_cycles(self, g: nx.DiGraph, *, limit: int | None = None) -> dict:
        eff = _effective_limit(limit, self._MAX_CYCLES_DISPLAY)
        if eff is None:
            cycles = list(nx.simple_cycles(g))
            truncated = False
        else:
            # Pull one extra cycle so we can detect "more exist" without
            # materializing every cycle — Johnson's enumeration can blow up
            # combinatorially on dense graphs.
            sampled = list(islice(nx.simple_cycles(g), eff + 1))
            truncated = len(sampled) > eff
            cycles = sampled[:eff]
        return {
            "success": True,
            "has_cycles": len(cycles) > 0,
            "cycles": cycles,
            "truncated": truncated,
            "total_cycles_at_least": len(cycles) + (1 if truncated else 0),
            "note": (
                f"已展示 {len(cycles)} 个简单环；图可能存在更多环。"
                if truncated
                else None
            ),
        }

    @analysis_contract
    def maximum_cycle(
        self,
        g: nx.DiGraph,
        *,
        max_cycles: int | None = _MAX_CYCLE_SEARCH,
        optimize_by: str = "length",
    ) -> dict:
        """Find the best simple cycle found by bounded enumeration.

        Longest simple cycle is NP-hard on general graphs, so this intentionally
        exposes whether the search exhausted all cycles or stopped at a limit.
        """
        optimize = (optimize_by or "length").strip().lower()
        if optimize not in {"length", "weight"}:
            return {
                "success": False,
                "error": "optimize_by 仅支持 length 或 weight",
            }

        cap = _MAX_CYCLE_SEARCH if max_cycles is None else int(max_cycles)
        cap = max(1, min(cap, 100_000))
        best_cycle: list | None = None
        best_weight = 0.0
        best_key: tuple = (-1, float("-inf"))
        examined = 0
        overflow = False
        seen: set[tuple] = set()

        for raw_cycle in nx.simple_cycles(self._cycle_enumeration_graph(g)):
            cycle = self._normalize_cycle(g, raw_cycle)
            if not cycle:
                continue
            key = tuple(cycle)
            if key in seen:
                continue
            seen.add(key)
            if examined >= cap:
                overflow = True
                break
            weight = self._cycle_weight(g, cycle)
            length = len(cycle)
            candidate_key = (weight, length) if optimize == "weight" else (length, weight)
            examined += 1
            if candidate_key > best_key:
                best_key = candidate_key
                best_cycle = cycle
                best_weight = weight

        search_exhausted = not overflow
        if best_cycle is None:
            return {
                "success": True,
                "has_cycle": False,
                "cycle": [],
                "cycle_length": 0,
                "cycle_weight": 0.0,
                "cycles_examined": examined,
                "search_exhausted": search_exhausted,
                "truncated": not search_exhausted,
                "optimize_by": optimize,
            }

        return {
            "success": True,
            "has_cycle": True,
            "cycle": best_cycle,
            "cycle_length": len(best_cycle),
            "cycle_weight": round(float(best_weight), 6),
            "edges": self._cycle_edges(g, best_cycle),
            "cycles_examined": examined,
            "search_exhausted": search_exhausted,
            "truncated": not search_exhausted,
            "optimize_by": optimize,
            "note": (
                f"已扫描 {examined} 个简单环后达到上限；返回的是已扫描范围内的最大环。"
                if not search_exhausted
                else None
            ),
        }

    @staticmethod
    def _cycle_enumeration_graph(g: nx.DiGraph) -> nx.DiGraph:
        if g.is_directed():
            return g
        dg = nx.DiGraph()
        dg.add_nodes_from(g.nodes(data=True))
        for u, v, attrs in g.edges(data=True):
            dg.add_edge(u, v, **attrs)
            if u != v:
                dg.add_edge(v, u, **attrs)
        return dg

    @classmethod
    def _normalize_cycle(cls, g: nx.DiGraph, cycle: list) -> list:
        if g.is_directed():
            return cls._rotate_cycle(cycle)
        if len(cycle) < 3:
            return []
        forward = cls._rotate_cycle(cycle)
        backward = cls._rotate_cycle(list(reversed(cycle)))
        return forward if tuple(map(str, forward)) <= tuple(map(str, backward)) else backward

    @staticmethod
    def _rotate_cycle(cycle: list) -> list:
        if not cycle:
            return []
        start = min(range(len(cycle)), key=lambda i: str(cycle[i]))
        return cycle[start:] + cycle[:start]

    @staticmethod
    def _edge_weight(g: nx.DiGraph, u, v) -> float:
        try:
            return float(g[u][v].get("weight", 1.0))
        except (KeyError, TypeError, ValueError):
            return 1.0

    @classmethod
    def _cycle_weight(cls, g: nx.DiGraph, cycle: list) -> float:
        if not cycle:
            return 0.0
        return sum(
            cls._edge_weight(g, cycle[i], cycle[(i + 1) % len(cycle)])
            for i in range(len(cycle))
        )

    @classmethod
    def _cycle_edges(cls, g: nx.DiGraph, cycle: list) -> list[dict]:
        return [
            {
                "from": cycle[i],
                "to": cycle[(i + 1) % len(cycle)],
                "weight": cls._edge_weight(g, cycle[i], cycle[(i + 1) % len(cycle)]),
            }
            for i in range(len(cycle))
        ]

    @staticmethod
    @analysis_contract
    def topological_sort(g: nx.DiGraph, *, limit: int | None = None) -> dict:
        if not g.is_directed():
            return {
                "success": False,
                "is_dag": False,
                "message": "拓扑排序仅适用于有向图，当前图为无向图",
            }
        if not nx.is_directed_acyclic_graph(g):
            sample = list(islice(nx.simple_cycles(g), 5))
            return {
                "success": False,
                "is_dag": False,
                "message": "图中存在环，无法进行拓扑排序",
                "sample_cycles": sample,
            }
        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        total_nodes = g.number_of_nodes()
        order_preview = list(islice(nx.topological_sort(g), eff))
        truncated = total_nodes > len(order_preview)
        return {
            "success": True,
            "is_dag": True,
            "total_nodes": total_nodes,
            "topological_order_preview": order_preview,
            "truncated": truncated,
            "note": f"仅展示前 {eff} 个节点，共 {total_nodes} 个" if truncated else None,
        }

    @staticmethod
    @analysis_contract
    def degree_analysis(g: nx.Graph, *, limit: int | None = None) -> dict:
        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        if g.is_directed():
            in_deg = dict(g.in_degree())
            out_deg = dict(g.out_degree())
            n_count = len(in_deg)

            in_values = list(in_deg.values())
            out_values = list(out_deg.values())
            total_values = [in_deg[n] + out_deg[n] for n in g.nodes()]

            sorted_by_total = sorted(
                g.nodes(), key=lambda n: in_deg[n] + out_deg[n], reverse=True
            )
            top_nodes_preview = [
                {"node": n, "in": in_deg[n], "out": out_deg[n], "total": in_deg[n] + out_deg[n]}
                for n in sorted_by_total[:eff]
            ]
            low_nodes_preview = [
                {"node": n, "in": in_deg[n], "out": out_deg[n], "total": in_deg[n] + out_deg[n]}
                for n in sorted_by_total[-10:]
            ]

            return {
                "success": True,
                "directed": True,
                "total_nodes": n_count,
                "in_degree_stats": _degree_stats(in_values),
                "out_degree_stats": _degree_stats(out_values),
                "total_degree_stats": _degree_stats(total_values),
                "avg_in_degree": round(sum(in_values) / max(n_count, 1), 4),
                "avg_out_degree": round(sum(out_values) / max(n_count, 1), 4),
                "top_degree_nodes": top_nodes_preview,
                "low_degree_nodes": low_nodes_preview,
                "truncated": eff is not None and n_count > eff,
            }
        else:
            deg = dict(g.degree())
            n_count = len(deg)
            deg_values = list(deg.values())
            sorted_nodes = sorted(g.nodes(), key=lambda n: deg[n], reverse=True)
            top_nodes_preview = [
                {"node": n, "degree": deg[n]} for n in sorted_nodes[:eff]
            ]
            low_nodes_preview = [
                {"node": n, "degree": deg[n]} for n in sorted_nodes[-10:]
            ]
            return {
                "success": True,
                "directed": False,
                "total_nodes": n_count,
                "degree_stats": _degree_stats(deg_values),
                "avg_degree": round(sum(deg_values) / max(n_count, 1), 4),
                "top_degree_nodes": top_nodes_preview,
                "low_degree_nodes": low_nodes_preview,
                "truncated": eff is not None and n_count > eff,
            }

    @staticmethod
    @analysis_contract
    def subgraph_extraction(
        g: nx.DiGraph,
        method: str,
        *,
        node: str | None = None,
        k: int = 2,
        nodes: list[str] | None = None,
        limit: int | None = None,
        include_full_nodes: bool = False,
    ) -> dict:
        try:
            if method == "ego_graph":
                if not node or node not in g:
                    return {"success": False, "error": f"节点 '{node}' 不存在"}
                sub = nx.ego_graph(g, node, radius=max(int(k), 1), undirected=not g.is_directed())
            elif method == "k_core":
                sub = nx.k_core(g, k=max(int(k), 1))
            elif method == "k_truss":
                sub = nx.k_truss(g.to_undirected(), k=max(int(k), 2))
            elif method == "k_corona":
                requested_k = max(int(k), 1)
                if g.is_directed():
                    # Directed core degree is in-degree + out-degree.  Apply
                    # the same definition to the degree inside the surviving
                    # core when selecting its corona.
                    core = nx.k_core(g, k=requested_k)
                    corona_nodes = [
                        current
                        for current in core
                        if core.in_degree(current) + core.out_degree(current)
                        == requested_k
                    ]
                    sub = core.subgraph(corona_nodes).copy()
                else:
                    sub = nx.k_corona(g, k=requested_k)
            elif method == "node_subset":
                picked = [name for name in (nodes or []) if name in g]
                sub = g.subgraph(picked).copy()
            else:
                return {"success": False, "error": f"未知方法 '{method}'"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        all_nodes = sorted(sub.nodes())
        all_edges = [{"from": u, "to": v, "weight": d.get("weight", 1.0),
                      **({"edge_id": d["id"]} if "id" in d else {})} for u, v, d in sub.edges(data=True)]
        nodes_preview, nodes_truncated = _truncate(all_nodes, eff)
        edges_preview, edges_truncated = _truncate(all_edges, eff)

        result = {
            "success": True,
            "method": method,
            "num_nodes": sub.number_of_nodes(),
            "num_edges": sub.number_of_edges(),
            "nodes_preview": nodes_preview,
            "nodes_truncated": nodes_truncated,
            "edges_preview": edges_preview,
            "edges_truncated": edges_truncated,
        }
        if include_full_nodes:
            result["_full_nodes"] = all_nodes
        return result

    @staticmethod
    @analysis_contract
    def link_prediction_score(
        g: nx.DiGraph,
        method: str,
        *,
        source: str | None = None,
        target: str | None = None,
        top_k: int = 10,
    ) -> dict:
        ug = g.to_undirected()
        if method not in {"jaccard", "adamic_adar", "resource_allocation", "preferential_attachment"}:
            return {"success": False, "error": f"未知方法 '{method}'"}

        def _score_pair_iter(pair_iter):
            # Score one (u, v) at a time — NetworkX vectorised helpers don't
            # accept a true generator for ebunch on all backends, and this
            # keeps heap memory bounded.  Use list() instead of next() to
            # avoid triggering PEP 479: a StopIteration from an empty result
            # inside a generator is converted to RuntimeError in Python 3.7+.
            for u, v in pair_iter:
                if method == "jaccard":
                    result = list(nx.jaccard_coefficient(ug, [(u, v)]))
                elif method == "adamic_adar":
                    result = list(nx.adamic_adar_index(ug, [(u, v)]))
                elif method == "resource_allocation":
                    result = list(nx.resource_allocation_index(ug, [(u, v)]))
                else:
                    result = list(nx.preferential_attachment(ug, [(u, v)]))
                if result:
                    yield (u, v, float(result[0][2]))

        if source is not None and target is not None:
            err = GraphCompute._check_nodes(
                ug, [(source, "源点"), (target, "目标点")]
            )
            if err:
                return err
            triples = list(_score_pair_iter([(source, target)]))
            return {
                "success": True,
                "method": method,
                "scores": [
                    {"source": u, "target": v, "score": round(s, 6)}
                    for u, v, s in triples
                ],
            }

        capped_k = max(1, min(int(top_k), _MAX_LIST_ITEMS))
        # Bounded-heap top-K over the streaming non_edges iterator: memory
        # stays O(k) regardless of |V|^2 candidate pairs.
        top = heapq.nlargest(
            capped_k,
            _score_pair_iter(nx.non_edges(ug)),
            key=lambda triple: triple[2],
        )
        return {
            "success": True,
            "method": method,
            "scores": [
                {"source": u, "target": v, "score": round(s, 6)}
                for u, v, s in top
            ],
        }

    @staticmethod
    @analysis_contract
    def path_analysis(
        g: nx.DiGraph,
        method: str,
        *,
        source: str | None = None,
        target: str | None = None,
        cutoff: int = 5,
        limit: int | None = None,
    ) -> dict:
        if method == "all_simple_paths":
            if not source or not target:
                return {"success": False, "error": "source 与 target 为必填项"}
            err = GraphCompute._check_nodes(
                g, [(source, "起点"), (target, "终点")]
            )
            if err:
                return err
            eff = _effective_limit(limit, _MAX_PATHS)
            paths: list = []
            truncated = False
            for p in nx.all_simple_paths(g, source, target, cutoff=max(int(cutoff), 1)):
                paths.append(p)
                if eff is not None and len(paths) > eff:
                    paths.pop()
                    truncated = True
                    break
            return {
                "success": True,
                "method": method,
                "count": len(paths),
                "paths": paths,
                "truncated": truncated,
                "note": f"最多返回 {eff} 条路径" if truncated else None,
            }
        if method == "has_path":
            if not source or not target:
                return {"success": False, "error": "source 与 target 为必填项"}
            err = GraphCompute._check_nodes(
                g, [(source, "起点"), (target, "终点")]
            )
            if err:
                return err
            return {"success": True, "method": method, "has_path": nx.has_path(g, source, target)}
        if method == "average_shortest_path_length":
            if g.number_of_nodes() == 0:
                return {
                    "success": False,
                    "error": "空图没有平均最短路径长度",
                }
            if g.is_directed():
                if not nx.is_strongly_connected(g):
                    return {
                        "success": False,
                        "error": "有向图不是强连通图，无法计算全图平均最短路径长度",
                    }
                measured = g
            else:
                measured = (
                    g
                    if nx.is_connected(g)
                    else g.subgraph(max(nx.connected_components(g), key=len)).copy()
                )
            return {
                "success": True,
                "method": method,
                "value": round(
                    float(nx.average_shortest_path_length(measured, weight="weight")),
                    6,
                ),
            }
        return {"success": False, "error": f"未知方法 '{method}'"}

    @staticmethod
    @analysis_contract
    def clique_analysis(g: nx.DiGraph, method: str, *, node: str | None = None, limit: int | None = None) -> dict:
        ug = g.to_undirected()
        if method == "find_cliques":
            eff = _effective_limit(limit, _MAX_LIST_ITEMS)
            if eff is None:
                cliques = [sorted(clique) for clique in nx.find_cliques(ug)]
                truncated = False
            else:
                sampled = list(islice(nx.find_cliques(ug), eff + 1))
                truncated = len(sampled) > eff
                cliques = [sorted(clique) for clique in sampled[:eff]]
            return {
                "success": True,
                "method": method,
                "cliques": cliques,
                "truncated": truncated,
                "total_cliques_at_least": len(cliques) + (1 if truncated else 0),
                "note": (
                    f"已展示 {len(cliques)} 个极大团；图中可能存在更多。"
                    if truncated
                    else None
                ),
            }
        if method == "clique_number":
            value = max((len(c) for c in nx.find_cliques(ug)), default=0)
            return {"success": True, "method": method, "value": value}
        if method == "number_of_cliques":
            if node:
                if node not in ug:
                    return {"success": False, "error": f"节点 '{node}' 不存在"}
                return {"success": True, "method": method, "node": node, "value": nx.number_of_cliques(ug, nodes=node)}
            values = nx.number_of_cliques(ug)
            # 返回统计摘要 + top 节点
            if isinstance(values, dict):
                sorted_vals = sorted(values.items(), key=lambda x: x[1], reverse=True)
                eff2 = _effective_limit(limit, _MAX_LIST_ITEMS)
                top_preview = [{"node": n, "clique_count": v} for n, v in (sorted_vals if eff2 is None else sorted_vals[:eff2])]
                return {
                    "success": True,
                    "method": method,
                    "total_nodes": len(values),
                    "top_nodes": top_preview,
                    "truncated": eff2 is not None and len(values) > eff2,
                }
            return {"success": True, "method": method, "values": values}
        return {"success": False, "error": f"未知方法 '{method}'"}

    @staticmethod
    @analysis_contract
    def reachability(g: nx.DiGraph, method: str, *, node: str | None = None, limit: int | None = None) -> dict:
        if method == "transitive_closure":
            # Materialize closure edges via per-node descendant traversal —
            # avoids building the full closure graph (worst case O(V^2)) just
            # to throw most of it away. limit=None materializes everything.
            edges_preview: list[dict] = []
            eff = _effective_limit(limit, _MAX_LIST_ITEMS)
            truncated = False
            for u in g.nodes():
                if truncated:
                    break
                for v in nx.descendants(g, u):
                    edges_preview.append({"from": u, "to": v})
                    if eff is not None and len(edges_preview) > eff:
                        truncated = True
                        break
            if eff is not None:
                edges_preview = edges_preview[:eff]
            return {
                "success": True,
                "method": method,
                "edges_preview": edges_preview,
                "truncated": truncated,
                "num_edges_at_least": len(edges_preview) + (1 if truncated else 0),
                "note": (
                    f"已展示前 {len(edges_preview)} 条传递闭包边；完整闭包未物化。"
                    if truncated
                    else None
                ),
            }
        if not node or node not in g:
            return {"success": False, "error": f"节点 '{node}' 不存在"}
        if method == "descendants":
            all_nodes = sorted(nx.descendants(g, node))
            nodes_preview, truncated = _truncate(all_nodes, _effective_limit(limit, _MAX_LIST_ITEMS))
            return {
                "success": True,
                "method": method,
                "node": node,
                "total_count": len(all_nodes),
                "nodes_preview": nodes_preview,
                "truncated": truncated,
            }
        if method == "ancestors":
            all_nodes = sorted(nx.ancestors(g, node))
            nodes_preview, truncated = _truncate(all_nodes, _effective_limit(limit, _MAX_LIST_ITEMS))
            return {
                "success": True,
                "method": method,
                "node": node,
                "total_count": len(all_nodes),
                "nodes_preview": nodes_preview,
                "truncated": truncated,
            }
        return {"success": False, "error": f"未知方法 '{method}'"}

    @staticmethod
    @analysis_contract
    def global_metrics(g: nx.DiGraph, metric: str) -> dict:
        ug = g.to_undirected()
        largest = ug.subgraph(max(nx.connected_components(ug), key=len)).copy() if ug.number_of_nodes() else ug
        undefined_reason_code: str | None = None

        def degree_assortativity() -> float:
            nonlocal undefined_reason_code
            if ug.number_of_edges() == 0:
                undefined_reason_code = "no_edges"
                return float("nan")
            endpoint_degrees = {
                int(ug.degree(node))
                for source, target in ug.edges()
                for node in (source, target)
            }
            if len(endpoint_degrees) <= 1:
                undefined_reason_code = "zero_endpoint_degree_variance"
                return float("nan")
            value = float(nx.degree_assortativity_coefficient(ug))
            if not math.isfinite(value):
                undefined_reason_code = "zero_endpoint_degree_variance"
            return value

        dispatch = {
            "density": lambda: nx.density(g),
            "diameter": lambda: nx.diameter(largest) if largest.number_of_nodes() else 0,
            "radius": lambda: nx.radius(largest) if largest.number_of_nodes() else 0,
            "transitivity": lambda: nx.transitivity(ug),
            "average_clustering": lambda: (
                nx.average_clustering(ug, weight="weight")
                if ug.number_of_nodes()
                else 0.0
            ),
            "degree_assortativity": degree_assortativity,
        }
        if metric not in dispatch:
            return {"success": False, "error": f"未知指标 '{metric}'"}
        value = float(dispatch[metric]())
        finite = math.isfinite(value)
        result = {
            "success": True,
            "metric": metric,
            "value": round(value, 6) if finite else None,
            "defined": finite,
        }
        if metric == "degree_assortativity" and not finite:
            result.update(
                _undefined_assortativity(
                    undefined_reason_code or "zero_endpoint_degree_variance"
                )
            )
        return result

    # ── YiGraph 对标扩展算子 ───────────────────────────────────────

    @staticmethod
    @analysis_contract
    def graph_structure_analysis(
        g: nx.DiGraph,
        method: str,
        *,
        source: str | None = None,
        depth_limit: int | None = None,
        iterations: int = 3,
        limit: int | None = None,
    ) -> dict:
        """图结构判定、遍历、平面性、二部图与 WL 哈希。"""
        ug = g.to_undirected()
        scalar_dispatch = {
            "is_tree": lambda: nx.is_tree(ug),
            "is_forest": lambda: nx.is_forest(ug),
            "is_directed_acyclic_graph": lambda: nx.is_directed_acyclic_graph(g),
            "is_at_free": lambda: nx.is_at_free(ug),
            "is_tournament": lambda: nx.is_tournament(g),
            "is_bipartite": lambda: nx.is_bipartite(ug),
        }
        if method in scalar_dispatch:
            return {"success": True, "method": method, "value": bool(scalar_dispatch[method]())}
        if method == "check_planarity":
            planar, _embedding = nx.check_planarity(ug)
            return {"success": True, "method": method, "is_planar": bool(planar)}
        if method in {"bfs_tree", "dfs_tree"}:
            if not source or source not in g:
                return {"success": False, "error": f"节点 '{source}' 不存在"}
            tree = (
                nx.bfs_tree(g, source, depth_limit=depth_limit)
                if method == "bfs_tree"
                else nx.dfs_tree(g, source, depth_limit=depth_limit)
            )
            edge_rows = [{"from": u, "to": v} for u, v in tree.edges()]
            eff = _effective_limit(limit, _MAX_LIST_ITEMS)
            preview, truncated = _truncate(edge_rows, eff)
            return {
                "success": True,
                "method": method,
                "source": source,
                "num_nodes": tree.number_of_nodes(),
                "num_edges": tree.number_of_edges(),
                "edges_preview": preview,
                "truncated": truncated,
            }
        if method == "lexicographical_topological_sort":
            if not nx.is_directed_acyclic_graph(g):
                return {"success": False, "error": "该方法要求有向无环图（DAG）"}
            order = list(nx.lexicographical_topological_sort(g, key=str))
            preview, truncated = _truncate(order, _effective_limit(limit, _MAX_LIST_ITEMS))
            return {"success": True, "method": method, "order": preview, "total_nodes": len(order), "truncated": truncated}
        if method == "all_topological_sorts":
            if not nx.is_directed_acyclic_graph(g):
                return {"success": False, "error": "该方法要求有向无环图（DAG）"}
            # 拓扑序可能指数爆炸；即使请求 all 也保留 200 条硬上限。
            eff = _effective_limit(limit, _MAX_LIST_ITEMS) or 200
            rows = list(islice(nx.all_topological_sorts(g), eff + 1))
            truncated = len(rows) > eff
            return {"success": True, "method": method, "orders": rows[:eff], "truncated": truncated, "hard_limit": 200}
        if method == "score_sequence":
            if not g.is_directed():
                return {"success": False, "error": "score_sequence 要求有向图"}
            sequence = sorted((int(g.out_degree(node)) for node in g.nodes()), reverse=True)
            ascending = list(reversed(sequence))
            prefix_sum = 0
            is_score_sequence = True
            for size, score in enumerate(ascending, start=1):
                prefix_sum += score
                if score < 0 or score >= len(sequence) or prefix_sum < size * (size - 1) // 2:
                    is_score_sequence = False
                    break
            is_score_sequence = (
                is_score_sequence
                and sum(sequence) == len(sequence) * (len(sequence) - 1) // 2
            )
            preview, truncated = _truncate(
                sequence,
                _effective_limit(limit, _MAX_LIST_ITEMS),
            )
            return {
                "success": True,
                "method": method,
                "score_sequence": preview,
                "total_nodes": len(sequence),
                "truncated": truncated,
                "is_tournament_score_sequence": is_score_sequence,
            }
        if method == "weisfeiler_lehman_graph_hash":
            return {
                "success": True,
                "method": method,
                "iterations": max(1, int(iterations)),
                "hash": nx.weisfeiler_lehman_graph_hash(g, iterations=max(1, int(iterations))),
            }
        return {"success": False, "error": f"未知方法 '{method}'"}

    @staticmethod
    @analysis_contract
    def connectivity_analysis(
        g: nx.DiGraph,
        method: str,
        *,
        source: str | None = None,
        target: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """点/边连通度、割集、割点、桥及缩点图。"""
        ug = g.to_undirected()
        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        if method in {"node_connectivity", "edge_connectivity"}:
            fn = nx.node_connectivity if method == "node_connectivity" else nx.edge_connectivity
            if bool(source) != bool(target):
                return {"success": False, "error": "source 与 target 必须同时提供或同时省略"}
            if source and (source not in g or target not in g):
                return {"success": False, "error": "source 或 target 节点不存在"}
            value = fn(ug, source, target) if source else fn(ug)
            return {"success": True, "method": method, "source": source, "target": target, "value": int(value)}
        if method in {"minimum_node_cut", "minimum_edge_cut"}:
            if bool(source) != bool(target):
                return {"success": False, "error": "source 与 target 必须同时提供或同时省略"}
            if source and (source not in g or target not in g):
                return {"success": False, "error": "source 或 target 节点不存在"}
            if not source and (ug.number_of_nodes() == 0 or not nx.is_connected(ug)):
                return {"success": False, "error": "全局最小割要求非空连通图"}
            fn = nx.minimum_node_cut if method == "minimum_node_cut" else nx.minimum_edge_cut
            cut = fn(ug, source, target) if source else fn(ug)
            if method == "minimum_node_cut":
                rows = sorted(cut, key=str)
                key = "nodes"
            else:
                rows = [{"from": u, "to": v} for u, v in sorted(cut, key=lambda item: (str(item[0]), str(item[1])))]
                key = "edges"
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "method": method, "size": len(cut), key: preview, "truncated": truncated}
        if method == "articulation_points":
            rows = sorted(nx.articulation_points(ug), key=str)
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "method": method, "count": len(rows), "nodes": preview, "truncated": truncated}
        if method in {"bridges", "local_bridges", "biconnected_component_edges"}:
            if method == "bridges":
                rows = [{"from": u, "to": v} for u, v in nx.bridges(ug)]
            elif method == "local_bridges":
                rows = [
                    {"from": u, "to": v, "span": None if span == float("inf") else span}
                    for u, v, span in nx.local_bridges(ug, with_span=True, weight="weight")
                ]
            else:
                rows = [
                    [{"from": u, "to": v} for u, v in component]
                    for component in nx.biconnected_component_edges(ug)
                ]
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "method": method, "count": len(rows), "items": preview, "truncated": truncated}
        if method == "bridge_components":
            bridge_free = ug.copy()
            bridge_free.remove_edges_from(nx.bridges(ug))
            components = [sorted(nodes, key=str) for nodes in nx.connected_components(bridge_free)]
            components.sort(key=len, reverse=True)
            preview, truncated = _truncate(components, eff)
            return {"success": True, "method": method, "count": len(components), "components": preview, "truncated": truncated}
        if method == "condensation":
            if not g.is_directed():
                return {"success": False, "error": "condensation 要求有向图"}
            condensed = nx.condensation(g)
            nodes = [
                {"id": node, "members": sorted(data.get("members", []), key=str)}
                for node, data in condensed.nodes(data=True)
            ]
            edges = [{"from": u, "to": v} for u, v in condensed.edges()]
            nodes_preview, nodes_truncated = _truncate(nodes, eff)
            edges_preview, edges_truncated = _truncate(edges, eff)
            return {
                "success": True,
                "method": method,
                "num_components": condensed.number_of_nodes(),
                "nodes": nodes_preview,
                "edges": edges_preview,
                "truncated": nodes_truncated or edges_truncated,
            }
        return {"success": False, "error": f"未知方法 '{method}'"}

    @staticmethod
    @analysis_contract
    def tree_dag_analysis(
        g: nx.DiGraph,
        method: str,
        *,
        source: str | None = None,
        node1: str | None = None,
        node2: str | None = None,
        seed: int = 42,
        limit: int | None = None,
    ) -> dict:
        """生成树、DAG 最长路、支配关系、反链和碰撞结构。"""
        ug = g.to_undirected()
        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        if method in {"maximum_spanning_tree", "random_spanning_tree"}:
            if ug.number_of_nodes() and not nx.is_connected(ug):
                return {"success": False, "error": "该方法要求连通图"}
            tree = (
                nx.maximum_spanning_tree(ug, weight="weight")
                if method == "maximum_spanning_tree"
                else nx.random_spanning_tree(ug, weight="weight", seed=int(seed))
            )
            rows = [{"from": u, "to": v, "weight": float(data.get("weight", 1.0))} for u, v, data in tree.edges(data=True)]
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "method": method, "num_edges": len(rows), "total_weight": round(sum(row["weight"] for row in rows), 6), "edges": preview, "truncated": truncated}
        if method == "lowest_common_ancestor":
            if not node1 or not node2 or node1 not in g or node2 not in g:
                return {"success": False, "error": "node1 和 node2 必须是图中的节点"}
            if not nx.is_directed_acyclic_graph(g):
                return {"success": False, "error": "lowest_common_ancestor 要求 DAG"}
            return {"success": True, "method": method, "node1": node1, "node2": node2, "ancestor": nx.lowest_common_ancestor(g, node1, node2)}
        if method in {"immediate_dominators", "dominance_frontiers"}:
            if not g.is_directed():
                return {"success": False, "error": "支配关系分析要求有向图"}
            if not source or source not in g:
                return {"success": False, "error": f"起点 '{source}' 不存在"}
            values = nx.immediate_dominators(g, source) if method == "immediate_dominators" else nx.dominance_frontiers(g, source)
            rows = [
                {"node": node, "value": sorted(value, key=str) if isinstance(value, set) else value}
                for node, value in values.items()
            ]
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "method": method, "source": source, "count": len(rows), "items": preview, "truncated": truncated}
        if method in {"dag_longest_path", "dag_longest_path_length"}:
            if not nx.is_directed_acyclic_graph(g):
                return {"success": False, "error": "该方法要求 DAG"}
            if method == "dag_longest_path":
                path = nx.dag_longest_path(g, weight="weight")
                preview, truncated = _truncate(path, eff)
                return {"success": True, "method": method, "path": preview, "path_nodes": len(path), "truncated": truncated}
            return {"success": True, "method": method, "value": float(nx.dag_longest_path_length(g, weight="weight"))}
        if method == "antichains":
            if not nx.is_directed_acyclic_graph(g):
                return {"success": False, "error": "antichains 要求 DAG"}
            cap = eff or 200
            rows = list(islice(nx.antichains(g), cap + 1))
            return {"success": True, "method": method, "antichains": rows[:cap], "truncated": len(rows) > cap, "hard_limit": 200}
        if method == "is_aperiodic":
            if not g.is_directed():
                return {"success": False, "error": "is_aperiodic 要求有向图"}
            if g.number_of_nodes() == 0 or not nx.is_strongly_connected(g):
                return {"success": False, "error": "is_aperiodic 要求非空强连通有向图"}
            return {"success": True, "method": method, "value": bool(nx.is_aperiodic(g))}
        if method in {"colliders", "v_structures"}:
            if not g.is_directed():
                return {"success": False, "error": "碰撞结构分析要求有向图"}

            def iter_structures():
                for center in g.nodes():
                    for left, right in combinations(g.predecessors(center), 2):
                        if method == "v_structures" and (
                            g.has_edge(left, right) or g.has_edge(right, left)
                        ):
                            continue
                        yield {"left": left, "center": center, "right": right}

            # These methods are quadratic in a node's in-degree.  Keep a hard
            # cap even when callers request "all", just like topology/antichain
            # enumeration, so a high-degree node cannot exhaust worker memory.
            cap = eff or 200
            sampled = list(islice(iter_structures(), cap + 1))
            truncated = len(sampled) > cap
            preview = sampled[:cap]
            exact_count = None
            if method == "colliders":
                exact_count = sum(
                    degree * (degree - 1) // 2
                    for _, degree in g.in_degree()
                )
            elif not truncated:
                exact_count = len(preview)
            return {
                "success": True,
                "method": method,
                "count": exact_count,
                "returned_count": len(preview),
                "count_is_exact": exact_count is not None,
                "structures": preview,
                "truncated": truncated,
                "hard_limit": 200,
            }
        return {"success": False, "error": f"未知方法 '{method}'"}

    @staticmethod
    @analysis_contract
    def matching_coloring_analysis(
        g: nx.DiGraph,
        method: str,
        *,
        edges: list | None = None,
        maxcardinality: bool = False,
        strategy: str = "largest_first",
        num_colors: int | None = None,
        limit: int | None = None,
    ) -> dict:
        """匹配、边覆盖与图着色。"""
        ug = g.to_undirected()
        eff = _effective_limit(limit, _MAX_LIST_ITEMS)

        def matching_rows(matching: Any) -> list[dict]:
            pairs = []
            for item in matching:
                u, v = tuple(item)
                pairs.append({"from": u, "to": v, "weight": float(ug.get_edge_data(u, v, {}).get("weight", 1.0))})
            return sorted(pairs, key=lambda row: (str(row["from"]), str(row["to"])))

        if method in {"max_weight_matching", "min_weight_matching", "maximal_matching"}:
            if method == "max_weight_matching":
                matching = nx.max_weight_matching(ug, maxcardinality=bool(maxcardinality), weight="weight")
            elif method == "min_weight_matching":
                matching = nx.min_weight_matching(ug, weight="weight")
            else:
                matching = nx.maximal_matching(ug)
            rows = matching_rows(matching)
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "method": method, "size": len(rows), "total_weight": round(sum(row["weight"] for row in rows), 6), "matching": preview, "truncated": truncated}
        if method == "min_edge_cover":
            if any(degree == 0 for _, degree in ug.degree()):
                return {"success": False, "error": "存在孤立节点，无法计算边覆盖"}
            cover = nx.min_edge_cover(ug)
            rows = matching_rows(cover)
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "method": method, "size": len(rows), "edges": preview, "truncated": truncated}
        if method == "is_matching":
            parsed = []
            for edge in edges or []:
                if isinstance(edge, (list, tuple)) and len(edge) == 2:
                    parsed.append((edge[0], edge[1]))
                elif isinstance(edge, dict) and "from" in edge and "to" in edge:
                    parsed.append((edge["from"], edge["to"]))
                else:
                    return {"success": False, "error": "edges 中每项必须是 [u,v] 或 {from,to}"}
            return {"success": True, "method": method, "value": bool(nx.is_matching(ug, parsed)), "edge_count": len(parsed)}
        if method in {"greedy_color", "equitable_color"}:
            if method == "greedy_color":
                coloring = nx.greedy_color(ug, strategy=strategy)
            else:
                if not num_colors or int(num_colors) < 1:
                    return {"success": False, "error": "equitable_color 要求 num_colors >= 1"}
                maximum_degree = max((degree for _, degree in ug.degree()), default=0)
                if int(num_colors) <= maximum_degree:
                    return {
                        "success": False,
                        "error": f"equitable_color 要求 num_colors > 最大度数 {maximum_degree}",
                    }
                coloring = nx.equitable_color(ug, int(num_colors))
            rows = [{"node": node, "color": int(color)} for node, color in coloring.items()]
            rows.sort(key=lambda row: (row["color"], str(row["node"])))
            preview, truncated = _truncate(rows, eff)
            used = len(set(coloring.values()))
            return {"success": True, "method": method, "num_colors": used, "coloring": preview, "truncated": truncated}
        return {"success": False, "error": f"未知方法 '{method}'"}

    @staticmethod
    @analysis_contract
    def distance_measures(g: nx.DiGraph, metric: str, *, limit: int | None = None) -> dict:
        """扩展距离、效率、邻居度、富俱乐部与有向层级指标。"""
        ug = g.to_undirected()
        if ug.number_of_nodes():
            largest_nodes = max(nx.connected_components(ug), key=len)
            largest = ug.subgraph(largest_nodes).copy()
        else:
            largest = ug
        eff = _effective_limit(limit, _MAX_LIST_ITEMS)
        if metric == "flow_hierarchy" and not g.is_directed():
            return {"success": False, "error": "flow_hierarchy 要求有向图"}
        if metric == "girth":
            value = nx.girth(ug)
            if value == float("inf"):
                return {
                    "success": True,
                    "metric": metric,
                    "value": None,
                    "acyclic": True,
                }
            return {
                "success": True,
                "metric": metric,
                "value": int(value),
                "acyclic": False,
            }
        scalar_dispatch = {
            "wiener_index": lambda: nx.wiener_index(largest) if largest.number_of_nodes() else 0,
            "global_efficiency": lambda: nx.global_efficiency(ug),
            "local_efficiency": lambda: nx.local_efficiency(ug),
            "reciprocity": lambda: nx.reciprocity(g) if g.number_of_edges() else 0,
            "flow_hierarchy": lambda: nx.flow_hierarchy(g, weight="weight") if g.number_of_edges() else 0,
            "estrada_index": lambda: nx.estrada_index(ug),
        }
        if metric in scalar_dispatch:
            value = scalar_dispatch[metric]()
            value = 0 if value is None else value
            return {"success": True, "metric": metric, "value": round(float(value), 6)}
        if metric in {"center", "periphery", "barycenter"}:
            fn = {"center": nx.center, "periphery": nx.periphery, "barycenter": nx.barycenter}[metric]
            rows = fn(largest, weight="weight") if metric == "barycenter" else fn(largest)
            preview, truncated = _truncate(list(rows), eff)
            return {"success": True, "metric": metric, "nodes": preview, "count": len(rows), "truncated": truncated, "scope": "largest_connected_component"}
        mapping_dispatch = {
            "eccentricity": lambda: nx.eccentricity(largest),
            "average_neighbor_degree": lambda: nx.average_neighbor_degree(g, weight="weight"),
            "average_degree_connectivity": lambda: nx.average_degree_connectivity(g, weight="weight"),
            "rich_club_coefficient": lambda: nx.rich_club_coefficient(ug, normalized=False),
        }
        if metric in mapping_dispatch:
            values = mapping_dispatch[metric]()
            rows = [{"key": key, "value": round(float(value), 6)} for key, value in values.items()]
            rows.sort(key=lambda row: (-row["value"], str(row["key"])))
            preview, truncated = _truncate(rows, eff)
            return {"success": True, "metric": metric, "count": len(rows), "values": preview, "truncated": truncated}
        return {"success": False, "error": f"未知指标 '{metric}'"}

    # ── 第 4 批新增（图可视化）───────────────────────────────────────

    # 高亮默认调色板
    _HIGHLIGHT_PALETTE = [
        "#ef4444",  # 红
        "#22c55e",  # 绿
        "#f59e0b",  # 琥珀
        "#8b5cf6",  # 紫
        "#06b6d4",  # 青
        "#ec4899",  # 粉
        "#14b8a6",  # 蓝绿
        "#f97316",  # 橙
    ]

    @staticmethod
    def _normalize_highlights(
        highlights: Optional[dict],
        g: nx.DiGraph,
        visible_nodes: Optional[set] = None,
    ) -> Optional[dict]:
        """校验、补全 highlights 结构，自动分配颜色。

        highlights 统一格式:
        {
            "nodes": [
                {"ids": ["A","B"], "color": "#ff0000", "label": "关键节点"}
            ],
            "paths": [
                {"node_sequence": ["A","B","C"], "color": "#00ff00", "label": "最短路径"}
            ],
            "subgraphs": [
                {"nodes": ["A","B","C","D"], "color": "#ff9900", "label": "官能团"}
            ]
        }
        """
        if not isinstance(highlights, dict) or not highlights:
            return None

        palette = GraphCompute._HIGHLIGHT_PALETTE
        color_idx = 0

        def next_color():
            nonlocal color_idx
            c = palette[color_idx % len(palette)]
            color_idx += 1
            return c

        # Only emit highlights that the client can actually draw.  Passing the
        # already-selected set avoids building another all-graph node set and
        # prevents metadata from pointing at nodes removed by the overview cap.
        all_nodes = visible_nodes if visible_nodes is not None else set(g.nodes())
        result = {"nodes": [], "paths": [], "subgraphs": []}

        # Process node highlights
        for item in highlights.get("nodes", []):
            if not isinstance(item, dict):
                continue
            ids = item.get("ids", [])
            # Filter to existing nodes
            valid_ids = [n for n in ids if n in all_nodes]
            if valid_ids:
                result["nodes"].append({
                    "ids": valid_ids,
                    "color": item.get("color") or next_color(),
                    "label": item.get("label", ""),
                })

        # Process path highlights
        for item in highlights.get("paths", []):
            if not isinstance(item, dict):
                continue
            seq = item.get("node_sequence", [])
            valid_seq = [n for n in seq if n in all_nodes]
            if len(valid_seq) >= 2:
                # Derive edges from node sequence
                path_edges = []
                for i in range(len(valid_seq) - 1):
                    u, v = valid_seq[i], valid_seq[i + 1]
                    if g.has_edge(u, v) or g.has_edge(v, u):
                        path_edges.append([u, v])
                result["paths"].append({
                    "node_sequence": valid_seq,
                    "edges": path_edges,
                    "color": item.get("color") or next_color(),
                    "label": item.get("label", ""),
                })

        # Process subgraph highlights
        for item in highlights.get("subgraphs", []):
            if not isinstance(item, dict):
                continue
            node_list = item.get("nodes", [])
            valid_nodes = [n for n in node_list if n in all_nodes]
            if valid_nodes:
                # Derive edges within the subgraph
                valid_set = set(valid_nodes)
                sub_edges = [
                    [u, v]
                    for u, v in g.subgraph(valid_set).edges()
                ]
                result["subgraphs"].append({
                    "nodes": valid_nodes,
                    "edges": sub_edges,
                    "color": item.get("color") or next_color(),
                    "label": item.get("label", ""),
                })

        # Return None if nothing valid
        if not result["nodes"] and not result["paths"] and not result["subgraphs"]:
            return None
        return result

    # SVG 画布对小图直接全量渲染；大图由确定性智能概览收敛到该上限。
    _MAX_VIZ_NODES = int(os.environ.get("VISUALIZE_MAX_NODES", "800"))
    _MAX_VIZ_EDGES = int(os.environ.get("VISUALIZE_MAX_EDGES", "2000"))

    def visualize_subgraph(
        self,
        g: nx.DiGraph,
        gd: GraphData,
        node_names: Optional[List[str]] = None,
        include_community: bool = True,
        highlights: Optional[dict] = None,
        max_nodes: Optional[int] = None,
    ) -> dict:
        """获取图的完整结构用于可视化渲染。

        Args:
            node_names: 指定节点列表；为空则返回全图
            include_community: 是否包含社区信息，默认开启
            highlights: 高亮信息，统一格式:
                {
                    "nodes": [{"ids": [...], "color": "#hex", "label": "..."}],
                    "paths": [{"node_sequence": [...], "color": "#hex", "label": "..."}],
                    "subgraphs": [{"nodes": [...], "color": "#hex", "label": "..."}]
                }
                color 和 label 可选，会自动分配。
            max_nodes: 返回的最大节点数，默认 800。超出时生成确定性的智能概览：
                       保留高亮，覆盖节点类型和社区，优先高连接度节点，并补齐
                       代表节点之间的有界连接路径。
        """
        try:
            requested_cap = int(max_nodes) if max_nodes is not None else self._MAX_VIZ_NODES
        except (TypeError, ValueError):
            requested_cap = self._MAX_VIZ_NODES
        node_cap = max(1, requested_cap) if requested_cap > 0 else max(1, self._MAX_VIZ_NODES)
        edge_cap = max(1, self._MAX_VIZ_EDGES)

        # 确定要可视化的节点
        if node_names:
            # 验证节点存在性
            invalid = [n for n in node_names if not g.has_node(n)]
            if invalid:
                return {
                    "success": False,
                    "error": f"部分节点不存在: {invalid[:10]}",
                    "available_nodes": list(g.nodes())[:20],
                }
            target_nodes = set(node_names)
        else:
            target_nodes = set(g.nodes())

        # Keep source-scope totals before replacing the node set with the smart
        # overview.  For an explicitly requested subset these totals must refer
        # to that subset, not to the unrelated remainder of the session graph.
        source_nodes = set(target_nodes)
        source_total_nodes = len(source_nodes)
        source_total_edges = (
            g.subgraph(source_nodes).number_of_edges()
            if node_names
            else g.number_of_edges()
        )

        # ── 节点数 cap：超出上限时生成稳定、可复现的拓扑智能概览 ──────
        nodes_truncated = False
        overview_selection: Optional[SmartOverviewSelection] = None
        if len(target_nodes) > node_cap:
            required_nodes: set = set()
            if highlights and isinstance(highlights, dict):
                for item in highlights.get("nodes", []):
                    required_nodes.update(item.get("ids", []))
                for item in highlights.get("paths", []):
                    required_nodes.update(item.get("node_sequence", []))
                for item in highlights.get("subgraphs", []):
                    required_nodes.update(item.get("nodes", []))

            node_types: dict = {}
            for node in target_nodes:
                node_type = None
                try:
                    nid = gd.name_to_id(node) if gd.has_node_names else None
                except KeyError:
                    nid = None
                if nid is not None and gd.has_node_type:
                    try:
                        node_type = gd.get_node_type_by_id(nid)
                    except Exception:
                        node_type = None
                nx_attrs = g.nodes[node]
                node_type = node_type or nx_attrs.get("node_type") or nx_attrs.get("type")
                node_types[node] = str(node_type or "unknown")

            overview_selection = select_smart_overview(
                g,
                target_nodes,
                max_nodes=node_cap,
                node_types=node_types,
                required_nodes=required_nodes,
            )
            target_nodes = set(overview_selection.nodes)
            nodes_truncated = True

        # 构建节点列表
        nodes = []
        for node in sorted(target_nodes, key=lambda value: (type(value).__name__, repr(value))):
            try:
                nid = gd.name_to_id(node) if gd.has_node_names else None
            except KeyError:
                nid = None
            # ── 通用节点属性：收集所有可用的非数值型属性 ─────────────────
            attrs: dict = {}
            # 优先通过 GraphData 接口查询（按 id 索引，最准确）
            if nid is not None:
                if gd.has_node_type:
                    v = gd.get_node_type_by_id(nid)
                    if v is not None:
                        attrs["type"] = str(v)
                if gd.has_node_label:
                    try:
                        v = self._serialize_label(gd.get_node_label_by_id(nid))
                        if v is not None:
                            attrs["label"] = v
                    except Exception:
                        pass
                if gd.has_node_text:
                    try:
                        v = gd.get_node_text_by_id(nid)
                        if v:
                            text = str(v)
                            attrs["text"] = text[:300] + "..." if len(text) > 300 else text
                    except Exception:
                        pass
            # Fallback：从 NetworkX 节点属性读取（adapters.py 已将属性存入节点）
            nx_attrs = g.nodes[node]
            if isinstance(nx_attrs.get("properties"), dict):
                attrs["properties"] = nx_attrs["properties"]
            if not attrs.get("type") and nx_attrs.get("node_type"):
                attrs["type"] = str(nx_attrs["node_type"])
            if not attrs.get("label") and nx_attrs.get("node_label") is not None:
                attrs["label"] = str(nx_attrs["node_label"])
            if not attrs.get("text") and nx_attrs.get("text"):
                text = str(nx_attrs["text"])
                attrs["text"] = text[:300] + "..." if len(text) > 300 else text
            nodes.append({
                "id": node,
                "label": node,
                "in_degree": g.in_degree(node) if g.is_directed() else g.degree(node),
                "out_degree": g.out_degree(node) if g.is_directed() else 0,
                "total_degree": g.degree(node),
                "attrs": attrs if attrs else None,
            })

        # 构建边列表。即使节点数不超过 800，稠密图仍可能包含几十万条边；
        # 使用有界 top-k 而不是先物化全部边。连接路径/高亮路径各保留一条
        # 关系，其余边优先选择连接高连接度端点的边，并稳定地打破平局。
        display_graph = g.subgraph(target_nodes)
        available_display_edges = display_graph.number_of_edges()
        display_degrees = {node: int(g.degree(node)) for node in target_nodes}

        def undirected_pair(left, right):
            left_key = (f"{type(left).__module__}.{type(left).__qualname__}", repr(left))
            right_key = (f"{type(right).__module__}.{type(right).__qualname__}", repr(right))
            return (left, right) if left_key <= right_key else (right, left)

        protected_pairs = set()
        # Reserve a spanning forest before dense/parallel edges can consume the
        # edge budget. Preserve real topology without inventing relationships.
        if available_display_edges > edge_cap:
            undirected = display_graph.to_undirected(as_view=True)
            seen = set()
            for root in sorted(target_nodes, key=lambda n: (type(n).__name__, repr(n))):
                if root in seen:
                    continue
                seen.add(root)
                for left, right in nx.bfs_edges(undirected, root, sort_neighbors=lambda values: sorted(values, key=lambda n: (type(n).__name__, repr(n)))):
                    seen.add(right)
                    protected_pairs.add(undirected_pair(left, right))
        if overview_selection is not None:
            for path in overview_selection.connector_paths:
                protected_pairs.update(undirected_pair(left, right) for left, right in zip(path, path[1:]))
        if isinstance(highlights, dict):
            for item in highlights.get("paths", []):
                if not isinstance(item, dict):
                    continue
                path = [node for node in item.get("node_sequence", []) if node in target_nodes]
                protected_pairs.update(undirected_pair(left, right) for left, right in zip(path, path[1:]))

        def iter_edge_records():
            if display_graph.is_multigraph():
                yield from display_graph.edges(keys=True, data=True)
            else:
                for left, right, data in display_graph.edges(data=True):
                    yield left, right, None, data

        def edge_stable_key(record):
            left, right, key, data = record
            return (
                f"{type(left).__module__}.{type(left).__qualname__}", repr(left),
                f"{type(right).__module__}.{type(right).__qualname__}", repr(right),
                repr(data.get("id", data.get("name", key))),
                repr(data.get("type", data.get("edge_type", ""))),
                repr(key),
            )

        def edge_priority(record):
            left, right, _key, _data = record
            return (
                -(display_degrees.get(left, 0) + display_degrees.get(right, 0)),
                edge_stable_key(record),
            )

        # One canonical edge per protected path segment is selected first so a
        # large set of parallel relationships cannot crowd out another segment.
        protected_best = {}
        if protected_pairs and available_display_edges > edge_cap:
            for record in iter_edge_records():
                pair = undirected_pair(record[0], record[1])
                if pair not in protected_pairs:
                    continue
                previous = protected_best.get(pair)
                if previous is None or edge_stable_key(record) < edge_stable_key(previous):
                    protected_best[pair] = record
        protected_records = sorted(protected_best.values(), key=edge_stable_key)[:edge_cap]
        protected_keys = {edge_stable_key(record) for record in protected_records}
        remaining_edge_slots = edge_cap - len(protected_records)
        if available_display_edges <= edge_cap:
            overview_edges = sorted(iter_edge_records(), key=edge_stable_key)
        elif remaining_edge_slots > 0:
            overview_edges = protected_records + heapq.nsmallest(
                remaining_edge_slots,
                (
                    record for record in iter_edge_records()
                    if edge_stable_key(record) not in protected_keys
                ),
                key=edge_priority,
            )
        else:
            overview_edges = protected_records
        overview_edges.sort(key=edge_stable_key)

        edges = []
        for u, v, _key, data in overview_edges:
            edge = {"src": u, "dst": v, "weight": data.get("weight", 1.0)}
            edge_id = data.get("id", data.get("name"))
            edge_type = data.get("type", data.get("edge_type"))
            if edge_id is not None:
                edge["id"] = edge_id
            if edge_type is not None:
                edge["type"] = edge_type
            if "properties" in data:
                edge["properties"] = data["properties"]
            edges.append(edge)
        edges_truncated = available_display_edges > len(edges)

        # 社区检测（可选）
        community_info = None
        if include_community and len(target_nodes) > 1:
            try:
                communities = (
                    overview_selection.communities
                    if overview_selection is not None
                    else self._detect_communities(g.subgraph(target_nodes).to_undirected(), "louvain")
                )
                node_community = {}
                for i, comm in enumerate(communities):
                    for n in comm:
                        if n in target_nodes:
                            node_community[n] = i
                community_info = {
                    "num_communities": len(communities),
                    "node_community": node_community,
                }
            except Exception:
                pass

        # 标准化高亮信息
        normalized_highlights = self._normalize_highlights(highlights, g, target_nodes)

        result = {
            "degree_scope": "full_graph",
            "success": True,
            "directed": g.is_directed(),
            "num_nodes": len(nodes),
            "num_edges": len(edges),
            "nodes": nodes,
            "edges": edges,
            "community": community_info,
            "highlights": normalized_highlights,
        }
        if nodes_truncated or edges_truncated:
            result["truncated"] = True
            result["total_nodes"] = source_total_nodes
            result["total_edges"] = source_total_edges
            result["nodes_truncated"] = nodes_truncated
            result["edges_truncated"] = edges_truncated
            if nodes_truncated:
                result["truncated_to"] = len(nodes)
            if edges_truncated:
                result["truncated_edges_to"] = len(edges)
            result["overview"] = {
                "mode": "smart",
                "algorithm": "topology-types-communities-v1",
                "deterministic": True,
                "node_limit": node_cap,
                "edge_limit": edge_cap,
                "community_count": len(overview_selection.communities) if overview_selection is not None else 0,
                "community_representative_count": len(overview_selection.community_representatives) if overview_selection is not None else 0,
                "node_type_representative_count": len(overview_selection.type_representatives) if overview_selection is not None else 0,
                "required_node_count": len(overview_selection.required_nodes) if overview_selection is not None else 0,
                "required_nodes_truncated": overview_selection.required_truncated if overview_selection is not None else False,
                "connector_node_count": len(overview_selection.connector_nodes) if overview_selection is not None else 0,
                "connector_path_count": len(overview_selection.connector_paths) if overview_selection is not None else 0,
            }
        return result



NX_TOOL_NAMES: frozenset = frozenset({
    "shortest_path", "all_shortest_paths", "maximum_flow", "minimum_cut",
    "global_minimum_cut", "minimum_spanning_tree", "community_detection", "node_centrality",
    "graph_connectivity", "find_cycles", "maximum_cycle", "topological_sort", "degree_analysis",
    "subgraph_extraction", "link_prediction_score", "path_analysis",
    "clique_analysis", "reachability", "global_metrics",
    "graph_structure_analysis", "connectivity_analysis", "tree_dag_analysis",
    "matching_coloring_analysis", "distance_measures",
})


def run_nx_tool(tool_name: str, g: nx.DiGraph, arguments: dict) -> dict:
    """Picklable entry point for ProcessPoolExecutor.

    Each worker process has its own GIL, so multiple calls truly run on
    separate CPU cores without GIL contention.  Only tools that require
    nothing beyond an nx.DiGraph belong here.
    """
    c = GraphCompute()
    a = arguments or {}
    raw_limit = a.get("limit")
    if raw_limit is None:
        lim = None
    elif isinstance(raw_limit, str) and raw_limit.strip().lower() == "all":
        lim = -1  # _effective_limit treats <=0 as unlimited
    else:
        lim = int(raw_limit)

    if tool_name == "shortest_path":
        return c.shortest_path(
            g,
            str(a.get("source", "")),
            str(a.get("target", "")),
            bool(a.get("use_weight", True)),
            str(a.get("algorithm", "default")),
            int(a.get("k", 3)),
            limit=lim,
        )
    if tool_name == "all_shortest_paths":
        return c.all_shortest_paths(g, str(a.get("source", "")), str(a.get("target", "")), limit=lim)
    if tool_name == "maximum_flow":
        return c.maximum_flow(g, str(a.get("source", "")), str(a.get("sink", "")), limit=lim)
    if tool_name == "minimum_cut":
        return c.minimum_cut(g, str(a.get("source", "")), str(a.get("sink", "")))
    if tool_name == "global_minimum_cut":
        raw_undirected = a.get("treat_as_undirected", True)
        if isinstance(raw_undirected, str):
            treat_as_undirected = raw_undirected.strip().lower() not in {"0", "false", "no", "off"}
        else:
            treat_as_undirected = bool(raw_undirected)
        return c.global_minimum_cut(
            g,
            treat_as_undirected=treat_as_undirected,
            limit=lim,
        )
    if tool_name == "minimum_spanning_tree":
        return c.minimum_spanning_tree(g, limit=lim)
    if tool_name == "community_detection":
        return c.community_detection(
            g,
            str(a.get("algorithm", "louvain")),
            limit=lim,
            focus_nodes=(
                [str(node) for node in a.get("focus_nodes")]
                if isinstance(a.get("focus_nodes"), list)
                else None
            ),
        )
    if tool_name == "node_centrality":
        return c.node_centrality(g, str(a.get("centrality_type", "degree")), limit=lim)
    if tool_name == "graph_connectivity":
        return c.graph_connectivity(g, limit=lim)
    if tool_name == "find_cycles":
        return c.find_cycles(g, limit=lim)
    if tool_name == "maximum_cycle":
        return c.maximum_cycle(
            g,
            max_cycles=int(a.get("max_cycles", _MAX_CYCLE_SEARCH)),
            optimize_by=str(a.get("optimize_by", "length")),
        )
    if tool_name == "topological_sort":
        return c.topological_sort(g, limit=lim)
    if tool_name == "degree_analysis":
        return c.degree_analysis(g, limit=lim)
    if tool_name == "subgraph_extraction":
        return c.subgraph_extraction(
            g,
            str(a.get("method", "")),
            node=str(a.get("node") or "") or None,
            k=int(a.get("k", 2)),
            nodes=a.get("nodes") if isinstance(a.get("nodes"), list) else None,
            limit=lim,
        )
    if tool_name == "link_prediction_score":
        return c.link_prediction_score(
            g,
            str(a.get("method", "")),
            source=str(a.get("source") or "") or None,
            target=str(a.get("target") or "") or None,
            top_k=int(a.get("top_k", 10)),
        )
    if tool_name == "path_analysis":
        return c.path_analysis(
            g,
            str(a.get("method", "")),
            source=str(a.get("source") or "") or None,
            target=str(a.get("target") or "") or None,
            cutoff=int(a.get("cutoff", 5)),
            limit=lim,
        )
    if tool_name == "clique_analysis":
        return c.clique_analysis(
            g,
            str(a.get("method", "")),
            node=str(a.get("node") or "") or None,
            limit=lim,
        )
    if tool_name == "reachability":
        return c.reachability(
            g,
            str(a.get("method", "")),
            node=str(a.get("node") or "") or None,
            limit=lim,
        )
    if tool_name == "global_metrics":
        return c.global_metrics(g, str(a.get("metric", "")))
    if tool_name == "graph_structure_analysis":
        return c.graph_structure_analysis(
            g,
            str(a.get("method", "")),
            source=str(a.get("source") or "") or None,
            depth_limit=int(a["depth_limit"]) if a.get("depth_limit") is not None else None,
            iterations=int(a.get("iterations", 3)),
            limit=lim,
        )
    if tool_name == "connectivity_analysis":
        return c.connectivity_analysis(
            g,
            str(a.get("method", "")),
            source=str(a.get("source") or "") or None,
            target=str(a.get("target") or "") or None,
            limit=lim,
        )
    if tool_name == "tree_dag_analysis":
        return c.tree_dag_analysis(
            g,
            str(a.get("method", "")),
            source=str(a.get("source") or "") or None,
            node1=str(a.get("node1") or "") or None,
            node2=str(a.get("node2") or "") or None,
            seed=int(a.get("seed", 42)),
            limit=lim,
        )
    if tool_name == "matching_coloring_analysis":
        return c.matching_coloring_analysis(
            g,
            str(a.get("method", "")),
            edges=a.get("edges") if isinstance(a.get("edges"), list) else None,
            maxcardinality=bool(a.get("maxcardinality", False)),
            strategy=str(a.get("strategy", "largest_first")),
            num_colors=int(a["num_colors"]) if a.get("num_colors") is not None else None,
            limit=lim,
        )
    if tool_name == "distance_measures":
        return c.distance_measures(g, str(a.get("metric", "")), limit=lim)
    return {"success": False, "error": f"未知 nx 工具: {tool_name}"}

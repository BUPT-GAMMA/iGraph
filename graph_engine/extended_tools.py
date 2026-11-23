"""Contracts for additional, independently callable graph analysis tools.

This module has no numerical dependencies: MCP, the scheduler and the graph
engine consume exactly the same names, limits and argument schemas.
"""
from __future__ import annotations

from copy import deepcopy


_PAGE = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    "offset": {"type": "integer", "minimum": 0, "maximum": 20000, "default": 0},
}
_DIRECTION = {"type": "string", "enum": ["out", "in", "both"], "default": "both"}
_NODE = {"type": "string", "minLength": 1, "maxLength": 1024}
_NODES = {"type": "array", "items": _NODE, "minItems": 1, "maxItems": 100, "uniqueItems": True}

# Name, Chinese UI label, user-facing semantics, extra properties, required fields.
_RETRIEVAL = [
    ("list_graph_nodes", "分页查看图节点", "分页列出节点名称和类型；可按 node_type 精确过滤。", {"node_type": _NODE}, []),
    ("list_graph_edges", "分页查看图边", "分页列出边端点、类型和关系 ID；平行关系分别保留，可按 edge_type 精确过滤。", {"edge_type": _NODE}, []),
    ("get_node_type_counts", "统计节点类型", "统计各节点类型的节点数，缺失类型记为 null。", {}, []),
    ("get_edge_type_counts", "统计边类型", "统计各边类型的关系数，平行关系分别计数，缺失类型记为 null。", {}, []),
    ("get_isolated_nodes", "查询孤立节点", "查询总度为零的孤立节点。", {}, []),
    ("get_source_nodes", "查询源节点", "查询有向图中入度为零且出度大于零的节点；不含孤立节点。", {}, []),
    ("get_sink_nodes", "查询汇节点", "查询有向图中出度为零且入度大于零的节点；不含孤立节点。", {}, []),
    ("get_common_neighbors", "查询共同邻居", "查询 source 和 target 的共同邻居；direction 对两个端点使用相同方向，both 忽略方向。", {"source": _NODE, "target": _NODE, "direction": _DIRECTION}, ["source", "target"]),
    ("get_node_boundary", "查询节点边界", "返回 nodes 集合之外与该集合相邻的节点；out 为出边，in 为入边，both 为双向。", {"nodes": _NODES, "direction": _DIRECTION}, ["nodes"]),
    ("get_edge_boundary", "查询边边界", "返回 nodes 集合与外部之间的边，保留实际边方向与平行关系；out/in 相对于该集合。", {"nodes": _NODES, "direction": _DIRECTION}, ["nodes"]),
]
_THEORY = [
    ("triangle_count", "统计三角形", "计算各节点参与的三角形数和全图三角形总数。"),
    ("node_clustering", "计算节点聚类系数", "计算每个节点的无权局部聚类系数。"),
    ("square_clustering", "计算方形聚类系数", "计算各节点的四环聚类系数。"),
    ("core_numbers", "计算节点核数", "计算所有节点的 core number（最大所属 k-core 的 k）。"),
    ("onion_layers", "计算洋葱分解层", "计算无向图的洋葱分解层号。"),
    ("edge_betweenness", "计算边介数中心性", "计算无权、归一化的边介数中心性。"),
    ("hits_scores", "计算 HITS 分数", "计算无权拓扑上的 HITS hub 和 authority 分数；空边图拒绝计算。"),
    ("structural_constraint", "计算结构洞约束", "计算 Burt 结构洞约束；孤立节点等未定义值返回 null 和 defined=false。"),
    ("effective_size", "计算有效网络规模", "计算各节点的有效网络规模，按无权拓扑计算；未定义值显式标记。"),
    ("closeness_vitality", "计算接近活力", "计算删除节点前后全体节点对距离和之差；需要连通图，有向图需强连通。断开导致的负无穷显式编码。"),
    ("average_node_connectivity", "计算平均节点连通度", "计算所有节点对之间的平均节点连通度。"),
    ("cycle_basis", "计算基本环基", "计算无向图的一组基本环基；每个返回环的节点按环路顺序排列。"),
    ("minimum_cycle_basis", "计算最小环基", "计算无向图的无权最小环基；每个环返回节点集合，不承诺其排列为环路顺序。"),
    ("dominating_set", "构造支配集", "构造一个支配集；不保证最小。"),
    ("approx_min_dominating_set", "近似最小支配集", "以单位节点权重计算近似最小支配集；不保证最优。"),
    ("maximal_independent_set", "构造极大独立集", "使用固定随机种子构造可复现的极大独立集；极大不等于最大。"),
    ("approx_min_vertex_cover", "近似最小顶点覆盖", "以单位节点权重计算最小顶点覆盖的 2-近似解。"),
    ("is_eulerian", "判断欧拉图", "判断图是否存在经过每条边恰好一次的闭合欧拉回路。"),
    ("is_semieulerian", "判断半欧拉图", "判断图是否存在开放欧拉路径但不存在欧拉回路。"),
    ("has_eulerian_path", "判断欧拉路径", "判断图是否存在开放或闭合欧拉路径；可指定起点 source。"),
    ("eulerian_circuit", "查询欧拉回路", "返回欧拉回路的有序边序列；分页仅限制返回量，不代表完整回路。"),
    ("eulerian_path", "查询欧拉路径", "返回欧拉路径的有序边序列；可指定起点 source，分页仅限制返回量。"),
    ("bipartite_redundancy", "计算二部图冗余", "计算无向二部图节点冗余系数；度小于 2 的节点返回 null 和 defined=false。"),
    ("s_metric", "计算度相关 s 指标", "计算各边两端节点度数乘积之和（无权、未归一化 s-metric）。"),
]

EXTENDED_RETRIEVAL_NAMES = frozenset(row[0] for row in _RETRIEVAL)
EXTENDED_THEORY_NAMES = frozenset(row[0] for row in _THEORY)
EXTENDED_TOOL_NAMES = EXTENDED_RETRIEVAL_NAMES | EXTENDED_THEORY_NAMES
EXTENDED_TOOL_LABELS = {row[0]: row[1] for row in [*_RETRIEVAL, *_THEORY]}
UNDIRECTED_TOOLS = frozenset({
    "triangle_count", "square_clustering", "onion_layers", "cycle_basis",
    "minimum_cycle_basis", "dominating_set", "approx_min_dominating_set",
    "maximal_independent_set", "approx_min_vertex_cover", "bipartite_redundancy",
})
_SCALARS = {"is_eulerian", "is_semieulerian", "has_eulerian_path", "s_metric", "average_node_connectivity"}
_EXPENSIVE = {"minimum_cycle_basis", "average_node_connectivity", "closeness_vitality", "structural_constraint", "effective_size"}


def extended_tool_limits(name: str) -> tuple[int, int]:
    if name in EXTENDED_RETRIEVAL_NAMES:
        return 20000, 100000
    if name in _EXPENSIVE:
        return 200, 2000
    return 2000, 10000


def extended_size_error(name: str, node_count: int, edge_count: int) -> dict | None:
    nodes, edges = extended_tool_limits(name)
    if node_count <= nodes and edge_count <= edges:
        return None
    return {"success": False, "code": "EXTENDED_TOOL_GRAPH_TOO_LARGE", "tool": name,
            "num_nodes": node_count, "num_edges": edge_count,
            "max_nodes": nodes, "max_edges": edges,
            "error": "该工具仅支持有界内存图，尚无分区执行实现；请显式选择较小子图后调用。"}


def _spec(name, description, properties, required, category):
    nodes, edges = extended_tool_limits(name)
    semantics = "支持保留平行关系。" if category == "retrieval" else "基于无权拓扑，拒绝平行关系和自环；保留方向，仅在 treat_as_undirected=true 时显式转无向图。"
    if name in UNDIRECTED_TOOLS:
        semantics += "此算法要求无向图。"
    return {
        "name": name,
        "description": description + f"\n{semantics} 仅支持本地内存图，最多 {nodes} 节点/{edges} 边。列表返回 total、offset、next_offset 和 truncated。",
        "inputSchema": {"type": "object", "properties": deepcopy(properties),
                        "required": required, "additionalProperties": False},
        "category": "compute", "analysis_category": category,
        "multigraph_support": "native" if category == "retrieval" else "unsupported",
    }


EXTENDED_TOOLS = [
    _spec(name, description, {**_PAGE, **properties}, required, "retrieval")
    for name, _, description, properties, required in _RETRIEVAL
]
for _name, _, _description in _THEORY:
    _props = {} if _name in _SCALARS else dict(_PAGE)
    _props["treat_as_undirected"] = {"type": "boolean", "default": False}
    if _name in {"eulerian_path", "eulerian_circuit", "has_eulerian_path"}:
        _props["source"] = _NODE
    EXTENDED_TOOLS.append(_spec(_name, _description, _props, [], "theory"))

EXTENDED_SCHEMAS = {spec["name"]: spec["inputSchema"] for spec in EXTENDED_TOOLS}


def validate_extended_arguments(name: str, arguments: dict) -> dict:
    """Validate HTTP/direct calls as strictly as the advertised MCP schemas."""
    schema = EXTENDED_SCHEMAS[name]
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象。")
    extra = arguments.keys() - schema["properties"].keys()
    missing = set(schema["required"]) - arguments.keys()
    if extra or missing:
        raise ValueError(f"未知参数: {sorted(extra)}；缺少参数: {sorted(missing)}")
    result = {}
    for key, spec in schema["properties"].items():
        if key not in arguments:
            if "default" in spec:
                result[key] = spec["default"]
            continue
        value = arguments[key]
        kind = spec["type"]
        valid = (
            kind == "integer" and type(value) is int and spec.get("minimum", 0) <= value <= spec.get("maximum", 20000)
            or kind == "boolean" and type(value) is bool
            or kind == "string" and isinstance(value, str) and bool(value.strip()) and len(value) <= spec.get("maxLength", 1024)
            or kind == "array" and isinstance(value, list) and spec["minItems"] <= len(value) <= spec["maxItems"]
                and all(isinstance(n, str) and n.strip() and len(n) <= 1024 for n in value) and len(set(value)) == len(value)
        )
        if not valid or ("enum" in spec and value not in spec["enum"]):
            raise ValueError(f"参数 {key} 格式或取值无效。")
        result[key] = value
    return result

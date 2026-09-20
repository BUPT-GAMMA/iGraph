"""Deterministic analysis report builder for graph tool results."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

_REPORT_VERSION = "analysis_report.v2"
_VALID_INTENTS = {"overview", "analysis", "diagnosis", "comparison", "custom"}
_DEFAULT_GOAL = "总结当前图的结构与统计特征"


def build_analysis_report(
    report_intent: str,
    analysis_goal: str,
    title: str,
    source_results: dict,
    options: dict | None = None,
) -> dict:
    if not isinstance(source_results, dict) or not source_results:
        return {"success": False, "error": "source_results 不能为空，且必须是对象。"}

    normalized_options = _normalize_options(options)
    intent = str(report_intent or "").strip() or "analysis"
    if intent not in _VALID_INTENTS:
        intent = "analysis"
    goal = str(analysis_goal or "").strip() or _DEFAULT_GOAL
    report_title = str(title or "").strip() or "图分析报告"

    normalized_sources = _normalize_source_results(source_results)
    data_sources = _data_sources(normalized_sources)
    if not data_sources:
        return {"success": False, "error": "source_results 中没有可用于生成报告的分析事实。"}

    limitations: list[str] = []
    sections: list[dict] = []

    builders = [
        _build_overview_section,
        _build_connection_section,
        _build_connectivity_section,
        _build_global_metrics_section,
        _build_structure_sections,
        _build_supplemental_section,
    ]
    for builder in builders:
        built = builder(normalized_sources, normalized_options)
        if isinstance(built, list):
            sections.extend(section for section in built if section)
        elif built:
            sections.append(built)

    if not sections:
        limitations.append("当前可结构化展示的分析事实有限。")
        sections.append(_minimal_section(data_sources))

    report = {
        "version": _REPORT_VERSION,
        "id": _report_id(intent, goal, report_title, normalized_sources, normalized_options),
        "intent": intent,
        "title": report_title,
        "subtitle": "规模、连接、关键节点与结构特征",
        "summary": _build_summary(sections),
        "sections": sections,
        "data_sources": data_sources,
        "limitations": limitations,
    }
    return {"success": True, "report": _compact_report(report, normalized_options)}


def _normalize_options(options: dict | None) -> dict:
    raw = options if isinstance(options, dict) else {}
    return {
        "top_k": _bounded_int(raw.get("top_k"), default=10, min_value=1, max_value=20),
        "max_sections": _bounded_int(raw.get("max_sections"), default=6, min_value=1, max_value=8),
        "include_narrative": _bool_value(raw.get("include_narrative"), default=True),
    }


def _source(source_results: dict, *names: str) -> dict | list | None:
    for name in names:
        if name in source_results:
            return source_results[name]
    return None


def _metric(label: str, value: Any, unit: str | None = None) -> dict | None:
    if value is None:
        return None
    item = {"label": label, "value": _json_value(value)}
    if unit:
        item["unit"] = unit
    return item


def _bar_chart(
    title: str,
    rows: list[dict],
    label_key: str,
    value_key: str,
    unit: str | None = None,
    top_k: int = 10,
) -> dict | None:
    data: list[dict] = []
    for row in rows[:top_k]:
        if not isinstance(row, dict):
            continue
        label = row.get(label_key)
        value = _number(row.get(value_key))
        if label is None or value is None:
            continue
        data.append({"label": str(label), "value": value})
    if not data:
        return None
    chart = {"type": "bar", "title": title, "data": data}
    if unit:
        chart["unit"] = unit
    return chart


def _table(title: str, rows: list[dict], columns: list[dict], top_k: int = 10) -> dict | None:
    out_rows: list[dict] = []
    keys = [c.get("key") for c in columns if isinstance(c, dict) and c.get("key")]
    for row in rows[:top_k]:
        if not isinstance(row, dict):
            continue
        out_rows.append({key: _json_value(row.get(key)) for key in keys if key in row})
    if not out_rows:
        return None
    return {"title": title, "columns": columns, "rows": out_rows}


def _build_overview_section(source_results: dict, options: dict) -> dict | None:
    raw = _source(source_results, "get_graph_info", "graph_info")
    if not isinstance(raw, dict):
        return None
    info = raw.get("graph_info") if isinstance(raw.get("graph_info"), dict) else raw

    metrics = _compact_list([
        _metric("节点数", info.get("num_nodes")),
        _metric("边数", info.get("num_edges")),
        _metric("图方向", "有向" if info.get("directed") is True else "无向" if info.get("directed") is False else None),
        _metric("边权重", "有" if info.get("has_edge_weight") is True else "无" if info.get("has_edge_weight") is False else None),
        _metric("节点特征", "有" if info.get("has_node_feat") is True else "无" if info.get("has_node_feat") is False else None),
        _metric("节点特征维度", info.get("node_feat_dim") if info.get("has_node_feat") else None),
    ])
    label_stats = info.get("node_label_stats")
    if isinstance(label_stats, dict):
        metrics.extend(_compact_list([
            _metric("可见标签节点", label_stats.get("visible")),
            _metric("缺失标签节点", label_stats.get("missing")),
        ]))
    node_types = info.get("node_type_values") if isinstance(info.get("node_type_values"), list) else []
    edge_types = info.get("edge_type_values") if isinstance(info.get("edge_type_values"), list) else []
    metrics.extend(_compact_list([
        _metric("节点类型数", len(node_types) if node_types else None),
        _metric("边类型数", len(edge_types) if edge_types else None),
    ]))

    limitations = []
    if info.get("num_nodes") is None or info.get("num_edges") is None:
        limitations.append("结构概览缺少节点数或边数。")
    return _section(
        section_id="structure-overview",
        title="结构概览",
        summary=_overview_summary(info),
        metrics=metrics,
        limitations=limitations,
    )


def _result_summary(result: Any) -> dict | None:
    if not _successful_mapping(result):
        return None
    summary = result.get("summary")
    return summary if _successful_mapping(summary) else result


def _centrality_label(value: Any) -> str:
    name = str(value or "centrality").strip().lower()
    return {
        "pagerank": "PageRank",
        "degree": "度中心性",
        "betweenness": "中介中心性",
        "closeness": "接近中心性",
        "eigenvector": "特征向量中心性",
    }.get(name, str(value or "中心性"))


def _build_connection_section(source_results: dict, options: dict) -> dict | None:
    degree = _result_summary(_source(source_results, "degree_analysis"))
    centrality = _result_summary(_source(source_results, "node_centrality"))
    if degree is None and centrality is None:
        return None

    top_degree = degree.get("top_degree_nodes") if degree and isinstance(degree.get("top_degree_nodes"), list) else []
    top_centrality = centrality.get("top_nodes") if centrality and isinstance(centrality.get("top_nodes"), list) else []
    directed = bool(degree and (degree.get("directed") is True or degree.get("avg_in_degree") is not None or any(
        isinstance(row, dict) and any(key in row for key in ("total", "in", "out")) for row in top_degree
    )))
    stats: dict = {}
    if degree:
        candidate = degree.get("total_degree_stats") if directed else degree.get("degree_stats")
        if isinstance(candidate, dict):
            stats = candidate
    mean_degree = None
    if degree:
        mean_degree = degree.get("avg_degree")
        if mean_degree is None:
            mean_degree = degree.get("total_degree_mean")
        if mean_degree is None:
            mean_degree = stats.get("mean")
        if mean_degree is None and degree.get("avg_in_degree") is not None and degree.get("avg_out_degree") is not None:
            mean_degree = (_number(degree.get("avg_in_degree")) or 0) + (_number(degree.get("avg_out_degree")) or 0)
    metrics = _compact_list([
        _metric("平均总度" if directed else "平均度", mean_degree),
        _metric("中位总度" if directed else "中位度", stats.get("median")),
        _metric("最大总度" if directed else "最大度", stats.get("max")),
        _metric("平均入度", degree.get("avg_in_degree") if directed and degree else None),
        _metric("平均出度", degree.get("avg_out_degree") if directed and degree else None),
    ])

    centrality_name = _centrality_label(centrality.get("centrality_type") if centrality else None)
    centrality_by_node = {
        str(row.get("node")): row.get("value")
        for row in top_centrality if isinstance(row, dict) and row.get("node") is not None and _number(row.get("value")) is not None
    }
    chart = None
    if top_degree:
        chart_data = []
        value_key = "total" if directed else "degree"
        for row in top_degree[:options["top_k"]]:
            if not isinstance(row, dict) or row.get("node") is None:
                continue
            value = _number(row.get(value_key))
            if value is None:
                value = _number(row.get("total"))
            if value is None:
                value = _number(row.get("degree"))
            if value is None:
                continue
            item = {"label": str(row["node"]), "value": value}
            score = centrality_by_node.get(str(row["node"]))
            if score is not None:
                item["detail"] = f"{centrality_name} {_json_value(score)}"
            chart_data.append(item)
        if chart_data:
            chart = {
                "type": "bar",
                "title": "关键节点：连接规模与全局影响力" if centrality_by_node else "高连接节点",
                "data": chart_data,
                "unit": "总度" if directed else "度",
            }
    elif top_centrality:
        chart = _bar_chart(f"{centrality_name} 排名", top_centrality, "node", "value", unit=centrality_name, top_k=options["top_k"])

    if degree and centrality:
        summary = f"条形长度表示{'总度' if directed else '度'}，右侧同时给出 {centrality_name}。"
    elif degree:
        summary = f"用{'总度' if directed else '度'}对比节点的连接规模。"
    else:
        summary = f"按 {centrality_name} 对关键节点排序。"
    limitations = []
    if (degree and degree.get("truncated")) or (centrality and centrality.get("truncated")):
        limitations.append(f"仅展示排名前 {options['top_k']} 的节点。")
    return _section(
        section_id="connections-and-key-nodes",
        title="连接与关键节点",
        summary=summary,
        metrics=metrics,
        charts=_compact_list([chart]),
        limitations=limitations,
    )


def _build_connectivity_section(source_results: dict, options: dict) -> dict | None:
    result = _result_summary(_source(source_results, "graph_connectivity"))
    if result is None:
        return None
    directed = result.get("directed") is True or "is_weakly_connected" in result or "is_strongly_connected" in result
    strong = result.get("strongly_connected_components") if isinstance(result.get("strongly_connected_components"), dict) else {}
    weak = result.get("weakly_connected_components") if isinstance(result.get("weakly_connected_components"), dict) else {}
    connected = result.get("connected_components") if isinstance(result.get("connected_components"), dict) else {}
    metrics = _compact_list([
        _metric("弱连通", "是" if result.get("is_weakly_connected") is True else "否" if directed and result.get("is_weakly_connected") is False else None),
        _metric("强连通", "是" if result.get("is_strongly_connected") is True else "否" if directed and result.get("is_strongly_connected") is False else None),
        _metric("连通", "是" if result.get("is_connected") is True else "否" if not directed and result.get("is_connected") is False else None),
        _metric("强连通分量", strong.get("count")),
        _metric("最大强连通分量", strong.get("largest_size"), "节点"),
        _metric("弱连通分量", weak.get("count")),
        _metric("最大弱连通分量", weak.get("largest_size"), "节点"),
        _metric("连通分量", connected.get("count")),
        _metric("最大连通分量", connected.get("largest_size"), "节点"),
    ])
    component = (weak or strong) if directed else connected
    if component.get("count") is not None:
        summary = f"图包含 {component['count']} 个{'弱连通' if directed and weak else '强连通' if directed else '连通'}分量，最大分量有 {component.get('largest_size', 0)} 个节点。"
    else:
        summary = "图的连通状态和分量规模。"
    return _section(
        section_id="connectivity",
        title="连通性",
        summary=summary,
        metrics=metrics,
    )


def _build_global_metrics_section(source_results: dict, options: dict) -> dict | None:
    items = _global_metric_items(source_results)
    if not items:
        return None
    metrics = [_metric(_metric_label(item["metric"]), item["value"]) for item in items]
    return _section(
        section_id="global-metrics",
        title="整体结构指标",
        summary="图的稀疏程度、路径距离、聚集性与连接倾向。",
        metrics=_compact_list(metrics),
    )


def _build_structure_sections(source_results: dict, options: dict) -> list[dict]:
    sections: list[dict] = []
    result = _result_summary(_source(source_results, "community_detection"))
    if result:
        communities = result.get("communities") if isinstance(result.get("communities"), list) else []
        chart = _bar_chart("结构分组规模", communities, "community_id", "size", unit="节点", top_k=options["top_k"])
        metrics = _compact_list([
            _metric("社区数", result.get("num_communities")),
            _metric("模块度", result.get("modularity")),
        ])
        metrics.extend(_stats_metrics("规模", result.get("size_stats") if isinstance(result.get("size_stats"), dict) else {}))
        limitations = []
        if result.get("communities_truncated"):
            limitations.append("结构分组列表已按展示上限截断。")
        if any(isinstance(c, dict) and c.get("nodes_truncated") for c in communities):
            limitations.append("部分结构分组的节点预览已截断。")
        sections.append(_section(
            section_id="structure-patterns",
            title="社区结构",
            summary=f"识别到 {result.get('num_communities', '-')} 个社区，模块度为 {result.get('modularity', '-')}。",
            metrics=metrics,
            charts=_compact_list([chart]),
            limitations=limitations,
        ))
    return sections


def _build_supplemental_section(source_results: dict, options: dict) -> dict | None:
    known = {"get_graph_info", "graph_info", "degree_analysis", "node_centrality", "global_metrics", "community_detection", "graph_connectivity"}
    rows: list[dict] = []
    for source_name, result in source_results.items():
        if source_name in known or not _successful_mapping(result):
            continue
        for key, value in _scalar_items(result).items():
            rows.append({"field": key, "value": value})
            if len(rows) >= options["top_k"]:
                break
    table = _table(
        "补充事实",
        rows,
        [
            {"key": "field", "label": "指标"},
            {"key": "value", "label": "值"},
        ],
        top_k=options["top_k"],
    )
    if not table:
        return None
    return _section(
        section_id="supplemental-results",
        title="补充分析结果",
        summary="其他可展示的补充事实。",
        tables=[table],
    )


def _compact_report(report: dict, options: dict) -> dict:
    top_k = options["top_k"]
    max_sections = options["max_sections"]
    sections = report.get("sections") if isinstance(report.get("sections"), list) else []
    root_limitations = list(report.get("limitations") or [])
    if len(sections) > max_sections:
        root_limitations.append(f"报告 section 已限制为前 {max_sections} 个。")
        sections = sections[:max_sections]
    compacted_sections = []
    for section in sections:
        compacted = dict(section)
        compacted["metrics"] = (compacted.get("metrics") or [])[: min(8, top_k)]
        compacted["insights"] = (compacted.get("insights") or [])[:5]
        compacted["limitations"] = (compacted.get("limitations") or [])[:8]
        compacted["charts"] = _compact_charts(compacted.get("charts") or [], top_k)
        compacted["tables"] = _compact_tables(compacted.get("tables") or [], top_k)
        for limitation in compacted["limitations"]:
            if limitation not in root_limitations:
                root_limitations.append(limitation)
        compacted_sections.append(compacted)
    report["sections"] = compacted_sections
    report["limitations"] = root_limitations[:10]
    return report


def _normalize_source_results(source_results: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in source_results.items():
        if not isinstance(key, str) or not key.strip():
            continue
        out[key.strip()] = _coerce_json_value(value)
    return out


def _data_sources(source_results: dict) -> list[str]:
    names: list[str] = []
    for name, value in source_results.items():
        if isinstance(value, dict) and value.get("success") is False:
            continue
        if value is None:
            continue
        names.append(name)
    return names


def _section(
    *,
    section_id: str,
    title: str,
    summary: str,
    metrics: list[dict] | None = None,
    charts: list[dict] | None = None,
    tables: list[dict] | None = None,
    insights: list[str] | None = None,
    limitations: list[str] | None = None,
) -> dict:
    return {
        "id": section_id,
        "title": title,
        "summary": summary,
        "metrics": metrics or [],
        "charts": charts or [],
        "tables": tables or [],
        "insights": insights or [],
        "limitations": limitations or [],
    }


def _minimal_section(data_sources: list[str]) -> dict:
    return _section(
        section_id="available-results",
        title="可用分析结果",
        summary="当前可展示的结构化内容较少，建议补充图规模、连接分布或关键节点等分析结果。",
        tables=[{
            "title": "当前状态",
            "columns": [{"key": "item", "label": "内容"}, {"key": "value", "label": "说明"}],
            "rows": [{
                "item": "可展示事实",
                "value": f"已识别到 {len(data_sources)} 类分析结果，但缺少可直接图表化的统计字段。",
            }],
        }],
    )


def _build_summary(sections: list[dict]) -> str:
    by_id = {str(section.get("id")): section for section in sections}
    overview_metrics = {
        str(item.get("label")): item.get("value")
        for item in by_id.get("structure-overview", {}).get("metrics", []) if isinstance(item, dict)
    }
    parts = []
    nodes, edges = overview_metrics.get("节点数"), overview_metrics.get("边数")
    if isinstance(nodes, (int, float)) and isinstance(edges, (int, float)):
        parts.append(f"{nodes:,} 个节点 · {edges:,} 条边")
    connection = by_id.get("connections-and-key-nodes", {})
    connection_metrics = {
        str(item.get("label")): item.get("value")
        for item in connection.get("metrics", []) if isinstance(item, dict)
    }
    average_degree = connection_metrics.get("平均总度", connection_metrics.get("平均度"))
    if average_degree is not None:
        parts.append(f"平均度 {average_degree}")
    community = by_id.get("structure-patterns", {})
    community_metrics = {
        str(item.get("label")): item.get("value")
        for item in community.get("metrics", []) if isinstance(item, dict)
    }
    if community_metrics.get("社区数") is not None:
        parts.append(f"{community_metrics['社区数']} 个社区")
    if parts:
        return "；".join(parts) + "。"
    section_titles = "、".join(str(section.get("title")) for section in sections[:3] if section.get("title"))
    return f"报告包含{section_titles}。" if section_titles else "当前可展示的分析事实有限。"


def _overview_summary(info: dict) -> str:
    n = info.get("num_nodes")
    m = info.get("num_edges")
    directed = info.get("directed")
    if n is not None and m is not None:
        kind = "有向图" if directed is True else "无向图" if directed is False else "图"
        return f"当前{kind}包含 {n} 个节点和 {m} 条边。"
    return "当前图的基础结构信息摘要。"


def _stats_metrics(prefix: str, stats: dict) -> list[dict]:
    if not isinstance(stats, dict):
        return []
    return _compact_list([
        _metric(f"{prefix}均值", stats.get("mean")),
        _metric(f"{prefix}最大值", stats.get("max")),
        _metric(f"{prefix}最小值", stats.get("min")),
        _metric(f"{prefix}中位数", stats.get("median")),
    ])


def _global_metric_items(source_results: dict) -> list[dict]:
    result = _source(source_results, "global_metrics")
    items: list[dict] = []
    if _successful_mapping(result) and result.get("metric") is not None and result.get("value") is not None:
        items.append({"metric": str(result["metric"]), "value": _json_value(result["value"])})
        return items
    if isinstance(result, list):
        for item in result:
            if _successful_mapping(item) and item.get("metric") is not None and item.get("value") is not None:
                items.append({"metric": str(item["metric"]), "value": _json_value(item["value"])})
        return items
    if isinstance(result, dict):
        for key, value in result.items():
            if _successful_mapping(value) and value.get("metric") is not None and value.get("value") is not None:
                items.append({"metric": str(value["metric"]), "value": _json_value(value["value"])})
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                items.append({"metric": str(key), "value": _json_value(value)})
    return items


def _compact_charts(charts: list, top_k: int) -> list:
    out = []
    for chart in charts:
        if not isinstance(chart, dict):
            continue
        item = dict(chart)
        if isinstance(item.get("data"), list):
            item["data"] = item["data"][:top_k]
        out.append(item)
    return out[:4]


def _compact_tables(tables: list, top_k: int) -> list:
    out = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        item = dict(table)
        if isinstance(item.get("rows"), list):
            item["rows"] = item["rows"][:top_k]
        out.append(item)
    return out[:4]


def _scalar_items(result: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in result.items():
        if key in {"success", "error"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = _json_value(value)
    return out


def _metric_label(metric: str) -> str:
    labels = {
        "density": "密度",
        "diameter": "直径",
        "radius": "半径",
        "transitivity": "传递性",
        "average_clustering": "平均聚类系数",
        "degree_assortativity": "度同配性",
    }
    return labels.get(str(metric), str(metric))


def _successful_mapping(value: Any) -> bool:
    return isinstance(value, dict) and value.get("success") is not False


def _compact_list(values: list) -> list:
    return [value for value in values if value is not None]


def _bounded_int(value: Any, *, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _bool_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return round(value, 6)
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, float):
        if math.isfinite(value):
            return round(value, 6)
        return None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _coerce_json_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _report_id(intent: str, goal: str, title: str, source_results: dict, options: dict) -> str:
    payload = {
        "intent": intent,
        "goal": goal,
        "title": title,
        "source_results": source_results,
        "options": options,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"rpt_{digest}"

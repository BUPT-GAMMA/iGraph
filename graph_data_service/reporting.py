"""Deterministic analysis report builder for graph tool results."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

_REPORT_VERSION = "analysis_report.v1"
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
        return {"success": False, "error": "source_results 中没有可用于生成报告的工具结果。"}

    limitations: list[str] = []
    sections: list[dict] = []

    builders = [
        _build_overview_section,
        _build_degree_section,
        _build_centrality_section,
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
        limitations.append("当前工具结果可提取的结构化字段有限。")
        sections.append(_minimal_section(data_sources))

    report = {
        "version": _REPORT_VERSION,
        "id": _report_id(intent, goal, report_title, normalized_sources, normalized_options),
        "intent": intent,
        "title": report_title,
        "subtitle": "基于当前可用图分析结果生成",
        "summary": _build_summary(goal, sections, data_sources),
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
        insights=["基础规模和属性状态为后续分析提供上下文。"] if options["include_narrative"] else [],
        limitations=limitations,
    )


def _build_degree_section(source_results: dict, options: dict) -> dict | None:
    result = _source(source_results, "degree_analysis")
    if not _successful_mapping(result):
        return None

    directed = bool(result.get("directed"))
    stats = result.get("total_degree_stats") if directed else result.get("degree_stats")
    if not isinstance(stats, dict):
        stats = result.get("degree_stats") if isinstance(result.get("degree_stats"), dict) else {}
    top_nodes = result.get("top_degree_nodes") if isinstance(result.get("top_degree_nodes"), list) else []
    value_key = "total" if directed else "degree"
    metrics = _stats_metrics("度", stats)
    metrics.extend(_compact_list([
        _metric("平均入度", result.get("avg_in_degree") if directed else None),
        _metric("平均出度", result.get("avg_out_degree") if directed else None),
        _metric("平均度", result.get("avg_degree") if not directed else None),
    ]))
    chart = _bar_chart("Top 连接节点", top_nodes, "node", value_key, unit="连接", top_k=options["top_k"])
    table_columns = [
        {"key": "node", "label": "节点"},
        {"key": value_key, "label": "总度" if directed else "度"},
    ]
    if directed:
        table_columns.extend([{"key": "in", "label": "入度"}, {"key": "out", "label": "出度"}])
    table = _table("高度节点", top_nodes, table_columns, top_k=options["top_k"])

    limitations = []
    if result.get("truncated"):
        limitations.append("节点列表已按展示上限截断。")
    if not stats:
        limitations.append("连接分布缺少统计摘要。")
    return _section(
        section_id="degree-distribution",
        title="连接分布",
        summary="节点连接数量的统计分布和高连接节点摘要。",
        metrics=metrics,
        charts=_compact_list([chart]),
        tables=_compact_list([table]),
        insights=["高连接节点可作为进一步解释整体结构的入口。"] if options["include_narrative"] else [],
        limitations=limitations,
    )


def _build_centrality_section(source_results: dict, options: dict) -> dict | None:
    result = _source(source_results, "node_centrality")
    if not _successful_mapping(result):
        return None
    top_nodes = result.get("top_nodes") if isinstance(result.get("top_nodes"), list) else []
    chart = _bar_chart("Top 关键节点", top_nodes, "node", "value", unit="分数", top_k=options["top_k"])
    table = _table(
        "关键节点",
        top_nodes,
        [{"key": "node", "label": "节点"}, {"key": "value", "label": "分数"}],
        top_k=options["top_k"],
    )
    limitations = []
    if result.get("truncated"):
        limitations.append("关键节点列表已按展示上限截断。")
    return _section(
        section_id="centrality-ranking",
        title="关键对象",
        summary=f"基于 {result.get('centrality_type') or 'centrality'} 指标得到的关键节点排序。",
        metrics=_stats_metrics("中心性", result.get("value_stats") if isinstance(result.get("value_stats"), dict) else {}),
        charts=_compact_list([chart]),
        tables=_compact_list([table]),
        insights=["排序靠前的节点在当前指标下具有更高结构重要性。"] if options["include_narrative"] else [],
        limitations=limitations,
    )


def _build_global_metrics_section(source_results: dict, options: dict) -> dict | None:
    items = _global_metric_items(source_results)
    if not items:
        return None
    metrics = [_metric(_metric_label(item["metric"]), item["value"]) for item in items]
    rows = [{"metric": _metric_label(item["metric"]), "value": item["value"]} for item in items]
    table = _table(
        "整体指标",
        rows,
        [{"key": "metric", "label": "指标"}, {"key": "value", "label": "值"}],
        top_k=options["top_k"],
    )
    return _section(
        section_id="global-metrics",
        title="整体指标",
        summary="全局图指标提供整体结构强度、聚集或距离特征的量化依据。",
        metrics=_compact_list(metrics),
        tables=_compact_list([table]),
        insights=["整体指标适合与规模、分布和关键对象结果一起解释。"] if options["include_narrative"] else [],
    )


def _build_structure_sections(source_results: dict, options: dict) -> list[dict]:
    sections: list[dict] = []
    result = _source(source_results, "community_detection")
    if _successful_mapping(result):
        communities = result.get("communities") if isinstance(result.get("communities"), list) else []
        chart = _bar_chart("结构分组规模", communities, "community_id", "size", unit="节点", top_k=options["top_k"])
        metrics = _compact_list([
            _metric("分组数量", result.get("num_communities")),
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
            title="结构模式",
            summary="根据结构分组结果生成的模式摘要。",
            metrics=metrics,
            charts=_compact_list([chart]),
            insights=["结构分组结果可用于观察图中局部聚集和整体分化现象。"] if options["include_narrative"] else [],
            limitations=limitations,
        ))
    return sections


def _build_supplemental_section(source_results: dict, options: dict) -> dict | None:
    known = {"get_graph_info", "graph_info", "degree_analysis", "node_centrality", "global_metrics", "community_detection"}
    rows: list[dict] = []
    for source_name, result in source_results.items():
        if source_name in known or not _successful_mapping(result):
            continue
        for key, value in _scalar_items(result).items():
            rows.append({"source": source_name, "field": key, "value": value})
            if len(rows) >= options["top_k"]:
                break
    table = _table(
        "补充事实",
        rows,
        [
            {"key": "source", "label": "来源"},
            {"key": "field", "label": "字段"},
            {"key": "value", "label": "值"},
        ],
        top_k=options["top_k"],
    )
    if not table:
        return None
    return _section(
        section_id="supplemental-results",
        title="补充分析结果",
        summary="其他工具结果中的可展示标量事实。",
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
        summary="当前报告基于已提供的工具结果生成。",
        tables=[{
            "title": "数据来源",
            "columns": [{"key": "source", "label": "来源"}],
            "rows": [{"source": name} for name in data_sources],
        }],
    )


def _build_summary(goal: str, sections: list[dict], data_sources: list[str]) -> str:
    if sections:
        section_titles = "、".join(str(s.get("title")) for s in sections[:3] if s.get("title"))
        return f"围绕“{goal}”，报告基于 {len(data_sources)} 类工具结果生成，覆盖{section_titles}等内容。"
    return f"围绕“{goal}”，报告基于当前可用工具结果生成。"


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

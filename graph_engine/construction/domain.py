"""Transparent domain priors and anonymous, transferable strategy features.

This module performs no persistence and calls no language model. Domain priors
are hand-written hypotheses, never learned evidence. Ranking only proposes an
evaluation order: source fidelity and the independent query oracle remain the
authority for every candidate, regardless of historical outcomes.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter, defaultdict
from typing import Any


DOMAIN_KEYS = ("finance", "academic", "commerce", "healthcare", "social", "general")
STRATEGY_IDS = frozenset(("materialize_join", "collapse_node", "extract_property_node", "node_property_to_edge",
                          "connect_columns", "generate_entity_table", "unfold_multi_category", "add_primary_key",
                          "remove_primary_key", "reverse_edges", "rename_type", "delete"))
QUERY_FAMILIES = ("lookup", "filter", "join", "multi_hop", "aggregation", "group_by", "sort", "existence", "left_join", "reverse_join")
_LABELS = {"finance": "金融", "academic": "学术", "commerce": "商业与电商", "healthcare": "医疗健康", "social": "社交与社区", "general": "通用"}

# Only these fixed concept tokens can appear in profile.signals. Identifiers and
# domain-description substrings are never copied into the output.
_CONCEPTS = {
    "finance": {
        "finance_domain": (4, ("finance", "financial", "banking", "金融", "银行业", "证券")),
        "finance_account": (1, ("account", "accounts", "balance", "balances", "账户", "余额")),
        "finance_transaction": (2, ("transaction", "transactions", "ledger", "ledgers", "transfer", "transfers", "交易", "账本", "转账")),
        "finance_market": (2, ("portfolio", "portfolios", "security", "securities", "bond", "bonds", "银行", "投资组合", "债券")),
    },
    "academic": {
        "academic_domain": (4, ("academic", "research", "scholarly", "学术", "科研", "论文")),
        "academic_publication": (2, ("paper", "papers", "publication", "publications", "journal", "journals", "文献", "期刊")),
        "academic_author": (1, ("author", "authors", "affiliation", "affiliations", "作者", "研究机构")),
        "academic_citation": (2, ("citation", "citations", "doi", "conference", "conferences", "引文", "引用关系")),
    },
    "commerce": {
        "commerce_domain": (4, ("commerce", "ecommerce", "retail", "shopping", "电商", "电子商务", "零售")),
        "commerce_order": (2, ("order", "orders", "purchase", "purchases", "invoice", "invoices", "订单", "采购", "发票")),
        "commerce_product": (2, ("product", "products", "inventory", "cart", "carts", "shipment", "shipments", "商品", "库存", "购物车", "发货")),
        "commerce_customer": (1, ("customer", "customers", "merchant", "merchants", "客户", "商家")),
    },
    "healthcare": {
        "healthcare_domain": (4, ("healthcare", "medical", "clinical", "hospital", "hospitals", "医疗", "临床", "医院")),
        "healthcare_patient": (2, ("patient", "patients", "患者", "病人")),
        "healthcare_treatment": (2, ("diagnosis", "diagnoses", "prescription", "prescriptions", "medication", "medications", "诊断", "处方", "用药")),
        "healthcare_encounter": (1, ("encounter", "encounters", "clinician", "clinicians", "就诊", "医师")),
    },
    "social": {
        "social_domain": (4, ("social", "forum", "forums", "stackexchange", "reddit", "社交", "社区", "论坛")),
        "social_content": (2, ("post", "posts", "comment", "comments", "帖子", "评论")),
        "social_interaction": (2, ("follow", "follows", "follower", "followers", "friend", "friends", "badge", "badges", "vote", "votes", "关注", "好友", "点赞")),
        "social_user": (1, ("user", "users", "用户")),
    },
}

_GENERIC_PRIORS = (
    ("materialize_join", "对实际工作负载中的连续多跳外键路径试验物化；保留中间实体、路径多重性和原始关系，收益须重新测量。"),
    ("collapse_node", "桥接行可试验表示为当前边实体，但必须保留全部行值和两个外键角色；原记录不可丢失。"),
    ("extract_property_node", "重复业务属性可试验共享实体；避免把高基数标识符当作类别，并验证提取后的全部查询。"),
    ("connect_columns", "关系恢复和派生连接必须尊重声明键及原始列比较语义，不能按历史经验猜测关联。"),
)
_DOMAIN_PRIORS = {
    "finance": (
        ("materialize_join", "可试验交易—账户多跳路径物化；账户角色、资金方向、金额、币种和发生时间必须完整保留。"),
        ("collapse_node", "交易行只有在边实体仍保留金额、时间、币种及付款/收款角色时才可试验折叠，不能合并重复交易。"),
        ("node_property_to_edge", "交易关系上的金额或时间可试验迁移到当前关联边；必须保持每笔交易的归属及原值。"),
    ),
    "academic": (
        ("materialize_join", "可试验论文—作者—机构或引文路径物化；保留作者角色、署名顺序、引用方向与原始论文身份。"),
        ("generate_entity_table", "作者、机构和主题可作为共享实体候选；同名不等于同一实体，必须以可靠键约束连接。"),
        ("unfold_multi_category", "可试验把多主题字段展开为主题实体；保留原字段、类别位置和重复性，验证论文检索与聚合。"),
        ("extract_property_node", "重复主题或机构属性可试验实体化；不应仅依据文本相同合并具有不同标识的作者。"),
    ),
    "commerce": (
        ("materialize_join", "可试验订单—客户—商品或库存路径物化；订单行数量、价格、折扣、履约状态和时间须保持准确。"),
        ("collapse_node", "订单明细等桥接行可试验边实体表示；每行数量、金额和商品角色必须保留，不可聚合掉重复购买。"),
        ("extract_property_node", "重复的商品类别或履约状态可试验共享实体；以当前筛选和分组工作负载衡量实际收益。"),
    ),
    "healthcare": (
        ("materialize_join", "可试验患者—就诊—诊疗路径物化；保留患者身份、就诊时间、医护角色与每次诊疗事件。"),
        ("generate_entity_table", "具有明确编码的诊断或药物可试验共享实体；相似名称不能替代标准编码或临床事件身份。"),
        ("extract_property_node", "重复诊疗类别可试验实体化，但必须保留缺失值和患者事件的准确关联，不能把优化当成临床结论。"),
    ),
    "social": (
        ("materialize_join", "可试验用户—内容—互动路径物化；作者、互动者与引用对象的角色、方向和事件时间必须保留。"),
        ("collapse_node", "互动桥接行可试验边实体表示；保留重复互动、时间和双方角色，不能把多次行为合并为一次。"),
        ("unfold_multi_category", "可试验把多标签字段展开为共享标签实体，保留原标签顺序及原始内容记录。"),
    ),
    "general": (),
}


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _contains(text: str, alias: str) -> bool:
    if re.search(r"[\u3400-\u9fff]", alias):
        return alias in text
    return re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", text) is not None


def _normalized(text: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ").lower()


def _priors(key: str) -> list[dict]:
    seen, result = set(), []
    for strategy, note in (*_DOMAIN_PRIORS[key], *_GENERIC_PRIORS):
        if strategy not in seen:
            result.append({"strategy_id": strategy, "note": note, "evidence": "unvalidated_prior"})
            seen.add(strategy)
    return result


def domain_profile(source: Any, category: str = "auto") -> dict:
    """Classify only the supplied description and schema names, never cell data.

    Auto classification requires at least three weighted concept points and a
    clear lead. Description matches carry twice the schema weight. Competing
    domains within 1.75x of the leader are treated as mixed/general. Confidence
    is a transparent heuristic strength indicator, not a learned probability.
    """
    if not isinstance(category, str) or category not in ("auto", *DOMAIN_KEYS):
        raise ValueError("category must be auto, finance, academic, commerce, healthcare, social or general")
    if category != "auto":
        return {"key": category, "label": _LABELS[category], "origin": "explicit", "confidence": 1.0,
                "signals": [], "prior_strategies": _priors(category)}
    description = _normalized(str(_get(source, "domain", ""))[:16000])
    names = []
    for name, table in source.tables.items():
        names.append(str(name))
        names.extend(str(column) for column in _get(table, "columns", []))
    schema_text = _normalized(" ".join(names))
    scores, signals = {}, []
    for key, concepts in _CONCEPTS.items():
        score = 0
        for signal, (weight, aliases) in concepts.items():
            in_description = any(_contains(description, alias) for alias in aliases)
            in_schema = any(_contains(schema_text, alias) for alias in aliases)
            if in_description or in_schema:
                signals.append(signal)
                score += weight * (2 if in_description else 1)
        scores[key] = score
    ranked = sorted(scores, key=lambda key: (-scores[key], key))
    winner, runner = ranked[:2]
    strong = scores[winner] >= 3
    mixed = scores[runner] >= 3 and scores[winner] < 1.75 * scores[runner]
    key = winner if strong and not mixed else "general"
    confidence = (round(min(0.95, 0.5 + 0.025 * scores[winner] + 0.02 * (scores[winner] - scores[runner])), 3)
                  if key != "general" else 0.25 if signals else 0.0)
    return {"key": key, "label": _LABELS[key], "origin": "heuristic" if key != "general" else "fallback",
            "confidence": confidence, "signals": sorted(signals), "prior_strategies": _priors(key)}


def _families(query: dict) -> set[str]:
    result = set()
    joins, filters = query.get("joins", []), query.get("filters", [])
    if filters:
        result.add("filter")
        if any(f.get("op") in ("eq", "in") for f in filters):
            result.add("lookup")
        if any(f.get("op") in ("is_null", "not_null") for f in filters):
            result.add("existence")
    if joins:
        result.add("join")
        if len(joins) >= 2:
            result.add("multi_hop")
        if any(j.get("kind") == "left" for j in joins):
            result.add("left_join")
        if any(j.get("direction") == "reverse" for j in joins):
            result.add("reverse_join")
    for field, family in (("aggregations", "aggregation"), ("group_by", "group_by"), ("order_by", "sort")):
        if query.get(field):
            result.add(family)
    return result


def workload_context(source: Any, queries: list[dict]) -> dict:
    """Return only fixed structural counters, booleans and query-family enums."""
    tables = list(source.tables.values())
    sql_tables = sum(bool(_get(table, "create_sql", "")) for table in tables)
    mix = {family: 0 for family in QUERY_FAMILIES}
    for query in queries:
        for family in _families(query):
            mix[family] += 1
    nullable = any(any(row.get(column) is None or (not _get(table, "create_sql", "") and row.get(column) == "")
                       for column in _get(fk, "columns", []))
                   for table in tables for fk in _get(table, "foreign_keys", []) for row in _get(table, "rows", []))
    return {"source_kind": "sqlite" if tables and sql_tables == len(tables) else "mixed" if sql_tables else "csv",
            "table_count": len(tables), "row_count": sum(len(_get(t, "rows", [])) for t in tables),
            "column_count": sum(len(_get(t, "columns", [])) for t in tables),
            "fk_count": sum(len(_get(t, "foreign_keys", [])) for t in tables), "query_count": len(queries),
            "max_join_hops": max((len(q.get("joins", [])) for q in queries), default=0),
            "has_composite_keys": any(len(_get(t, "primary_key", [])) > 1 or any(len(_get(f, "columns", [])) > 1 for f in _get(t, "foreign_keys", [])) for t in tables),
            "has_nullable_fks": nullable, "query_families": [f for f in QUERY_FAMILIES if mix[f]], "query_mix": mix}


def _rows_bucket(count: int) -> str:
    return "empty" if count == 0 else "tiny" if count <= 100 else "small" if count <= 10000 else "medium" if count <= 100000 else "large"


def _fanout_bucket(count: int) -> str:
    return "one" if count <= 1 else "few" if count <= 10 else "many"


def _fk_key(fk: Any, table: str | None = None) -> tuple:
    return (_get(fk, "table", table), tuple(_get(fk, "columns", [])), _get(fk, "ref_table"), tuple(_get(fk, "ref_columns", [])))


def _property_features(source: Any, references: set[tuple[str, str]]) -> dict:
    kinds, ratios = [], []
    for table, column in sorted(references):
        spec = source.tables[table]
        keys = set(_get(spec, "primary_key", [])) | {c for fk in _get(spec, "foreign_keys", []) for c in _get(fk, "columns", [])}
        values = [r.get(column) for r in _get(spec, "rows", []) if r.get(column) is not None]
        unique = len({json.dumps(v, sort_keys=True, ensure_ascii=False, allow_nan=False) for v in values})
        ratio = 1 - unique / len(values) if values else 0.0
        ratios.append(ratio)
        if column in keys or column.lower() in ("id", "uuid", "guid", "identifier") or column.lower().endswith("_id") or re.search(r"(?:[a-z]Id|ID)$", column):
            kinds.append("identifier")
        elif values and all(isinstance(v, bool) for v in values):
            kinds.append("categorical")
        elif values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            kinds.append("numeric")
        elif values and all(isinstance(v, str) for v in values):
            kinds.append("categorical" if unique <= 20 and ratio > 0 else "text")
        else:
            kinds.append("mixed")
    return {"property_kind": kinds[0] if kinds and len(set(kinds)) == 1 else "mixed",
            "repeated_value_ratio": round(sum(ratios) / len(ratios), 3) if ratios else 0.0}


def _strategy_signature(tool: str, args: dict, source: Any, graph: dict, queries: list[dict]) -> dict | None:
    if not isinstance(tool, str) or not isinstance(args, dict):
        return None
    operations = args.get("operations") if tool == "optimize_graph" else None
    if tool == "optimize_graph":
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100 or not all(isinstance(op, dict) for op in operations):
            return None
        kinds = {op.get("op") for op in operations}
        if len(kinds) != 1:
            return None  # Do not assign one operation credit for a mixed batch.
        strategy = next(iter(kinds))
    else:
        strategy, operations = tool, [args]
    if strategy not in STRATEGY_IDS:
        return None
    by_id = {node["id"]: node for node in graph.get("nodes", [])}
    edges = {edge["id"]: edge for edge in graph.get("edges", [])}
    affected, references, selected_nodes, selected_edges = set(), set(), set(), set()
    pattern = {}

    def add_table(table: str) -> None:
        if table not in source.tables:
            raise ValueError("Unknown current source table")
        affected.add(table)

    def add_node(identity: str) -> None:
        node = by_id[identity]
        selected_nodes.add(identity)
        table = node.get("properties", {}).get("table")
        if table in source.tables:
            affected.add(table)

    def add_edge(identity: str) -> None:
        edge = edges[identity]
        selected_edges.add(identity)
        add_node(edge["source"])
        add_node(edge["target"])

    def add_property(table: str, column: str) -> None:
        add_table(table)
        if not isinstance(column, str) or column not in _get(source.tables[table], "columns", []):
            raise ValueError("Unknown source property")
        references.add((table, column))

    if strategy == "materialize_join":
        path = args.get("path")
        if not isinstance(path, list) or not 2 <= len(path) <= 3:
            return None
        declared = {_fk_key(fk, table) for table, spec in source.tables.items() for fk in _get(spec, "foreign_keys", [])}
        previous, maximum_fanout = None, 1
        for step in path:
            key = _fk_key(step)
            if key not in declared or step.get("direction") not in ("forward", "reverse"):
                return None
            reverse = step["direction"] == "reverse"
            start, end = (step["ref_table"], step["table"]) if reverse else (step["table"], step["ref_table"])
            if previous is not None and start != previous:
                return None
            previous = end
            add_table(start)
            add_table(end)
            counts = Counter()
            for edge in edges.values():
                fk = edge.get("properties", {}).get("foreign_key")
                if fk is not None and _fk_key(fk) == key:
                    counts[edge["target"] if reverse else edge["source"]] += 1
            maximum_fanout = max(maximum_fanout, max(counts.values(), default=0))
        pattern.update(join_hops=len(path), fk_arity=max(len(step["columns"]) for step in path), relationship_shape="chain",
                       fanout_bucket=_fanout_bucket(maximum_fanout),
                       query_family="reverse_join" if any(step["direction"] == "reverse" for step in path) else "multi_hop")
    elif strategy in ("generate_entity_table", "unfold_multi_category"):
        add_property(args["table"], args["column"])
        pattern["relationship_shape"] = "attribute"
    elif strategy in ("connect_columns", "add_primary_key", "remove_primary_key"):
        add_table(args["table"])
        if strategy == "connect_columns":
            add_table(args["ref_table"])
            if not args.get("columns") or len(args["columns"]) != len(args.get("ref_columns", [])):
                return None
            for table, cols in ((args["table"], args["columns"]), (args["ref_table"], args["ref_columns"])):
                for column in cols:
                    if column not in _get(source.tables[table], "columns", []):
                        return None
            pattern.update(join_hops=1, fk_arity=len(args["columns"]), relationship_shape="single_fk", query_family="join")
        else:
            columns = args.get("columns", _get(source.tables[args["table"]], "primary_key", []))
            if not isinstance(columns, list) or any(c not in _get(source.tables[args["table"]], "columns", []) for c in columns):
                return None
            pattern.update(property_kind="identifier", fk_arity=len(columns))
    else:
        for op in operations:
            if strategy == "collapse_node":
                add_node(op["node_id"])
                incident = [edge for edge in edges.values() if op["node_id"] in (edge["source"], edge["target"])]
                for edge in incident:
                    add_edge(edge["id"])
                pattern.update(join_hops=2, relationship_shape="bridge" if len(incident) == 2 else "star",
                               fanout_bucket=_fanout_bucket(len(incident)))
            elif strategy in ("extract_property_node", "node_property_to_edge"):
                identities = op["node_ids"] if strategy == "extract_property_node" else [op["node_id"]]
                if not isinstance(identities, list) or not identities:
                    return None
                for identity in identities:
                    add_node(identity)
                    add_property(by_id[identity]["properties"]["table"], op["property"])
                if strategy == "node_property_to_edge":
                    for identity in op["edge_ids"]:
                        add_edge(identity)
                pattern["relationship_shape"] = "attribute"
            elif strategy in ("reverse_edges", "delete"):
                identities = op["edge_ids"] if strategy == "reverse_edges" else op["ids"]
                if not isinstance(identities, list) or not identities:
                    return None
                for identity in identities:
                    (add_edge if strategy == "reverse_edges" or op.get("target") == "edge" else add_node)(identity)
                pattern["relationship_shape"] = "single_fk"
                if strategy == "reverse_edges":
                    pattern["query_family"] = "reverse_join"
            elif strategy == "rename_type":
                selected = [item for item in (by_id if op.get("target") == "node" else edges).values() if item.get("type") == op["old_type"]]
                if not selected:
                    return None
                for item in selected:
                    (add_node if op.get("target") == "node" else add_edge)(item["id"])
            else:
                return None
    if not affected:
        return None
    pattern["affected_table_count"] = len(affected)
    pattern["row_count_bucket"] = _rows_bucket(sum(len(_get(source.tables[t], "rows", [])) for t in affected))
    if references:
        pattern.update(_property_features(source, references))
        for query in queries:
            aliases = {query["root"]["alias"]: query["root"]["table"], **{j["alias"]: j["table"] for j in query.get("joins", [])}}
            grouped = {tuple(column.split(".", 1)) for column in query.get("group_by", [])}
            filtered = {tuple(f["column"].split(".", 1)) for f in query.get("filters", [])}
            if any((aliases.get(alias), column) in references for alias, column in grouped):
                pattern["query_family"] = "group_by"
                break
            if any((aliases.get(alias), column) in references for alias, column in filtered):
                pattern["query_family"] = "filter"
    if "fk_arity" not in pattern and selected_edges:
        arities = [len(_get(edges[e].get("properties", {}).get("foreign_key", {}), "columns", [])) for e in selected_edges]
        if any(arities):
            pattern["fk_arity"] = max(arities)
    return {"strategy_id": strategy, "pattern": pattern}


def strategy_signature(tool: str, args: dict, source: Any, graph: dict, queries: list[dict]) -> dict | None:
    """Abstract a current candidate without retaining any name, ID or cell value.

    Unknown/malformed candidates and heterogeneous operation batches return None
    rather than inventing a transferable strategy or failing the main build.
    """
    try:
        return _strategy_signature(tool, args, source, graph, queries)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _finite(value: Any, minimum: float, maximum: float, default: float = 0.0) -> float:
    return max(minimum, min(maximum, float(value))) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def _matching_evidence(records: list[dict], signature: dict) -> list[dict]:
    result = []
    for record in records:
        if not isinstance(record, dict) or record.get("strategy_id") != signature["strategy_id"] or not isinstance(record.get("pattern"), dict):
            continue
        pattern = record["pattern"]
        if any(field in pattern and field in signature["pattern"] and pattern[field] != signature["pattern"][field]
               for field in ("join_hops", "fk_arity", "relationship_shape", "property_kind", "query_family")):
            continue
        status = record.get("confidence_status")
        if status not in ("candidate", "supported", "cautioned"):
            continue
        counts = {key: int(_finite(record.get(key), 0, 1_000_000))
                  for key in ("independent_sources", "trials", "accepted", "no_gain", "unsafe")}
        if not counts["independent_sources"] or not counts["trials"]:
            continue
        # A forged status cannot upgrade a single-source anecdote to support.
        if status == "supported" and (counts["independent_sources"] < 3 or not counts["accepted"]):
            status = "candidate"
        if counts["unsafe"]:
            status = "cautioned"
        result.append({**counts, "confidence_status": status,
                       "mean_gain": _finite(record.get("mean_gain"), -1, 1),
                       "policy_score": _finite(record.get("policy_score"), -1, 1),
                       "context_similarity": _finite(record.get("context_similarity"), 0, 1)})
    return result


def rank_candidates(examples: list[dict], profile: dict, learned: list[dict], source: Any,
                    graph: dict, queries: list[dict]) -> list[dict]:
    """Annotate and stably order current proposals; never approve or execute one.

    Current arguments remain available to the LLM. Learned annotations contain
    only bounded counters/enums and measured gains; arbitrary historical notes,
    source identifiers and arguments are not copied. Correctness failures lower
    priority regardless of a positive historical speed measurement.
    """
    key = profile.get("key", "general") if isinstance(profile, dict) else "general"
    if key not in DOMAIN_KEYS:
        key = "general"
    priors = {p["strategy_id"]: p for p in _priors(key)}
    confidence = _finite(profile.get("confidence", 0) if isinstance(profile, dict) else 0, 0, 1)
    ranked = []
    for original in examples:
        example = copy.deepcopy(original)
        signature = strategy_signature(example.get("tool"), example.get("arguments", {}), source, graph, queries)
        prior = priors.get(signature["strategy_id"]) if signature else None
        evidence = _matching_evidence(learned if isinstance(learned, list) else [], signature) if signature else []
        priority = (0.1 + 0.2 * confidence) if prior else 0.0
        if signature:
            if signature["strategy_id"] == "materialize_join" and any(len(q.get("joins", [])) >= 2 for q in queries):
                priority += 0.1
            if signature["pattern"].get("property_kind") == "identifier" and signature["strategy_id"] in ("extract_property_node", "generate_entity_table"):
                priority -= 0.2
        if evidence:
            # Do not sum overlapping retrieved histories and manufacture extra
            # confidence. One most-contextual summary supplies the ranking hint.
            chosen = max(evidence, key=lambda e: (e["context_similarity"], e["independent_sources"], e["trials"]))
            weight = 1.0 if chosen["confidence_status"] == "supported" else 0.35
            priority += weight * chosen["context_similarity"] * chosen["policy_score"]
            if any(e["unsafe"] for e in evidence):
                priority -= 0.75
        example.update(strategy_id=signature["strategy_id"] if signature else None,
                       pattern=signature["pattern"] if signature else {},
                       prior_note=prior["note"] if prior else None,
                       prior_evidence="unvalidated_prior" if prior else None,
                       learned_evidence=evidence, priority=round(priority, 4))
        ranked.append(example)
    return sorted(ranked, key=lambda example: -example["priority"])


__all__ = ["DOMAIN_KEYS", "STRATEGY_IDS", "QUERY_FAMILIES", "domain_profile", "strategy_signature", "workload_context", "rank_candidates"]

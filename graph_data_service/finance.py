"""Deterministic ownership analysis over original construction records.

No generated shortcut, inferred name match, or LLM output is an ownership fact.
All ratios cross the public boundary as decimal strings, with scientific
notation for extreme exponents. ``complete`` describes
the selected computation, independently of rule coverage and display limits.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, Overflow, Underflow, localcontext
import hashlib
import heapq
import json
import time
from typing import Any

VERSION = "finance-1.0"
TABLES = ("institution_main", "individual_info", "institution_shareholder_relationship")
ZERO, ONE, HALF = Decimal(0), Decimal(1), Decimal("0.5")


class BudgetExceeded(Exception):
    pass


class FinanceInputError(ValueError):
    pass


@dataclass
class Budget:
    max_nodes: int
    max_edges: int
    max_seconds: float
    started: float = field(default_factory=time.monotonic)
    checks: int = 0

    def check(self, nodes: int = 0, edges: int = 0) -> None:
        self.checks += 1
        if nodes > self.max_nodes or edges > self.max_edges or time.monotonic() - self.started > self.max_seconds:
            raise BudgetExceeded("node, edge or elapsed-time budget exhausted")


@dataclass
class Entity:
    key: str
    business_id: str
    kind: str
    name: str
    status: str
    source_ids: list[str]


@dataclass
class Fact:
    key: str
    holder: str
    target: str
    ratio: Decimal
    source_ids: list[str]


@dataclass
class Projection:
    entities: dict[str, Entity] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    issue_counts: Counter = field(default_factory=Counter)
    blocked: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    counts: Counter = field(default_factory=Counter)
    complete: bool = True
    invalid_snapshot: bool = False
    digest: str = ""
    semantic_digest: str = ""
    source_edge_ids: set[str] = field(default_factory=set)

    def issue(self, code: str, entity: str | None = None, source_id: str | None = None,
              detail: str = "", blocking: bool = False) -> None:
        self.issue_counts[code] += 1
        if len(self.issues) < 500:
            self.issues.append({"code": code, "entity_id": entity, "source_node_id": source_id, "detail": detail[:250]})
        if blocking and entity:
            self.blocked.add(entity)


def _s(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _id(value: Any) -> str:
    text = _s(value)
    if not text or len(text) > 256:
        raise FinanceInputError("business IDs must contain 1 to 256 characters")
    return text


def _selector(value: Any) -> str:
    if not isinstance(value, str):
        raise FinanceInputError("entity selectors must be strings")
    text = value.strip()
    _id(text[2:] if text.startswith(("I:", "P:")) else text)
    return text


def _ratio(raw: Any, fmt: str) -> Decimal:
    text = _s(raw)
    if not text or len(text) > 128:
        raise FinanceInputError("missing or oversized ownership ratio")
    try:
        if fmt == "percent_string":
            if not text.endswith("%"):
                raise FinanceInputError("ratio requires an explicit percent sign")
            parsed, shift = Decimal(text[:-1]), 2
        elif fmt == "percent_number":
            parsed, shift = Decimal(text), 2
        elif fmt == "fraction":
            parsed, shift = Decimal(text), 0
        else:
            raise FinanceInputError("unsupported ratio_format")
    except InvalidOperation as exc:
        raise FinanceInputError("invalid ownership ratio") from exc
    if not parsed.is_finite():
        raise FinanceInputError("ownership ratio must be finite")
    # Validate before arithmetic: Decimal construction is exact, while /100 or
    # normalize() would silently round an over-precision input in this context.
    sign, digits, exponent = parsed.as_tuple()
    significant = list(digits)
    while significant and significant[-1] == 0:
        significant.pop()
        exponent += 1
    if not significant:
        return ZERO
    exponent -= shift
    if len(significant) > 50 or not -100 <= exponent <= 100:
        raise FinanceInputError("ratio exceeds 50 significant digits or normalized exponent range [-100,100]")
    value = Decimal((sign, tuple(significant), exponent))
    if not value.is_finite() or value < ZERO or value > ONE:
        raise FinanceInputError("ownership ratio must be in [0,1]")
    return value


def _decimal(value: Decimal) -> str:
    # Keep ordinary percentages readable without expanding tiny cumulative
    # values into thousands of leading zeroes. Both forms parse as Decimal.
    return format(value, "E" if abs(value.as_tuple().exponent) > 100 else "f")


def _source_evidence(p: Projection, source_ids: list[str]) -> dict:
    shown = source_ids[:8]
    result = {"source_node_ids": [key for key in shown if key not in p.source_edge_ids],
              "source_count": len(source_ids)}
    if any(key in p.source_edge_ids for key in shown):
        result["source_edge_ids"] = [key for key in shown if key in p.source_edge_ids]
    return result


def _entity(p: Projection, key: str) -> dict:
    e = p.entities[key]
    return {"entity_id": key, "business_id": e.business_id, "entity_type": e.kind,
            "name": e.name, "operating_status": e.status,
            **_source_evidence(p, e.source_ids)}


def _fact(p: Projection, f: Fact) -> dict:
    return {"fact_id": f.key, "holder_id": p.entities[f.holder].business_id,
            "holder_entity_id": f.holder, "target_id": p.entities[f.target].business_id,
            "target_entity_id": f.target, "ratio": _decimal(f.ratio),
            **_source_evidence(p, f.source_ids)}


def _resolve(p: Projection, value: Any) -> str:
    value = _selector(value)
    if value.startswith(("I:", "P:")):
        _id(value[2:])
        if value in p.entities:
            return value
        if len(value) > 256:
            raise FinanceInputError("NOT_FOUND")
    else:
        _id(value)
    if value in p.entities:
        return value
    matches = [k for k, e in p.entities.items() if e.business_id == value or value in e.source_ids]
    if len(matches) != 1:
        raise FinanceInputError("AMBIGUOUS_ID" if matches else "NOT_FOUND")
    return matches[0]


def _current_records(graph: dict, budget: Budget) -> list[dict]:
    """Resolve live source cells after lossless construction edits.

    Legacy row documents lack table/row_index metadata. Keep those nodes, but
    never reconstruct rows from historical collapse snapshots or shortcut edges.
    """
    from graph_engine.construction.budget import ConstructionBudgetExceeded, deadline_scope
    from graph_engine.construction.representation import resolve_rows

    legacy, has_current_rows = [], False
    for is_edge, elements in ((False, graph.get("nodes", [])), (True, graph.get("edges", []))):
        for index, element in enumerate(elements):
            if index % 128 == 0:
                budget.check()
            if not isinstance(element, dict):
                raise FinanceInputError("construction graph elements must be objects")
            props = element.get("properties") or {}
            if not isinstance(props, dict):
                raise FinanceInputError("construction element properties must be an object")
            if "table" in props or "row_index" in props:
                has_current_rows = True
            elif not is_edge:
                legacy.append(element)
    if not has_current_rows:
        return legacy
    try:
        with deadline_scope(budget.started + budget.max_seconds):
            current = resolve_rows({"nodes": graph.get("nodes", []), "edges": graph.get("edges", [])})
    except ConstructionBudgetExceeded as exc:
        raise BudgetExceeded("source-row resolution exceeded the elapsed-time budget") from exc
    return [*current, *legacy]


def project_ownership(construction_graph: dict, policy: dict, budget: Budget) -> Projection:
    """Keep original table rows, typed identities and deduplicated fact provenance."""
    p = Projection()
    graph = construction_graph.get("graph", construction_graph)
    if not isinstance(graph, dict):
        raise FinanceInputError("construction graph must be an object")
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise FinanceInputError("construction graph nodes/edges must be lists")
    budget.check(len(nodes), len(edges))
    records = []
    hasher = hashlib.sha256()
    current_records = _current_records(graph, budget)
    p.source_edge_ids = {row["id"] for row in current_records if "source" in row and "target" in row}
    for i, node in enumerate(current_records):
        if i % 128 == 0:
            budget.check()
        if not isinstance(node, dict):
            raise FinanceInputError("construction graph nodes must be objects")
        props = node.get("properties") or {}
        if not isinstance(props, dict):
            raise FinanceInputError("construction node properties must be an object")
        table = _s(props.get("table") or node.get("type") or props.get("type"))
        data = props.get("data")
        if table not in TABLES or not isinstance(data, dict):
            continue
        source = _s(node.get("id"))
        if not source or len(source) > 1024:
            raise FinanceInputError("source record IDs must contain 1 to 1024 characters")
        records.append((table, source, data))
        p.counts[table] += 1
        date = _s(data.get("STATC_DT"))
        if date:
            p.dates.add(date)
        else:
            p.complete = False
            p.invalid_snapshot = True
            p.issue("SNAPSHOT_DATE_MISSING", source_id=source)
        hasher.update(json.dumps([table, source, data], sort_keys=True, ensure_ascii=False, default=str).encode())
        if table == TABLES[2]:
            continue
        kind = "institution" if table == TABLES[0] else "individual"
        try:
            bid = _id(data.get("UUID" if kind == "institution" else "IDV_ID"))
        except FinanceInputError as exc:
            p.complete = False
            p.issue("ENTITY_ID_INVALID", source_id=source, detail=str(exc))
            continue
        key = ("I:" if kind == "institution" else "P:") + bid
        name = _s(data.get("CHN_NM" if kind == "institution" else "IDV_NM"))[:160]
        status = _s(data.get("ENTP_OPR_ST"))[:80]
        previous = p.entities.get(key)
        if previous:
            previous.source_ids.append(source)
            p.counts["duplicate_entity_records"] += 1
            if previous.name != name or previous.status != status:
                p.issue("ENTITY_CONFLICT", key, source, blocking=True)
        else:
            p.entities[key] = Entity(key, bid, kind, name, status, [source])
    p.digest = hasher.hexdigest()
    if len(p.dates) != 1:
        p.invalid_snapshot = True
        p.complete = False
        p.issue("SNAPSHOT_MISMATCH", detail=", ".join(sorted(p.dates))[:250])
    for table in TABLES:
        if not p.counts[table]:
            p.complete = False
            p.issue("SOURCE_TABLE_MISSING", detail=table)
    facts: dict[tuple[str, str], Fact] = {}
    for i, (table, source, data) in enumerate(records):
        if table != TABLES[2]:
            continue
        if i % 128 == 0:
            budget.check()
        target = "I:" + _s(data.get("UUID"))
        holder_type = _s(data.get("REL_SHRHLR_TP_CODE"))
        holder = ("I:" if holder_type == "0" else "P:") + _s(data.get("REL_SHRHLR_CUST_ID"))
        if holder_type not in {"0", "1"} or target not in p.entities or holder not in p.entities:
            p.complete = False
            p.issue("POLYMORPHIC_REFERENCE_INVALID", target, source, blocking=True)
            continue
        try:
            ratio = _ratio(data.get("REL_SHRHLR_HOLD_SHR_RTO"), policy.get("ratio_format", "percent_string"))
        except FinanceInputError as exc:
            p.complete = False
            p.issue("RATIO_INVALID", target, source, str(exc), True)
            continue
        pair = (holder, target)
        if pair in facts:
            old = facts[pair]
            old.source_ids.append(source)
            p.counts["duplicate_fact_records"] += 1
            if old.ratio != ratio:
                strategy = policy.get("conflict_strategy", "reject_conflict")
                p.issue("FACT_RATIO_CONFLICT", target, source, strategy,
                        blocking=strategy != "max_ratio_per_business_fact")
                if strategy == "max_ratio_per_business_fact":
                    old.ratio = max(old.ratio, ratio)
            continue
        fact_id = hashlib.sha256(json.dumps([sorted(p.dates), holder, target]).encode()).hexdigest()[:24]
        facts[pair] = Fact("ownership_" + fact_id, holder, target, ratio, [source])
    p.facts = sorted(facts.values(), key=lambda f: (f.holder, f.target))
    semantic = {"dates": sorted(p.dates),
                "entities": [(k, e.name, e.status) for k, e in sorted(p.entities.items())],
                "facts": [(f.holder, f.target, _decimal(f.ratio)) for f in p.facts]}
    p.semantic_digest = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    total = defaultdict(Decimal)
    # Every accepted ratio is an exact multiple of 1e-100. With at most two
    # million facts, 110 digits preserve exact threshold sums (including tiny
    # additions to 0.5 or 1); economic path products still use 50 digits.
    with localcontext() as total_context:
        total_context.prec = 110
        for f in p.facts:
            total[f.target] += f.ratio
    for key, entity in p.entities.items():
        if entity.kind != "institution":
            continue
        if total[key] > ONE:
            p.issue("OWNERSHIP_TOTAL_EXCEEDED", key, detail=_decimal(total[key]), blocking=True)
        elif total[key] < ONE:
            p.counts["institutions_with_coverage_gap"] += 1
        if not total[key]:
            p.counts["institutions_without_positive_owners"] += 1
    budget.check(len(p.entities), len(p.facts))
    return p


def _indexes(p: Projection, policy: dict) -> tuple[dict, dict, dict]:
    outgoing, incoming, stopped = defaultdict(list), defaultdict(list), {}
    statuses = set(policy.get("stop_statuses", []))
    stop_ids = {_selector(v) for v in policy.get("stop_entity_ids", [])}
    excluded = {_selector(v) for v in policy.get("exclude_entity_ids", [])}
    threshold = Decimal(str(policy.get("min_edge_ratio", "0")))
    if not threshold.is_finite() or threshold < ZERO or threshold > ONE:
        raise FinanceInputError("min_edge_ratio must be in [0,1]")
    for k, e in p.entities.items():
        if e.status in statuses or k in stop_ids or e.business_id in stop_ids:
            stopped[k] = "STOP_STATUS" if e.status in statuses else "STOP_ENTITY"
    for f in p.facts:
        if f.ratio <= ZERO or f.ratio < threshold or f.target in stopped:
            continue
        if any(k in excluded or p.entities[k].business_id in excluded for k in (f.holder, f.target)):
            continue
        outgoing[f.holder].append(f)
        incoming[f.target].append(f)
    return outgoing, incoming, stopped


def _reachable(start: str, index: dict, reverse: bool, budget: Budget) -> set[str]:
    seen, pending, traversed = {start}, [start], 0
    while pending:
        node = pending.pop()
        for f in index.get(node, []):
            traversed += 1
            nxt = f.holder if reverse else f.target
            if nxt not in seen:
                seen.add(nxt)
                pending.append(nxt)
        budget.check(len(seen), traversed)
    return seen


def _components(nodes: set[str], outgoing: dict, incoming: dict, budget: Budget):
    """Nonrecursive Kosaraju; its component IDs are in topological order."""
    seen, order = set(), []
    for seed in sorted(nodes):
        if seed in seen:
            continue
        seen.add(seed)
        stack = [(seed, iter(outgoing.get(seed, [])))]
        while stack:
            node, it = stack[-1]
            edge = next(it, None)
            if edge is None:
                order.append(node)
                stack.pop()
            elif edge.target in nodes and edge.target not in seen:
                seen.add(edge.target)
                stack.append((edge.target, iter(outgoing.get(edge.target, []))))
            if len(order) % 128 == 0:
                budget.check()
    mapping, components, cyclic = {}, [], set()
    for seed in reversed(order):
        if seed in mapping:
            continue
        cid, members, stack = len(components), [], [seed]
        mapping[seed] = cid
        while stack:
            node = stack.pop()
            members.append(node)
            for edge in incoming.get(node, []):
                if edge.holder in nodes and edge.holder not in mapping:
                    mapping[edge.holder] = cid
                    stack.append(edge.holder)
        components.append(sorted(members))
        if len(members) > 1 or any(f.target == seed for f in outgoing.get(seed, [])):
            cyclic.add(cid)
        budget.check()
    return components, mapping, cyclic


def _solve(matrix: list[list[Decimal]], rhs: list[Decimal], budget: Budget):
    """Solve (I-A)x=d and (I-A)y=1. Positive y certifies rho(A)<1."""
    n = len(rhs)
    m = [[(ONE if i == j else ZERO) - matrix[i][j] for j in range(n)] + [rhs[i], ONE] for i in range(n)]
    for col in range(n):
        budget.check()
        pivot = max(range(col, n), key=lambda i: abs(m[i][col]))
        if abs(m[pivot][col]) < Decimal("1e-40"):
            return None, "SINGULAR_OR_ILL_CONDITIONED", None
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        m[col] = [v / div for v in m[col]]
        for row in range(n):
            if row != col:
                mult = m[row][col]
                if mult:
                    m[row] = [a - mult * b for a, b in zip(m[row], m[col])]
    solution, certificate = [r[n] for r in m], [r[n+1] for r in m]
    if any(v <= ZERO for v in certificate):
        return None, "NON_CONVERGENT", None
    residual = max(abs(solution[i] - rhs[i] - sum((matrix[i][j] * solution[j] for j in range(n)), ZERO)) for i in range(n))
    scale = max(ONE, *(abs(v) for v in solution))
    if residual > Decimal("1e-35") * scale or any(v < ZERO for v in solution):
        return None, "NUMERICAL_REVIEW", residual
    return solution, "CONVERGED", residual


def _economic(p: Projection, target: str, out: dict, inc: dict, budget: Budget, limit: int):
    relevant = _reachable(target, inc, True, budget)
    components, mapping, cyclic = _components(relevant, out, inc, budget)
    values, reports, failed = {}, [], set()
    has_upstream = set()
    for source in relevant:
        for edge in out.get(source, []):
            if edge.target in relevant and mapping[source] != mapping[edge.target]:
                has_upstream.add(mapping[edge.target])
    for cid in reversed(range(len(components))):
        members = components[cid]
        positions = {v: i for i, v in enumerate(members)}
        rhs = [ONE if v == target else ZERO for v in members]
        if len(members) > 128:
            failed.add(cid)
            reports.append({"component_id": cid, "status": "TOO_LARGE", "member_count": len(members)})
            continue
        matrix = [[ZERO for _ in members] for _ in members] if cid in cyclic else None
        dependency_failed = False
        for source in members:
            for edge in out.get(source, []):
                if edge.target not in relevant:
                    continue
                if mapping[edge.target] == cid:
                    if matrix is not None:
                        matrix[positions[source]][positions[edge.target]] += edge.ratio
                elif edge.target in values:
                    rhs[positions[source]] += edge.ratio * values[edge.target]
                else:
                    dependency_failed = True
        if dependency_failed:
            solution, status, residual = None, "DEPENDENCY_FAILED", None
        elif matrix is not None:
            solution, status, residual = _solve(matrix, rhs, budget)
        else:
            solution, status, residual = rhs, "ACYCLIC", ZERO
        if solution is None:
            failed.add(cid)
        else:
            values.update(zip(members, solution))
        if cid in cyclic or solution is None:
            reports.append({"component_id": cid, "status": status, "member_count": len(members),
                            "members": [p.entities[k].business_id for k in members[:limit]],
                            "residual": _decimal(residual) if residual is not None else None,
                            "member_interests": [{"entity_id": k, "business_id": p.entities[k].business_id,
                                                  "interest": _decimal(values[k])} for k in members[:limit] if k in values],
                            "equation": "(I-A)s=d; d[target]+=1", "convergence_certificate": "positive (I-A)^-1 1" if solution else None})
        budget.check()
    roots = [k for k in relevant if mapping[k] not in has_upstream and k != target]
    ranked = sorted((k for k in roots if k in values), key=lambda k: (-values[k], k))
    return {"scope": relevant, "values": values, "roots": ranked, "failed": failed,
            "components": components, "mapping": mapping, "cyclic": cyclic, "reports": reports}


def _paths(p: Projection, target: str, roots: set[str], inc: dict, top_k: int, budget: Budget) -> dict:
    # Best-first simple paths: products cannot increase since every ratio <= 1.
    # Ranking is already computed; this independent budget never changes it.
    queue = [(-ONE, (target,), ())] if roots else []
    result, expanded, frontier_truncated = [], 0, False
    while queue and len(result) < top_k and expanded < 10000:
        try:
            budget.check()
        except BudgetExceeded:
            break
        negative, nodes, facts = heapq.heappop(queue)
        expanded += 1
        current = nodes[-1]
        if current in roots:
            result.append({"nodes": [p.entities[k].business_id for k in list(reversed(nodes))[:256]],
                           "entity_ids": list(reversed(nodes))[:256], "fact_ids": list(reversed(facts))[:255],
                           "node_count": len(nodes), "path_truncated": len(nodes) > 256, "product": _decimal(-negative)})
            continue
        for edge in inc.get(current, []):
            if edge.holder not in nodes:
                heapq.heappush(queue, (negative * edge.ratio, nodes + (edge.holder,), facts + (edge.key,)))
            if len(queue) > 10000:
                frontier_truncated = True
                break
        if frontier_truncated:
            break
    return {"paths": result, "top_k": top_k, "top_k_complete": not frontier_truncated and (len(result) == top_k or not queue),
            "complete": not queue and not any(r["path_truncated"] for r in result), "expanded_states": expanded,
            "semantics": "simple paths for display; SCC economics includes convergent repeated walks"}


def _closure(root: str, out: dict, budget: Budget, allowed: set[str] | None = None):
    controlled, votes, contributed, queue = set(), defaultdict(Decimal), defaultdict(list), deque([root])
    processed, traversed = set(), 0
    with localcontext() as voting_context:
        voting_context.prec = 110
        while queue:
            holder = queue.popleft()
            if holder in processed:
                continue
            processed.add(holder)
            for edge in out.get(holder, []):
                if allowed is not None and edge.target not in allowed:
                    continue
                traversed += 1
                votes[edge.target] += edge.ratio
                contributed[edge.target].append(edge)
                if edge.target != root and votes[edge.target] > HALF and edge.target not in controlled:
                    controlled.add(edge.target)
                    queue.append(edge.target)
            budget.check(len(processed), traversed)
    return controlled, votes, contributed


def _quality(p: Projection, scope: set[str], limit: int) -> dict:
    affected = sorted(p.blocked & scope)
    return {"status": "DATA_CONFLICT" if affected or p.invalid_snapshot else "VALID_WITH_COVERAGE_GAPS" if p.counts["institutions_with_coverage_gap"] else "VALID",
            "issue_counts": dict(p.issue_counts), "issues": p.issues[:limit],
            "issues_truncated": sum(p.issue_counts.values()) > min(limit, len(p.issues)),
            "blocked_entity_count": len(p.blocked), "affected_entity_count": len(affected),
            "affected_entity_ids": affected[:limit], "source_projection_complete": p.complete,
            "coverage_gap_institutions": p.counts["institutions_with_coverage_gap"]}


def _rule_status(policy: dict) -> str:
    return "COMPLETE_FOR_SELECTED_POLICY" if policy.get("rule_coverage") == "complete" else "RULE_DATA_MISSING"


def _authority(p: Projection, target: str, policy: dict) -> tuple[list[str], str | None, list[dict]]:
    matches = []
    excluded = {_selector(v) for v in policy.get("exclude_entity_ids", [])}
    for row in policy.get("rule_relations", []):
        if not isinstance(row, dict) or row.get("type") not in {"GP", "LEGAL_PERSON"}:
            raise FinanceInputError("rule_relations require GP or LEGAL_PERSON records")
        if not row.get("authority") or not row.get("evidence_refs"):
            raise FinanceInputError("authoritative rule relations require authority and evidence_refs")
        if _resolve(p, row.get("target_id")) != target:
            continue
        if row.get("snapshot_id") not in p.dates:
            raise FinanceInputError("rule relation snapshot_id must match source snapshot")
        holder = _resolve(p, row.get("holder_id"))
        if holder in excluded or p.entities[holder].business_id in excluded:
            continue
        matches.append((row["type"], holder, row))
    if not matches:
        return [], None, []
    types = {kind for kind, _, _ in matches}
    priority = policy.get("rule_priority", [])
    if len(types) > 1 and not all(kind in priority for kind in types):
        return [], "RULE_CONFIG_MISSING", []
    selected = min(types, key=lambda kind: priority.index(kind)) if len(types) > 1 else next(iter(types))
    selected_relations = [{**row, "target_entity_id": target, "holder_entity_id": key,
                           "evidence_refs": list(row["evidence_refs"])} for kind, key, row in matches if kind == selected]
    return sorted({key for kind, key, _ in matches if kind == selected}), selected, selected_relations


def _analysis(p: Projection, operation: str, policy: dict, budget: Budget,
              target_id: Any, root_id: Any, limit: int, top_k: int, query: str = "") -> dict:
    out, inc, stopped = _indexes(p, policy)
    rule_status = _rule_status(policy)
    response = {"success": True, "operation": operation, "algorithm_version": VERSION,
                "policy_version": _s(policy.get("version", "equity-only-v1"))[:128],
                "policy_mode": policy.get("mode", "equity_only"), "rule_status": rule_status,
                "snapshot_id": next(iter(p.dates)) if len(p.dates) == 1 else None,
                "input_manifest_hash": p.digest, "complete": p.complete,
                "input_manifest_hash_semantics": "resolved current table data and record IDs in representation order",
                "semantic_hash": p.semantic_digest,
                "numeric_policy": {"decimal_precision": 50, "economic_precision": 50,
                    "control_and_quality_precision": 110, "input_significant_digits_max": 50,
                    "normalized_ratio_exponent_range": [-100, 100], "input_rounding": "reject_over_precision",
                    "arithmetic": "50-digit economic arithmetic; exact 110-digit control/quality sums within input bounds; underflow/overflow fail explicitly",
                    "output_format": "decimal strings; scientific notation for extreme exponents"},
                "scope_semantics": "observed ownership facts under the selected policy; unrecorded shareholders remain unknown",
                "effective_policy": {"ratio_format": policy.get("ratio_format", "percent_string"),
                    "conflict_strategy": policy.get("conflict_strategy", "reject_conflict"),
                    "stop_statuses": policy.get("stop_statuses", []), "stop_entity_ids": policy.get("stop_entity_ids", []),
                    "exclude_entity_ids": policy.get("exclude_entity_ids", []), "min_edge_ratio": policy.get("min_edge_ratio", "0")},
                "completeness": {"projection": p.complete, "computation": True, "display_truncated": False,
                                 "business_rules": rule_status != "RULE_DATA_MISSING"},
                "warnings": ["GP/legal-person authority and special-entity rules are not supplied; equity-only conclusions are scoped candidates."] if rule_status == "RULE_DATA_MISSING" else [],
                "result": {}, "evidence": {}}
    excluded = {_selector(v) for v in policy.get("exclude_entity_ids", [])}
    for selector in (target_id, root_id):
        if selector is None:
            continue
        selected_entity = _resolve(p, selector)
        if selected_entity in excluded or p.entities[selected_entity].business_id in excluded:
            response.update(status="EXCLUDED_BY_POLICY", analysis_status="EXCLUDED_BY_POLICY",
                            review_required=False, review_reasons=[])
            if operation in {"control_chain", "group_tree"}:
                response["control_status"] = "EXCLUDED_BY_POLICY"
            response["result"] = {"excluded_entity": _entity(p, selected_entity), "confirmed_id": None, "primary_faction_id": None}
            response["quality"] = _quality(p, {selected_entity}, limit)
            response["evidence"] = {"policy_rule": "exclude_entity_ids", "complete": True}
            return response
    if operation == "snapshot":
        scope = set(p.entities)
        components, _, cyclic = _components(scope, out, inc, budget)
        selected = sorted(p.entities)
        selected_facts = p.facts
        if target_id is not None:
            target = _resolve(p, target_id)
            selected = [target]
            selected_facts = [f for f in p.facts if f.target == target or f.holder == target]
        if query:
            query = query.casefold()
            selected = [k for k in selected if query in k.casefold() or query in p.entities[k].name.casefold()]
            selected_set = set(selected)
            selected_facts = [f for f in selected_facts if f.target in selected_set or f.holder in selected_set]
        response["status"] = "DATA_CONFLICT" if p.blocked or not p.complete else "COMPLETE"
        response["result"] = {"entity_count": len(p.entities), "ownership_fact_count": len(p.facts),
            "institution_count": sum(e.kind == "institution" for e in p.entities.values()),
            "individual_count": sum(e.kind == "individual" for e in p.entities.values()),
            "source_record_counts": dict(p.counts), "entities": [_entity(p, k) for k in selected[:limit]],
            "matched_entity_count": len(selected), "matched_fact_count": len(selected_facts),
            "facts": [_fact(p, f) for f in selected_facts[:limit]], "scc_count": len(cyclic),
            "cyclic_components": [[p.entities[k].business_id for k in components[c][:limit]] for c in sorted(cyclic)[:limit]],
            "ignored_construction_edges": True, "ratio_format": policy.get("ratio_format", "percent_string")}
        response["completeness"]["display_truncated"] = len(selected) > limit or len(selected_facts) > limit
    elif operation in {"first_holder", "economic_interest"}:
        target = _resolve(p, target_id)
        scope = _reachable(target, inc, True, budget)
        result = {"target": _entity(p, target), "confirmed_id": None, "first_holder_id": None, "first_holder_ids": []}
        response["result"] = result
        authority, authority_rule, selected_relations = _authority(p, target, policy) if operation == "first_holder" else ([], None, [])
        direct = [f for f in inc.get(target, []) if f.ratio > HALF]
        if operation == "first_holder":
            with localcontext() as direct_context:
                direct_context.prec = 110
                direct_total = sum((f.ratio for f in inc.get(target, [])), ZERO)
            result.update(direct_ownership_total=_decimal(direct_total),
                direct_strict_majority_holder_count=len({f.holder for f in direct}),
                threshold_semantics="per_single_holder", direct_ownership_scope="selected_policy_direct_facts")
        chosen, economic = [], None
        if authority_rule == "RULE_CONFIG_MISSING":
            response["status"] = authority_rule
            response["completeness"]["business_rules"] = False
        elif authority:
            chosen = authority
            scope.update(authority)
            result["rule_applied"] = authority_rule
            response["status"] = "TIED_CANDIDATES" if len(chosen) > 1 else "COMPLETE"
        elif operation == "first_holder" and direct:
            chosen = sorted({f.holder for f in direct})
            result["rule_applied"] = "DIRECT_STRICT_MAJORITY"
            response["status"] = "DATA_CONFLICT" if len(chosen) > 1 else "COMPLETE"
            _, component_of, cyclic = _components(scope, out, inc, budget)
            if len(chosen) == 1 and (component_of[target] in cyclic or component_of[chosen[0]] in cyclic):
                response["status"] = "SCC_REVIEW"
        else:
            economic = _economic(p, target, out, inc, budget, limit)
            ranked, values = economic["roots"], economic["values"]
            path_roots, requested_root = set(ranked), None
            chosen = [k for k in ranked if values[k] == values[ranked[0]]] if ranked else []
            result.update(rule_applied="CUMULATIVE_ROOT_ECONOMIC_INTEREST",
                          root_interests=[{**_entity(p, k), "interest": _decimal(values[k])} for k in ranked[:limit]],
                          root_interests_scope="boundary_roots",
                          root_count=len(ranked), ranking_complete=not economic["failed"],
                          economic_semantics="sum of all directed walks; SCC (I-A)s=d with target unit injection",
                          component_solutions=economic["reports"][:limit],
                          target_interest=_decimal(values[target]) if target in values else None)
            if operation == "economic_interest" and root_id is not None:
                requested_root = _resolve(p, root_id)
                path_roots = {requested_root} if requested_root in scope else set()
                result["requested_interest"] = {"root": _entity(p, requested_root), "target_id": p.entities[target].business_id,
                    "target_entity_id": target,
                    "interest": _decimal(values[requested_root]) if requested_root in values else "0" if requested_root not in scope else None,
                    "complete": not economic["failed"], "scope": "requested_upstream"}
            response["completeness"]["display_truncated"] = len(ranked) > limit or len(economic["reports"]) > limit
            response["status"] = "NON_CONVERGENT" if economic["failed"] else "TIED_CANDIDATES" if len(chosen) > 1 else "COMPLETE" if chosen else "NO_EVIDENCE"
            if economic["failed"]:
                chosen = []
                response["complete"] = False
                response["completeness"]["computation"] = False
            elif any(economic["mapping"][k] in economic["cyclic"] for k in chosen) and operation == "first_holder":
                response["status"] = "SCC_REVIEW"
            response["evidence"] = _paths(p, target, path_roots, inc, top_k, budget)
            response["evidence"].update(path_scope="requested_upstream" if requested_root is not None else "boundary_roots",
                root_entity_ids=[requested_root] if requested_root is not None else ranked[:limit],
                root_count=1 if requested_root is not None else len(ranked), target_id=p.entities[target].business_id,
                target_entity_id=target)
            if requested_root is not None:
                response["evidence"].update(requested_root_entity_id=requested_root,
                    requested_root_id=p.entities[requested_root].business_id)
        if operation == "first_holder":
            result["first_holder_ids"] = [p.entities[k].business_id for k in chosen[:limit]]
            result["first_holder_entity_ids"] = chosen[:limit]
            result["first_holder_count"] = len(chosen)
            result["first_holder_id"] = p.entities[chosen[0]].business_id if len(chosen) == 1 else None
            if response["status"] == "COMPLETE" and len(chosen) == 1 and rule_status != "RULE_DATA_MISSING":
                result["confirmed_id"] = result["first_holder_id"]
            if not economic:
                if authority_rule == "RULE_CONFIG_MISSING":
                    response["evidence"] = {"selected_rule_relations": [], "selected_rule_relation_count": 0,
                        "complete": False, "reason": "GP and LEGAL_PERSON require an explicit rule_priority"}
                elif authority:
                    response["evidence"] = {"selected_rule_relations": selected_relations[:limit],
                        "selected_rule_relation_count": len(selected_relations), "complete": len(selected_relations) <= limit}
                    response["completeness"]["display_truncated"] = len(chosen) > limit or len(selected_relations) > limit
                else:
                    direct_facts = inc.get(target, [])
                    response["evidence"] = {"direct_facts": [_fact(p, f) for f in direct_facts[:limit]],
                        "fact_count": len(direct_facts), "complete": len(direct_facts) <= limit}
                    response["completeness"]["display_truncated"] = len(chosen) > limit or len(direct_facts) > limit
        result["stopped_at"] = [{"entity_id": k, "reason": v} for k, v in stopped.items() if k in scope][:limit]
    elif operation == "control_chain":
        target = _resolve(p, target_id)
        scope = _reachable(target, inc, True, budget)
        components, mapping, cyclic = _components(scope, out, inc, budget)
        closures, votes, proofs, details, closure_pairs = {}, {}, {}, {}, 0
        for root in sorted(scope - {target}):
            controlled, voting, evidence = _closure(root, out, budget, scope)
            closures[root] = controlled
            closure_pairs += len(controlled)
            budget.check(edges=closure_pairs)
            if target in controlled:
                votes[root], proofs[root] = voting[target], evidence[target]
                details[root] = (voting, evidence)
        controllers = sorted(votes)
        roots = [k for k in controllers if not any(k in closures[other] for other in controllers if other != k)]
        review = mapping[target] in cyclic or any(mapping[k] in cyclic for k in controllers)
        response["scc_review_required"] = review
        response["status"] = "SCC_REVIEW" if review else "CONFIRMED_CONTROL" if len(roots) == 1 else "MULTIPLE_CONTROLLERS" if len(roots) > 1 else "NO_SINGLE_CONTROLLER"
        response["result"] = {"target": _entity(p, target), "ultimate_controller_ids": [p.entities[k].business_id for k in roots[:limit]],
            "ultimate_controller_entity_ids": roots[:limit],
            "controller_count": len(controllers), "controller_ids": [p.entities[k].business_id for k in controllers[:limit]],
            "controller_entity_ids": controllers[:limit],
            "intermediate_controller_ids": [p.entities[k].business_id for k in controllers if k not in roots][:limit],
            "intermediate_controller_entity_ids": [k for k in controllers if k not in roots][:limit],
            "equity_controller_id": p.entities[roots[0]].business_id if len(roots) == 1 and not review else None,
            "equity_controller_entity_id": roots[0] if len(roots) == 1 and not review else None,
            "confirmed_id": p.entities[roots[0]].business_id if len(roots) == 1 and not review and rule_status != "RULE_DATA_MISSING" else None,
            "controllers": [{**_entity(p, k), "controllable_voting_stake": _decimal(votes[k]),
                             "contributing_facts": [_fact(p, f) for f in proofs[k][:top_k]],
                             "contribution_count": len(proofs[k]), "evidence_complete": len(proofs[k]) <= top_k} for k in controllers[:limit]],
            "cyclic_component_count": len(cyclic), "strict_threshold": ">0.5"}
        proof_dag = []
        proof_steps = sum(len(closures[k]) for k in roots)
        for root in roots:
            voting, contributions = details[root]
            for member in sorted(closures[root]):
                if len(proof_dag) >= limit:
                    break
                proof_dag.append({"controller_id": p.entities[root].business_id,
                    "controller_entity_id": root, "controlled_entity_id": member,
                    "controlled_id": p.entities[member].business_id, "controllable_voting_stake": _decimal(voting[member]),
                    "contributions": [_fact(p, f) for f in contributions[member][:top_k]],
                    "contribution_count": len(contributions[member]), "complete": len(contributions[member]) <= top_k})
        response["evidence"] = {"proof_dag": proof_dag, "proof_step_count": proof_steps,
            "complete": len(proof_dag) == proof_steps and all(r["complete"] for r in proof_dag),
            "semantics": "Each controlled holder contributes its direct votes once; root ownership facts seed the fixed point."}
        response["completeness"]["display_truncated"] = len(controllers) > limit
    elif operation == "group_tree":
        root = _resolve(p, root_id or target_id)
        controlled, votes, proofs = _closure(root, out, budget)
        scope = {root} | controlled
        # Detect cycles in the complete reachable ownership region, including
        # non-controlled members that may form a cycle with a controlled entity.
        reachable = _reachable(root, out, False, budget)
        _, mapping, cyclic = _components(reachable, out, inc, budget)
        review = any(mapping[k] in cyclic for k in scope)
        strict_edges, strict_proofs = [], {}
        closure_by_member = {}
        for member in sorted(scope):
            descendants, voting, contributions = _closure(member, out, budget, scope)
            closure_by_member[member] = descendants
            for descendant in sorted(descendants):
                strict_edges.append((member, descendant))
                strict_proofs[(member, descendant)] = (voting[descendant], contributions[descendant])
            budget.check(edges=len(strict_edges))
        # Prefer immediate strict-control parents. Transitive and alternate
        # control edges remain in additional_edges, never weak raw ownership.
        reduced = []
        for a, b in strict_edges:
            if not any(c != b and b in closure_by_member.get(c, set()) for c in closure_by_member[a]):
                reduced.append((a, b))
            budget.check()
        if review:
            reduced = strict_edges
        parent, tree, pending = {root: None}, [], deque([root])
        by_source = defaultdict(list)
        for source, target in sorted(reduced):
            by_source[source].append(target)
        while pending:
            source = pending.popleft()
            for target in by_source[source]:
                if target not in parent:
                    parent[target] = source
                    tree.append((source, target))
                    pending.append(target)
        shown = list(parent)[:limit]
        shown_set = set(shown)
        tree_set = set(tree)
        def public_edge(edge):
            a, b = edge
            voting, contributions = strict_proofs[edge]
            return {"source": p.entities[a].business_id, "target": p.entities[b].business_id,
                    "source_entity_id": a, "target_entity_id": b,
                    "controllable_voting_stake": _decimal(voting), "contribution_fact_ids": [f.key for f in contributions[:top_k]],
                    "contribution_count": len(contributions), "evidence_complete": len(contributions) <= top_k}
        response["scc_review_required"] = review
        response["status"] = "NO_CONTROLLED_MEMBERS" if not controlled else "SCC_REVIEW" if review else "IDENTIFIED_CONTROL_FACTION"
        response["result"] = {"root": _entity(p, root), "faction_type": "CONTROL", "member_count": len(scope),
            "node_count": len(scope), "node_count_includes_root": True, "member_count_includes_root": True,
            "controlled_member_count": len(controlled), "tree_edge_count": len(tree),
            "control_dag_edge_count": len(strict_edges), "count_scope": "complete_computed_scope_before_display_limits",
            "members": [_entity(p, k) for k in shown], "tree_edges": [public_edge(e) for e in tree if e[0] in shown_set and e[1] in shown_set],
            "additional_edges": [public_edge(e) for e in strict_edges if e not in tree_set and e[0] in shown_set and e[1] in shown_set][:limit],
            "control_dag": [public_edge(e) for e in strict_edges[:limit]], "control_edge_count": len(strict_edges),
            "primary_faction_id": ("CONTROL:" + p.semantic_digest[:16] + ":" + root) if controlled and not review and rule_status != "RULE_DATA_MISSING" else None,
            "cyclic_component_count": len({mapping[k] for k in scope if mapping[k] in cyclic}),
            "tree_semantics": "spanning tree of strict control, preferring non-transitive parents; additional_edges retains alternate/transitive control",
            "additional_edge_count": len(strict_edges) - len(tree)}
        group_facts = [f for f in p.facts if f.holder in scope and f.target in scope]
        response["evidence"] = {"facts": [_fact(p, f) for f in group_facts[:limit]],
            "fact_count": len(group_facts), "complete": len(group_facts) <= limit}
        response["completeness"]["display_truncated"] = len(scope) > limit or len(strict_edges) > limit
    else:
        raise FinanceInputError("unsupported finance operation")
    # Algorithm outcome and source quality are orthogonal. Preserve SCC review
    # even when the top-level status is subsequently blocked by bad totals.
    response["analysis_status"] = response["status"]
    if operation in {"control_chain", "group_tree"}:
        response["control_status"] = response["status"]
    response["review_reasons"] = [response["status"]] if response["status"] in {
        "SCC_REVIEW", "NON_CONVERGENT", "NUMERICAL_REVIEW", "RULE_CONFIG_MISSING"} else []
    if response.get("scc_review_required") and "SCC_REVIEW" not in response["review_reasons"]:
        response["review_reasons"].append("SCC_REVIEW")
    response["quality"] = _quality(p, scope, limit)
    if response["evidence"].get("complete") is False:
        response["completeness"]["display_truncated"] = True
    if p.invalid_snapshot or p.blocked & scope or not p.complete:
        response["status"] = "DATA_CONFLICT"
        response["review_reasons"].append("DATA_CONFLICT")
        response["result"]["confirmed_id"] = None
        if "primary_faction_id" in response["result"]:
            response["result"]["primary_faction_id"] = None
    response["review_required"] = bool(response["review_reasons"])
    response["budget_used"] = {"elapsed_seconds": round(time.monotonic() - budget.started, 6),
                               "input_entities": len(p.entities), "ownership_facts": len(p.facts)}
    # Evidence may use the remaining time but never invalidates a completed
    # ranking; every substantive computation above checks the shared budget.
    return response


def analyze_finance(construction_graph: dict, operation: str, **kwargs) -> dict:
    """Public bounded entry point. Business/input failures are structured results.

    Policy: mode=equity_only, version, ratio_format, conflict_strategy,
    stop_statuses / stop_entity_ids / exclude_entity_ids, min_edge_ratio,
    rule_relations (typed, authoritative, snapshot-bound), rule_priority,
    rule_coverage='complete' only for an explicitly supplied reviewed rule pack.
    """
    try:
        allowed_arguments = {"target_id", "root_id", "query", "policy", "evidence_top_k", "display_limit", "max_nodes", "max_edges", "max_seconds"}
        if set(kwargs) - allowed_arguments:
            raise FinanceInputError("unsupported arguments: " + ", ".join(sorted(set(kwargs) - allowed_arguments)))
        if not isinstance(construction_graph, dict):
            raise FinanceInputError("construction_graph must be an object")
        policy = kwargs.get("policy", {})
        if not isinstance(policy, dict):
            raise FinanceInputError("policy must be an object")
        allowed_policy = {"mode", "version", "ratio_format", "conflict_strategy", "stop_statuses", "stop_entity_ids",
                          "exclude_entity_ids", "min_edge_ratio", "rule_relations", "rule_priority", "rule_coverage"}
        if set(policy) - allowed_policy:
            raise FinanceInputError("unsupported policy fields: " + ", ".join(sorted(set(policy) - allowed_policy)))
        if policy.get("ratio_format", "percent_string") not in {"percent_string", "percent_number", "fraction"}:
            raise FinanceInputError("unsupported ratio_format")
        if policy.get("rule_coverage", "missing") not in {"missing", "complete"}:
            raise FinanceInputError("unsupported rule_coverage")
        for name in ("stop_statuses", "stop_entity_ids", "exclude_entity_ids", "rule_priority"):
            max_length = 258 if name in {"stop_entity_ids", "exclude_entity_ids"} else 256
            if name in policy and (not isinstance(policy[name], list) or len(policy[name]) > 100 or any(not isinstance(v, str) or not v or len(v) > max_length for v in policy[name])):
                raise FinanceInputError(name + " must be a bounded list of nonempty strings")
            if name in {"stop_entity_ids", "exclude_entity_ids"}:
                for value in policy.get(name, []):
                    _selector(value)
        priorities = policy.get("rule_priority", [])
        if set(priorities) - {"GP", "LEGAL_PERSON"} or len(priorities) != len(set(priorities)):
            raise FinanceInputError("rule_priority supports GP and LEGAL_PERSON only")
        if not isinstance(policy.get("rule_relations", []), list) or len(policy.get("rule_relations", [])) > 100:
            raise FinanceInputError("rule_relations must be a bounded list")
        for relation in policy.get("rule_relations", []):
            fields = {"type", "target_id", "holder_id", "snapshot_id", "authority", "evidence_refs"}
            if not isinstance(relation, dict) or set(relation) != fields or relation["type"] not in {"GP", "LEGAL_PERSON"}:
                raise FinanceInputError("invalid authoritative rule relation fields")
            for field_name in fields - {"evidence_refs"}:
                max_length = 258 if field_name in {"target_id", "holder_id"} else 256
                if not isinstance(relation[field_name], str) or not relation[field_name].strip() or len(relation[field_name]) > max_length:
                    raise FinanceInputError("invalid authoritative rule relation value")
                if field_name in {"target_id", "holder_id"}:
                    _selector(relation[field_name])
            refs = relation["evidence_refs"]
            if not isinstance(refs, list) or not 1 <= len(refs) <= 10 or any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in refs):
                raise FinanceInputError("rule evidence_refs require 1..10 bounded references")
        if "version" in policy and (not isinstance(policy["version"], str) or not policy["version"].strip() or len(policy["version"]) > 128):
            raise FinanceInputError("policy version must contain 1..128 characters")
        if policy.get("conflict_strategy", "reject_conflict") not in {"reject_conflict", "max_ratio_per_business_fact"}:
            raise FinanceInputError("unsupported conflict_strategy")
        if policy.get("mode", "equity_only") not in {"equity_only", "configured_rules"}:
            raise FinanceInputError("unsupported policy mode")
        if policy.get("rule_relations") and policy.get("mode", "equity_only") != "configured_rules":
            raise FinanceInputError("rule_relations require explicit configured_rules mode")
        if policy.get("rule_coverage") == "complete" and (not policy.get("version") or policy.get("mode") != "configured_rules"):
            raise FinanceInputError("complete rule coverage requires configured_rules and a versioned reviewed rule pack")
        query = kwargs.get("query", "")
        if not isinstance(query, str) or len(query) > 200 or (query and operation != "snapshot"):
            raise FinanceInputError("query is supported only for snapshot and must contain at most 200 characters")
        for integer_name in ("display_limit", "evidence_top_k", "max_nodes", "max_edges"):
            if integer_name in kwargs and type(kwargs[integer_name]) is not int:
                raise FinanceInputError(integer_name + " must be an integer")
        limit, top_k = kwargs.get("display_limit", 100), kwargs.get("evidence_top_k", 5)
        if not 1 <= limit <= 500 or not 0 <= top_k <= 30:
            raise FinanceInputError("display_limit must be 1..500; evidence_top_k must be 0..30")
        if type(kwargs.get("max_seconds", 10)) not in {int, float}:
            raise FinanceInputError("max_seconds must be a number")
        budget = Budget(kwargs.get("max_nodes", 100000), kwargs.get("max_edges", 1000000), float(kwargs.get("max_seconds", 10)))
        if not 1 <= budget.max_nodes <= 200000 or not 1 <= budget.max_edges <= 2000000 or not 0 < budget.max_seconds <= 60:
            raise FinanceInputError("positive node/edge budgets and 0<max_seconds<=60 are required")
        with localcontext() as context:
            context.prec = 50
            context.traps[Underflow] = True
            context.traps[Overflow] = True
            projection = project_ownership(construction_graph, policy, budget)
            if projection.issue_counts["SOURCE_TABLE_MISSING"]:
                return {"success": False, "status": "FINANCE_SCHEMA_REQUIRED", "operation": operation,
                        "complete": False, "result": {}, "error": "Original records from institution_main, individual_info and institution_shareholder_relationship are required."}
            return _analysis(projection, operation, policy, budget, kwargs.get("target_id"), kwargs.get("root_id"), limit, top_k, query)
    except BudgetExceeded as exc:
        return {"success": False, "status": "TRUNCATED", "operation": operation, "algorithm_version": VERSION,
                "complete": False, "completeness": {"projection": False, "computation": False, "display_truncated": True},
                "result": {"confirmed_id": None, "primary_faction_id": None}, "error": str(exc)}
    except (Underflow, Overflow) as exc:
        return {"success": False, "status": "NUMERICAL_REVIEW", "operation": operation,
                "algorithm_version": VERSION, "complete": False,
                "completeness": {"projection": False, "computation": False, "display_truncated": True},
                "result": {"confirmed_id": None, "primary_faction_id": None},
                "error": "Decimal arithmetic exceeded its supported exponent range: " + type(exc).__name__}
    except (FinanceInputError, InvalidOperation, TypeError, ValueError, KeyError) as exc:
        status = str(exc) if str(exc) in {"NOT_FOUND", "AMBIGUOUS_ID"} else "INVALID_ARGUMENT"
        return {"success": False, "status": status, "operation": operation, "algorithm_version": VERSION,
                "complete": False, "result": {"confirmed_id": None}, "error": str(exc)[:300]}

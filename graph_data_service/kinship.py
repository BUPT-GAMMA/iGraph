"""Read verified relationship assertions without composing social ties into blood kinship."""

from collections import defaultdict
from itertools import combinations

TOOL = {
    "name": "inspect_kinship",
    "description": "核验当前属性图中的亲缘、名义亲属、误认、婚姻和师承。涉及身世/父母/祖辈/表亲/师门亲属时优先调用。只沿已核实 biological_parent/grandparent 计算祖先，返回证据边、共同祖先、不同生父核验；普通路径、师徒、情人、名义父子不参与血缘推导。persons 可用真实节点 ID 或唯一姓名；省略则返回全部已核实事实（有界）。未审核抽取边不等于事实，缺证据不等于无关系。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "persons": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        },
        "additionalProperties": False,
    },
    "category": "analysis",
}


def inspect_kinship(gd, persons=None):
    if persons is not None and (
        not isinstance(persons, list)
        or len(persons) > 20
        or any(not isinstance(x, str) or not x.strip() for x in persons)
    ):
        return {
            "success": False,
            "code": "INVALID_ARGUMENT",
            "error": "persons must contain at most 20 names",
        }
    graph = gd.graph_metadata.get("construction_graph") or {}
    if not graph:
        return {
            "success": False,
            "code": "RELATIONSHIP_EVIDENCE_UNAVAILABLE",
            "error": "Current graph has no typed relationship evidence; generic topology cannot prove kinship.",
        }
    from graph_data_service.compute import GraphCompute

    names = {}
    for node in graph["nodes"]:
        props = node.get("properties", {})
        data = props.get("data")
        name = props.get("name") or (
            data.get("name") if isinstance(data, dict) else None
        )
        names[node["id"]] = name if isinstance(name, str) else node["id"]
    identities = {}
    for node in graph["nodes"]:
        identity = node.get("properties", {}).get("kinship_identity")
        identities[node["id"]] = (
            identity if isinstance(identity, str) and identity in names else node["id"]
        )
    focus = []
    for person in persons or []:
        result = GraphCompute._resolve_node_name(gd, person)
        if isinstance(result, dict):
            return result
        result = identities.get(result, result)
        if result not in focus:
            focus.append(result)
    facts = []
    for edge in graph["edges"]:
        semantic = edge.get("properties", {}).get("relationship", {})
        if (
            not isinstance(semantic, dict)
            or semantic.get("review_status") != "verified"
            or not isinstance(semantic.get("kind"), str)
        ):
            continue
        source = identities.get(edge["source"], edge["source"])
        target = identities.get(edge["target"], edge["target"])
        facts.append(
            {
                "edge_id": edge["id"],
                "source": source,
                "source_name": names[source],
                "target": target,
                "target_name": names[target],
                "relation": edge["type"],
                **{
                    key: semantic[key]
                    for key in (
                        "schema",
                        "kind",
                        "role",
                        "review_status",
                        "evidence",
                        "direction",
                    )
                    if key in semantic
                },
            }
        )
    if len(facts) > 1000:
        return {
            "success": False,
            "code": "RELATIONSHIP_SCOPE_TOO_LARGE",
            "error": "Over 1000 reviewed facts; use a smaller graph.",
        }
    parents = defaultdict(list)
    for fact in facts:
        if fact["kind"] in {"biological_parent", "biological_grandparent"}:
            parents[fact["target"]].append(fact)
    # Conflicting father/mother assertions cannot silently be selected as truth.
    conflicts = []
    for child, edges in parents.items():
        for role in ("father", "mother"):
            candidates = {
                e["source"]
                for e in edges
                if e["kind"] == "biological_parent" and e.get("role") == role
            }
            if len(candidates) > 1:
                conflicts.append(
                    {
                        "child": names[child],
                        "role": role,
                        "candidates": [names[n] for n in sorted(candidates)],
                    }
                )

    def ancestors(node):
        out = []
        stack = [(node, 0, [], {node}, [])]
        while stack:
            child, depth, path, seen, lineage = stack.pop()
            for e in parents[child]:
                d = depth + (2 if e["kind"] == "biological_grandparent" else 1)
                if d > 4:
                    continue
                if e["source"] in seen:
                    conflicts.append(
                        {"code": "ANCESTRY_CYCLE", "edge_id": e["edge_id"]}
                    )
                    continue
                p = path + [e["edge_id"]]
                roles = lineage + (
                    {
                        "paternal_grandmother": ["father", "mother"],
                        "maternal_grandmother": ["mother", "mother"],
                        "paternal_grandfather": ["father", "father"],
                        "maternal_grandfather": ["mother", "father"],
                    }.get(e.get("role"), ["unknown", "unknown"])
                    if e["kind"] == "biological_grandparent"
                    else [e.get("role", "unknown")]
                )
                title = {
                    ("father",): "生父",
                    ("mother",): "生母",
                    ("father", "father"): "祖父",
                    ("father", "mother"): "祖母",
                    ("mother", "father"): "外祖父",
                    ("mother", "mother"): "外祖母",
                }.get(tuple(roles), "祖先")
                out.append(
                    {
                        "ancestor": e["source"],
                        "name": e["source_name"],
                        "generations": d,
                        "edge_ids": p,
                        "lineage_roles": roles,
                        "relationship_to_subject": title,
                    }
                )
                if len(out) > 200:
                    return out[:200], True
                stack.append((e["source"], d, p, seen | {e["source"]}, roles))
        return out, False

    subjects = focus or sorted(parents)
    ancestry = {}
    truncated = False
    for node in subjects:
        rows, cut = ancestors(node)
        ancestry[node] = {"name": names[node], "ancestors": rows}
        truncated |= cut
    pairs = []
    for a, b in combinations(focus, 2):
        af = {
            e["source"]
            for e in parents[a]
            if e["kind"] == "biological_parent" and e.get("role") == "father"
        }
        bf = {
            e["source"]
            for e in parents[b]
            if e["kind"] == "biological_parent" and e.get("role") == "father"
        }
        common = []
        for x in ancestry[a]["ancestors"]:
            for y in ancestry[b]["ancestors"]:
                if x["ancestor"] == y["ancestor"]:
                    common.append(
                        {
                            "name": x["name"],
                            "generations": [x["generations"], y["generations"]],
                            "evidence": [x["edge_ids"], y["edge_ids"]],
                            "relationships_to_persons": [
                                x["relationship_to_subject"],
                                y["relationship_to_subject"],
                            ],
                            "lineage_roles": [x["lineage_roles"], y["lineage_roles"]],
                        }
                    )
        pairs.append(
            {
                "persons": [names[a], names[b]],
                "same_biological_father": (
                    af == bf if len(af) == len(bf) == 1 else "not_established"
                ),
                "biological_fathers": [
                    [names[n] for n in sorted(af)],
                    [names[n] for n in sorted(bf)],
                ],
                "common_ancestors": common,
                "blood_relationship_coverage": "known_paths_only; missing_ancestors_unknown",
            }
        )
    scope = set(focus)
    for v in ancestry.values():
        scope.update(e["ancestor"] for e in v["ancestors"])
    visible = [
        e for e in facts if not focus or e["source"] in scope or e["target"] in scope
    ]
    return {
        "success": True,
        "status": "CONFLICT" if conflicts else "VERIFIED_ASSERTIONS",
        "facts": visible[:200],
        "facts_total": len(visible),
        "facts_truncated": len(visible) > 200,
        "biological_ancestry": ancestry,
        "ancestry_truncated": truncated,
        "pair_checks": pairs,
        "conflicts": conflicts,
        "coverage": {
            "unrecorded_ancestors": "unknown_not_absent",
            "age_order": "not_established",
            "cousin_titles": "A shared grandparent via a son and a daughter supports a maternal/paternal cross-cousin line (表亲); no senior/junior title without age evidence.",
            "same_father": "Different verified fathers exclude same-father siblings, not all distant kinship.",
        },
        "rules": [
            "Only verified biological edges enter ancestry; nominal/adoptive, mistaken, social, spouse and mentor ties never do.",
            "Historical beliefs record a character's misconception, not actual parentage.",
            "Cousin/uncle titles require a complete typed family chain; generic paths and romance cannot establish them.",
            "Mentor ancestry and spouse ancestry explain different kinds of connection; neither proves blood kinship of the queried people.",
            "Unreviewed extracted edges and missing parents remain unknown; do not select conflicting assertions.",
            "Direction: source has the named role relative to target.",
        ],
    }

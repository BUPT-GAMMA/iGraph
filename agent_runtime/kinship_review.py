"""Create a small, current-turn evidence ledger for a final kinship review."""

import json
from agno.models.message import Message


def analysis_evidence(messages):
    current = []
    for message in messages:
        if message.role == "user":
            current = []
        current.append(message)
    records = []
    for message in current:
        if message.role != "tool" or message.tool_name not in {
            "community_detection",
            "node_centrality",
        }:
            continue
        try:
            data = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("success") is not True:
            continue
        labels = data.get("node_labels") or {}

        def named(value):
            if isinstance(value, str):
                return labels.get(value, value)
            if isinstance(value, list):
                return [named(v) for v in value]
            if isinstance(value, dict):
                return {k: named(v) for k, v in value.items()}
            return value

        if message.tool_name == "community_detection":
            record = {
                k: data[k]
                for k in (
                    "algorithm",
                    "num_communities",
                    "modularity",
                    "communities_truncated",
                )
                if k in data
            }
            record["communities"] = (data.get("communities") or [])[:25]
            record["focus_node_communities"] = (
                data.get("focus_node_communities") or []
            )[:100]
        else:
            record = {
                "centrality_type": data.get("centrality_type"),
                "top_nodes": (data.get("top_nodes") or [])[:40],
            }
        records.append(named(record))
    return records[-8:]


def review_request(messages, draft, response_format=None):
    if response_format is not None or not isinstance(draft, str) or not draft.strip():
        return None
    current = []
    for message in messages:
        if message.role == "user":
            current = []
        current.append(message)
    facts = {}
    ancestors = {}
    pairs = {}
    conflicts = []
    question = ""
    for message in current:
        if message.role == "user":
            question = message.content
        if message.role != "tool" or message.tool_name != "inspect_kinship":
            continue
        try:
            result = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict) or result.get("success") is not True:
            continue
        conflicts.extend(result.get("conflicts", []))
        for fact in result.get("facts", []):
            row = {
                k: fact.get(k)
                for k in ("source_name", "relation", "target_name", "kind", "role")
            }
            facts[json.dumps(row, sort_keys=True, ensure_ascii=False)] = row
        for entry in result.get("biological_ancestry", {}).values():
            for ancestor in entry.get("ancestors", []):
                row = {
                    "person": entry["name"],
                    "ancestor": ancestor["name"],
                    "ancestor_is_persons": ancestor.get(
                        "relationship_to_subject", "祖先"
                    ),
                    "generations": ancestor["generations"],
                }
                ancestors[json.dumps(row, sort_keys=True, ensure_ascii=False)] = row
        for pair in result.get("pair_checks", []):
            if pair.get("same_biological_father") is False or pair.get(
                "common_ancestors"
            ):
                row = {
                    "persons": pair["persons"],
                    "same_biological_father": pair["same_biological_father"],
                    "biological_fathers": pair.get("biological_fathers"),
                }
                pairs[json.dumps(row, sort_keys=True, ensure_ascii=False)] = row
    analysis = analysis_evidence(messages)
    if not facts and not any("communities" in record for record in analysis):
        return None
    ledger = {
        "verified_relationships": list(facts.values()),
        "verified_ancestors": list(ancestors.values()),
        "parent_checks": list(pairs.values()),
        "conflicts": conflicts,
        "analysis_results": analysis,
        "unknowns": "未记录不等于不存在；未核实长幼；师父的子女不自动成为其弟子。",
    }
    # Keep the original system instructions, including the current UI language.
    instructions = [
        message for message in messages if message.role in {"system", "developer"}
    ]
    instruction = Message(
        role="system",
        content=(
            "本次调用是最终答案发布前的亲缘事实校对，所有图工具已经完成，不再调用工具。"
            "下一条消息是JSON资料，question是用户原问题，draft是尚未展示的候选答案，ledger是当前图核验后的关系表。"
            "逐条对照ledger纠正draft中错误的血缘、祖辈方向、姻亲、师门或长幼称谓，"
            "表格、示意图、开头和总结中的同一关系必须全部一致。"
            "例如祖母不能改写成外祖母；母亲的父母只能称外祖父母；师父的女儿不自动是弟子的师姐；"
            "不同生父不是完全没有血缘的证明，缺少共同祖先记录只能写未确认。"
            "保留正常文学阐释与用户问题的完整回答，不额外审查文学扩展，不加入图谱里没有的确定亲缘。"
            "明显混淆的人物身份称谓应纠正，拿不准的身份修饰可省略。"
            "analysis_results包含本轮算法的成员归属、社区人数、中心性排序；有这些数据时同步纠正正文的统计误报。"
            "严格区分文学圈子和算法社区：不得合并多个社区后沿用其中一个社区的人数，也不得把子圈子另算作算法社区。"
            "nodes_preview只有部分成员；归属和圈内核心优先看focus_node_communities，不用邻居关系补造成员名单。"
        "表格、开头、正文和结尾必须使用一致的社区归属；不能先列同组又说两人分离。"
            "保持同一算法的社区划分，不混用不同算法结果，不将别名节点中心性直接相加。不能核实的具体排名或归属应删去，文学阐释保留。"
            "直接输出校对后的完整答案，不输出检查报告、草稿、解释或新的工具请求。"
            "严格遵循原系统给定的本轮界面语言。中文第一行以‘结论：’给一句话结论，末段以‘总结：’收束；"
            "英文对应Conclusion:与Summary:。不要复述执行进度。"
        ),
    )
    request = instructions + [
        instruction,
        Message(
            role="user",
            content=json.dumps(
                {"question": question, "ledger": ledger, "draft": draft},
                ensure_ascii=False,
            ),
        ),
    ]
    return request, len(facts)


def has_tool_calls(events):
    return any(getattr(event, "tool_calls", None) for event in events)


def content_of(events):
    return "".join(
        event.content
        for event in events
        if isinstance(getattr(event, "content", None), str)
    )

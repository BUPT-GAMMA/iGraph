"""Validate finance prose against this turn's actual tool evidence."""
import json
from agno.models.message import Message

TOOLS = {"finance_inspect_ownership", "finance_first_holder", "finance_control_chain",
         "finance_group_tree", "finance_economic_interest"}


def finance_evidence(messages):
    records = []
    for m in messages:
        if m.role == "user":
            records = []
        if m.role != "tool" or m.tool_name not in TOOLS:
            continue
        try:
            data = json.loads(m.content)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("success") is True:
            records.append({"tool": m.tool_name, "evidence": data})
    return records[-8:]


def review_request(messages, draft, response_format=None):
    if response_format is not None or not isinstance(draft, str) or not draft.strip():
        return None
    evidence = finance_evidence(messages)
    if not evidence:
        return None
    instructions = [m for m in messages if m.role in {"system", "developer"}]
    instructions.append(Message(role="system", content=(
        "发布金融回答前核对。只输出修正后的完整正文，不描述核对过程、不再调用工具。"
        "按原系统的当前界面语言回答，中文以一句‘结论：’开头和‘总结：’结尾，英文用 Conclusion: 和 Summary:。"
        "依据 ledger 中本轮实际工具结果，不猜统计值或状态。保留来源 ID。"
        "全图统计与目标主体、上游范围严格分开；matched_fact_count 包含入向与出向，不是直接股东数。"
        "优先使用 direct_ownership_summaries 的完整直接股东统计；有限展示列表不是全量。"
        "覆盖缺口表示已记录直接持股总比例不足100%，不等于没有正向股东；后者为 institutions_without_positive_owners。"
        "全图 DATA_CONFLICT 不可写成目标已有冲突；目标是否受影响读 scoped quality/affected_entity_ids。"
        "confirmed_id 为空不得宣称全部业务口径确认；RULE_DATA_MISSING 表示缺权威规则，不是股权计算失败。"
        "区分第一大股东候选、严格过半的可控制表决权、单一路径乘积、完整累计经济权益。"
        "NO_SINGLE_CONTROLLER 不等于没有控制人；SCC 存在不等于必然不收敛；只有本轮具体数值求解状态才能判定。"
        "economic_interest 必须用 requested_interest.interest 回答指定根，不把 root_interests 的首位或 Top-K 路径合计当作完整累计。指定上游主体未必是 boundary_roots，不得将它插入边界根榜单或改造排名。无需额外榜单时省略该扩展。路径比例不是会计并表比例。"
        "group_tree 的 tree_edges、additional_edges、普通持股证据互不替代；区分含根成员数与受控成员数。附加传递控制边是同一链路的可达摘要，不是新增独立路径，也不能说成原始直接持股边。结尾质量状态须与正文和工具一致。"
        "问题明确要求暂不下控制结论时，不提前宣布第一大股东或控制人。当前数据仅支持股权关联核查，不作授信决策。"
    )))
    question = next((m.content for m in reversed(messages) if m.role == "user"), "")
    instructions.append(Message(role="user", content=json.dumps(
        {"question": question, "ledger": evidence, "draft": draft}, ensure_ascii=False)))
    return instructions, len(evidence)

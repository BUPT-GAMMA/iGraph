"""Review the molecular answer against successful tools from this turn only."""
import ast
import json
from agno.models.message import Message

def borylation_evidence(messages):
    records = []
    for message in messages:
        if message.role == "user":
            records = []
        if message.role != "tool" or message.tool_name != "borylation_case_analysis":
            continue
        try:
            data = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("success") is not True:
            continue
        result = data.get("result")
        if not isinstance(result, dict):
            continue
        # The condition matrix has 96 rows; summaries and actual successes
        # retain all facts needed for the six-question review.
        records.append({**{key: data[key] for key in ("step", "structure_summary", "limitations", "prediction_status") if key in data},
            "result": {key: value for key, value in result.items() if key != "matrix"}})
    return records[-6:]

def rendering_receipts(messages):
    receipts = []
    for message in messages:
        if message.role == "user":
            receipts = []
        if message.role != "tool" or message.tool_name not in {"visualize_subgraph", "highlight_nodes"}:
            continue
        try:
            data = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("success") is not True:
            continue
        receipt = {"tool": message.tool_name, **{k:data[k] for k in ("success","num_nodes","num_edges","highlights") if k in data}}
        # Only small rendering receipts, never the full canvas.
        if len(json.dumps(receipt)) <= 12000:
            receipts.append(receipt)
    return receipts[-6:]


def review_request(messages, draft, response_format=None):
    if response_format is not None or not isinstance(draft, str) or not draft.strip():
        return None
    evidence = borylation_evidence(messages)
    if not evidence:
        return None
    question = next((m.content for m in reversed(messages) if m.role == "user"), "")
    instructions = [m for m in messages if m.role in {"system", "developer"}]
    instructions.append(Message(role="system", content=(
        "这是分子硼化示例最终答案发布前的数据核对。工具已完成，不再调用工具。"
        "下一条 JSON 的 question 为本轮问题，draft 为候选答案，ledger 为本轮成功工具的结果。"
        "只输出修正后的完整答案，保留必要表格；不输出核对过程。遵循原系统本轮界面语言。"
        "rendering_receipts 是本轮成功画图工具回执；有成功回执及 highlights 时保留已展示、高亮的描述，不能改写成未执行或未确认。逐条核对数字、分组、环归属、产物位点、实验条件和结尾汇总。开头、表格、正文和结尾必须一致。"
        "环用 structure_summary.rings 中的大小与元素描述，位点用 1 起始显示编号，symmetry_class 只是内部 ID。"
        "每个环贡献的候选与不等价类别必须对应 per_ring_sites，各环类别之和等于 distinct_site_count。"
        "不能把同一对位取代苯环的镜面对称解释成两个取代基相同；没有核验的官能团位置或机制修饰应省略。"
        "没有记录不能写成绝对未研究、从未报道或确定不反应。已报道只指当前数据，不代表穷尽文献。"
        "达标条件应覆盖 successful_conditions 全部记录；binary=0 不等于零转化，区分非零、达标、转化率和选择性。"
        "产物与条件只按底物关联，不能配对某个条件和位点，不能据此说其它位点在96组中未反应。"
        "统计观察不能升格为必要条件、位点选择性或已验证机理。计划中的结果仍未知，历史数值只能作为参考。"
        "配体只能用 ledger 里的 name/CAS，不补写完整名称、取代位次或官能团类型；尤其不能根据 CAS 猜名称。只修正数据错误和无依据的确定化学判断，保留有助理解且标明假设的解释。"
        "避免同一句结论重复。中文首段‘结论：’一句话，末段‘总结：’；英文为 Conclusion: 与 Summary:。"
    )))
    instructions.append(Message(role="user", content=json.dumps({"question": question, "ledger": evidence, "rendering_receipts": rendering_receipts(messages), "draft": draft}, ensure_ascii=False)))
    return instructions, len(evidence)

def evidence_tool_choice(messages, tools, tool_choice, run_response, response_format=None):
    """Require one fresh read for a backend-verified molecular session."""
    if response_format is not None or tool_choice not in (None, "auto"):
        return tool_choice
    state = getattr(run_response, "session_state", None) or {}
    # Agno fills RunOutput.session_state on save; during generation its
    # freshly constructed system message carries the current session state.
    if not state:
        for message in reversed(messages):
            if message.role != "system" or not isinstance(message.content, str):
                continue
            _, marker, payload = message.content.rpartition("<session_state>")
            if not marker or "</session_state>" not in payload:
                continue
            payload = payload.split("</session_state>", 1)[0].strip()
            if len(payload) > 1_000_000:
                continue
            try:
                state = ast.literal_eval(payload)
            except (SyntaxError, ValueError, RecursionError):
                continue
            if isinstance(state, dict):
                break
            state = {}
    data = state.get("data_state") or {}
    background = data.get("graph_background") or {}
    if background.get("case_id") != "borylation" or not background.get("dataset_id"):
        return tool_choice
    if state.get("active_dataset_id") != background["dataset_id"]:
        return tool_choice
    name = "borylation_case_analysis"
    if not any(isinstance(tool, dict) and (tool.get("function") or {}).get("name") == name for tool in (tools or [])):
        return tool_choice
    # A failed read must not create an endless forced-call loop.
    for message in reversed(messages):
        if message.role == "user":
            break
        if message.role == "tool" and message.tool_name == name:
            return tool_choice
    return {"type": "function", "function": {"name": name}}

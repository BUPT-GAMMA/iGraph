"""A read-only, evidence-based molecular case available to UI and Agent."""

BORYLATION_TOOL_NAME = "borylation_case_analysis"
BORYLATION_STEPS = ("structure", "symmetry", "products", "conditions", "plan")
BORYLATION_TOOLS = [{
    "name": BORYLATION_TOOL_NAME,
    "description": (
        "分析当前会话的分子硼化反应示例，直接从已加载图的原子、产物与实验记录计算结果。"
        "该案例的六问必须先调用本工具核对事实：structure 查重原子、化学键和芳香C–H候选；"
        "描述所属环时必须依据 structure.rings 和 candidate_atoms.ring_memberships，不能凭 SMILES 猜测。"
        "含 S 的五元芳杂环不能称三唑环，位点9不能归入苯环。"
        "symmetry 合并对称等价位点；products 查三个已报道产物及位置；"
        "conditions 统计96组配体/溶剂条件的成败、转化率排序与同底物条件对照；"
        "plan 按已有记录生成小批复核实验草案。"
        "它是公开实验复盘，不是模型位点预测；返回的转化率不是位点选择性或分离收率。"
        "产物表和条件表仅按底物关联，不能把最高转化条件指定给某个位点。"
        "本例96条binary标签与5%阈值一致，binary=0不等于零转化，不能直接外推其它底物。"
        "不可从三个产物推断这96组实验的产物分布，不可把环己烷说成必要条件。"
        "只有当前授权会话加载了该案例才可用，不能输入文件路径或其它会话。"
    ),
    "inputSchema": {"type": "object", "additionalProperties": False,
                    "properties": {"step": {"type": "string", "enum": list(BORYLATION_STEPS)}},
                    "required": ["step"]},
    "category": "compute",
}]

"""Questions supplied for the saved finance risk graph."""
FINANCE_RISK_CASE = {
    "id": "finance_risk", "name": "金融风控", "filename": "finance-risk.npz",
    "description": "使用已保存的三表图完成授信前股权关联核查：主体质检、第一大股东、控制链与累计经济权益。",
    "questions": [
        "请检查“合成示例青岛工程技术00044控股有限公司”的主体信息和股权数据。先确认机构 ID、统计日期、直接股东数量、机构股东数量和个人股东数量，并列出会影响后续判断的数据质量问题。不要先下控制结论。",
        "基于刚才确认的主体 ID，计算该企业的第一大股东。请区分直接持股比例、第一大股东候选、直接持股合计，以及是否存在单一股东严格超过 50%。请列出原始持股证据。",
        "请继续分析该企业的股权控制链。回答谁通过哪几级股权关系形成严格超过 50% 的控制，并列出中间控制主体和每一步的原始持股证据。随后以返回的股权控制根生成控制集团树，区分树边、附加控制边和普通关联边。",
        "请计算 `SYN-ENT-000001` 对 `SYN-ENT-000044` 的完整累计经济权益，并与主控制路径的比例分开显示。"
    ],
    "translations": {"en": {
        "name": "Financial risk",
        "description": "Review the saved three-table graph before lending: entity quality, largest shareholder, control chains, and cumulative economic interest.",
        "questions": [
            "Check the entity information and ownership data for 合成示例青岛工程技术00044控股有限公司. First confirm its institution ID, snapshot date, number of direct shareholders, institutional shareholders, and individual shareholders. List data-quality issues that could affect later findings. Do not draw a control conclusion yet.",
            "Using the entity ID just confirmed, calculate its largest shareholder. Distinguish direct ownership, largest-shareholder candidates, total direct ownership, and whether any single shareholder strictly exceeds 50%. Show the original ownership evidence.",
            "Continue with this company's equity control chain. Explain who controls it through which levels with strictly more than 50%, listing intermediate controllers and original ownership evidence for each step. Then build a control group tree from the returned equity-control root, distinguishing tree edges, additional control edges, and ordinary associations.",
            "Calculate the complete cumulative economic interest of `SYN-ENT-000001` in `SYN-ENT-000044`, displaying it separately from the percentage along the main control path."
        ]
    }}
}

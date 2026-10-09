"""Small original-record construction graph for finance tool boundaries."""


def ownership_document(prefix=""):
    nodes = []

    def row(table, name, data):
        nodes.append({"id": prefix + "row-" + name, "type": table,
                      "properties": {"data": {"STATC_DT": "2026-09-23", **data}}})

    for bid in ("R", "A", "T"):
        row("institution_main", bid, {"UUID": prefix + bid, "CHN_NM": prefix + bid + "公司", "ENTP_OPR_ST": "存续"})
    row("individual_info", "P", {"IDV_ID": prefix + "P", "IDV_NM": prefix + "个人"})
    for index, (holder, target, ratio, kind) in enumerate((
        ("R", "A", "60%", "0"), ("A", "T", "60%", "0"), ("P", "T", "20%", "1"),
    )):
        row("institution_shareholder_relationship", str(index), {
            "REL_SHRHLR_CUST_ID": prefix + holder, "UUID": prefix + target,
            "REL_SHRHLR_TP_CODE": kind, "REL_SHRHLR_HOLD_SHR_RTO": ratio,
        })
    return {"dataset_id": prefix + "owned-dataset", "revision": 7,
            "graph": {"directed": True, "nodes": nodes, "edges": []}}

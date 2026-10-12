"""Compute the molecular case from the owned graph's retained records."""
from collections import Counter, defaultdict
import networkx as nx
import math

from graph_engine.borylation_catalog import BORYLATION_STEPS

LIMITATIONS = [
    "当前结果来自公开实验记录及结构计算，未运行位点预测模型。",
    "产物表与条件表是不同任务的数据，仅按同一底物关联。不能说三个产物来自这96组实验，也不能说其它位点在这96组中未反应。",
    "单硼化转化率不等于某个位点的选择性或分离收率；未报道位置不等于已经验证不反应。",
    "本例BO-01的96条binary标签与单硼化转化率达到5%的判断一致，保留原作者标签。binary=0不等于零转化；8条非零记录中有4条未达5%。其它底物不能直接套用此口径。",
    "成功记录集中在环己烷仅是本次筛选的现象，不能推出环己烷是必要条件、其它溶剂都零转化、产物是同组混合物或某个位点活性低。",
    "只用记录中的配体简称或CAS号，不猜测取代位置、催化机理及产品条件对应关系。",
    "单次历史实验的条件差异需要独立重复验证。",
]

def analyze_borylation(graph, arguments: dict) -> dict:
    if not isinstance(arguments, dict) or set(arguments) != {"step"} or arguments.get("step") not in BORYLATION_STEPS:
        return {"success": False, "code": "INVALID_BORYLATION_ARGUMENTS", "message": "请选择有效的案例步骤。"}
    metadata = graph.graph_metadata or {}
    case = metadata.get("borylation_case")
    if not isinstance(case, dict) or case.get("case_id") != "borylation-bo01-v1":
        return {"success": False, "code": "BORYLATION_CASE_NOT_LOADED", "message": "请先打开分子硼化反应示例。"}
    try:
        atoms, records, products = case["atoms"], case["condition_records"], case["products"]
        if not (1 <= len(atoms) <= 256 and 1 <= len(records) <= 1000 and len(products) <= 100):
            raise ValueError("Unsupported case size")
        indices = {a["atom_index"] for a in atoms}
        if len(indices) != len(atoms) or indices != set(range(len(atoms))):
            raise ValueError("Invalid atom indices")
        if len({r["rxn_id"] for r in records}) != len(records):
            raise ValueError("Duplicate experiment identity")
        for r in records:
            value = r["conversion"]
            if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1 or r["binary"] not in {0, 1}:
                raise ValueError("Invalid experiment result")
        for p in products:
            if not p["atom_indices"] or not set(p["atom_indices"]).issubset(indices):
                raise ValueError("Invalid observed site")
        # Compute ring membership from the retained molecular bonds, not from
        # heterogeneous record edges or an LLM interpretation of SMILES.
        molecule = nx.Graph()
        molecule.add_nodes_from(indices)
        for bond in case["chemical_bonds"]:
            begin, end = bond["begin"], bond["end"]
            if begin not in indices or end not in indices or begin == end:
                raise ValueError("Invalid molecular bond")
            molecule.add_edge(begin, end)
        atom_by_index = {a["atom_index"]: a for a in atoms}
        rings = []
        for members in sorted((sorted(cycle) for cycle in nx.cycle_basis(molecule)), key=lambda ids: ids[0]):
            elements = dict(sorted(Counter(atom_by_index[i]["element"] for i in members).items()))
            aromatic = all(atom_by_index[i]["aromatic"] for i in members)
            rings.append({"ring_id": len(rings) + 1,
                "display_numbers": [atom_by_index[i]["display_number"] for i in members],
                "size": len(members), "elements": elements, "aromatic": aromatic,
                "kind": "aromatic_carbon_ring" if aromatic and set(elements) == {"C"} else "aromatic_heterocycle" if aromatic else "nonaromatic_ring"})
        candidates = [{**a, "ring_memberships": [ring for ring in rings if a["display_number"] in ring["display_numbers"]]}
            for a in atoms if a["element"] == "C" and a["aromatic"] and a["total_hydrogens"] > 0]
        groups = defaultdict(list)
        for a in candidates:
            groups[a["symmetry_class"]].append(a["display_number"])
        classes = [{"symmetry_class": key, "display_numbers": sorted(value)} for key, value in sorted(groups.items())]
        ligand_by_id = {x["id"]: x for x in case["ligands"]}
        solvent_by_id = {x["id"]: x for x in case["solvents"]}
        ordered = sorted(records, key=lambda r: (-r["conversion"], r["rxn_id"]))
        successes = [r for r in ordered if r["binary"] == 1]
        def public_record(r):
            return {**r, "conversion_percent": round(r["conversion"] * 100, 4),
                    "ligand_name": ligand_by_id[r["ligand_id"]]["name"],
                    "solvent_name": solvent_by_id[r["solvent_id"]]["name"]}
        cells = [public_record(r) for r in records]
        conditions = {
            "record_count": len(records), "success_count": len(successes), "negative_count": len(records)-len(successes),
            "success_definition": "本例96条原作者标签与5%阈值一致：binary=1达到5%，binary=0未达到5%，不等于零转化。",
            "nonzero_count": sum(r["conversion"] > 0 for r in records),
            "subthreshold_nonzero_count": sum(r["conversion"] > 0 and r["binary"] == 0 for r in records),
            "ligand_count": len({r["ligand_id"] for r in records}), "solvent_count": len({r["solvent_id"] for r in records}),
            "conversion_min_percent": min(c["conversion_percent"] for c in cells), "conversion_max_percent": max(c["conversion_percent"] for c in cells),
            "shared_conditions": case["shared_conditions"], "ligands": case["ligands"], "solvents": case["solvents"],
            "matrix": cells, "successful_conditions": [public_record(r) for r in successes],
            "top_conditions": [public_record(r) for r in ordered[:3]],
        }
        conditions["solvent_summary"] = [
            {"solvent_id": solvent["id"], "solvent_name": solvent["name"],
             "record_count": sum(r["solvent_id"] == solvent["id"] for r in records),
             "success_count": sum(r["solvent_id"] == solvent["id"] and r["binary"] == 1 for r in records),
             "nonzero_count": sum(r["solvent_id"] == solvent["id"] and r["conversion"] > 0 for r in records),
             "max_conversion_percent": round(max(r["conversion"] for r in records if r["solvent_id"] == solvent["id"]) * 100, 4)}
            for solvent in case["solvents"] if any(r["solvent_id"] == solvent["id"] for r in records)
        ]
        contrast = None
        if successes:
            best = successes[0]
            controls = [r for r in records if r["ligand_id"] == best["ligand_id"] and r["solvent_id"] != best["solvent_id"] and r["binary"] == 0]
            # Prefer THF for a readable recorded comparison, without inventing a condition.
            controls.sort(key=lambda r: (solvent_by_id[r["solvent_id"]]["cas"] != "109-99-9", r["conversion"], r["rxn_id"]))
            if controls:
                contrast = {"higher_record": public_record(best), "comparison_record": public_record(controls[0]),
                            "changed_field": "solvent", "interpretation": "同底物同配体的两条历史记录；需要重复实验确认差异。"}
        conditions["recorded_contrast"] = contrast
        step = arguments["step"]
        if step == "structure":
            result = {"molecule_id": case["molecule_id"], "smiles": case["smiles"], "heavy_atom_count": len(atoms),
                      "chemical_bond_count": len(case["chemical_bonds"]), "aromatic_ch_count": len(candidates),
                      "candidate_atoms": candidates, "rings": rings, "atom_numbering": "display_number从1开始，atom_index从0开始",
                      "visualization_node_names": [case["molecule_id"]] + [f'{case["molecule_id"]}:atom:{i}' for i in range(len(atoms))]}
        elif step == "symmetry":
            result = {"candidate_atom_count": len(candidates), "distinct_site_count": len(classes), "site_classes": classes}
        elif step == "products":
            atom_by_index = {a["atom_index"]: a for a in atoms}
            mapped_products = [{**p,
                "display_numbers": [atom_by_index[i]["display_number"] for i in p["atom_indices"]],
                "symmetry_classes": [atom_by_index[i]["symmetry_class"] for i in p["atom_indices"]]}
                for p in products]
            result = {"product_count": len(products), "observed_display_numbers": sorted({i+1 for p in products for i in p["atom_indices"]}),
                      "products": mapped_products, "condition_to_product_link": "unavailable"}
        elif step == "conditions":
            result = conditions
        else:
            planned = [{"experiment_id": f"P{i+1}", "purpose": "优先复核已有较高转化率条件", "reference": public_record(r),
                        "replicates": 2, "expected_outcome": "未知，需实验测定", "readouts": ["LC–MS转化率", "分离产物结构", "位点及区域异构体比例"]} for i, r in enumerate(ordered[:3])]
            if contrast:
                planned.append({"experiment_id": "P4", "purpose": "同配体溶剂对照", "reference": contrast["comparison_record"],
                                "replicates": 2, "expected_outcome": "未知，需实验测定", "readouts": ["LC–MS转化率", "分离产物结构", "位点及区域异构体比例"]})
            result = {"plan_type": "基于历史记录的复核草案", "condition_group_count": len(planned),
                      "recommended_total_runs": sum(p["replicates"] for p in planned), "experiments": planned,
                      "positions_to_identify": sorted({i+1 for p in products for i in p["atom_indices"]}),
                      "hypothesis": "较高转化率条件值得优先复核；溶剂差异及具体产物位置需新实验确认。"}
        structure_summary = {"rings": rings, "candidate_site_numbers": [a["display_number"] for a in candidates],
            "distinct_site_count": len(classes), "site_classes": classes,
            "per_ring_sites": [{"ring_id": ring["ring_id"],
                "candidate_site_numbers": [a["display_number"] for a in candidates if a["display_number"] in ring["display_numbers"]],
                "distinct_site_count": len({a["symmetry_class"] for a in candidates if a["display_number"] in ring["display_numbers"]})}
                for ring in rings]}
        return {"success": True, "step": step, "result": result, "structure_summary": structure_summary,
                "provenance": case["provenance"], "limitations": LIMITATIONS, "prediction_status": "not_run"}
    except (KeyError, TypeError, ValueError) as exc:
        return {"success": False, "code": "INVALID_BORYLATION_CASE_DATA", "message": "案例记录不完整或格式不符，请重新加载预置案例。"}

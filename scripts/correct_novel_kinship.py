"""Apply reviewed corrections to the bundled novel, preserving every changed assertion."""

import copy, hashlib, json, re, shutil
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from graph_data_service.graph_data import GraphData
from graph_data_service.construction import to_graph_data

ROOT = Path(__file__).resolve().parents[1]
R = ROOT / "graph_engine/resources/tianlongbabu"
asset = R / "tianlongbabu-characters.npz"
BASE = "f909d76443f3fb7b6983f2a538acb8d5c1b23ed7aa510eb01e38f419df7c9b24"


def main():
    oldsha = hashlib.sha256(asset.read_bytes()).hexdigest()
    if oldsha != BASE:
        raise SystemExit(
            "Expected reviewed revision 2; refusing to overwrite another graph"
        )
    graph = copy.deepcopy(
        GraphData.load(str(asset)).graph_metadata["construction_graph"]
    )
    names = {n["id"]: n["properties"]["data"]["name"] for n in graph["nodes"]}
    ids = {v: k for k, v in names.items()}
    changes = []
    src = "用户提供《天龙八部.txt》原文（GB18030）；关系按故事揭示后的身份核验，分块中的角色误认不作为生物亲缘。"
    external = "人工补录来源：https://zh.wikipedia.org/wiki/天龙八部角色列表 ; https://jinyongx.com/data/1492.html"

    def revise(e, a, b, label, kind, role="", evidence=src):
        before = copy.deepcopy(e)
        e.update(source=ids[a], target=ids[b], type=label)
        props = e.setdefault("properties", {})
        props["relationship"] = {
            "schema": "kinship.v1",
            "kind": kind,
            "role": role,
            "review_status": "verified",
            "evidence": evidence,
            "direction": "source_role_of_target",
        }
        props["correction"] = {
            "original_source": before["source"],
            "original_target": before["target"],
            "original_type": before["type"],
            "audit": "kinship-corrections.json",
        }
        changes.append(
            {
                "before": before,
                "after": copy.deepcopy(e),
                "source_name": a,
                "target_name": b,
            }
        )

    parent_re = re.compile("父|母|儿子|女儿|^亲属$|被抚养")
    # Fixed facts, never infer a parent's identity from extraction direction.
    parents = [
        ("段延庆", "段誉", "生父", "father"),
        ("刀白凤", "段誉", "生母", "mother"),
        ("段夫人", "段誉", "生母", "mother"),
        ("玉虚散人", "段誉", "生母", "mother"),
        ("段正淳", "王语嫣", "生父", "father"),
        ("王夫人", "王语嫣", "生母", "mother"),
        ("段正淳", "木婉清", "生父", "father"),
        ("秦红棉", "木婉清", "生母", "mother"),
        ("段正淳", "阿朱", "生父", "father"),
        ("阮星竹", "阿朱", "生母", "mother"),
        ("段正淳", "阿紫", "生父", "father"),
        ("阮星竹", "阿紫", "生母", "mother"),
        ("段正淳", "钟灵", "生父", "father"),
        ("甘宝宝", "钟灵", "生母", "mother"),
        ("钟夫人", "钟灵", "生母", "mother"),
        ("玄慈", "虚竹", "生父", "father"),
        ("叶二娘", "虚竹", "生母", "mother"),
        ("萧远山", "萧峰", "生父", "father"),
        ("慕容博", "慕容复", "生父", "father"),
        ("高智升", "高升泰", "生父", "father"),
        ("游驹", "游坦之", "生父", "father"),
        ("司马卫", "司马林", "生父", "father"),
        ("包不同", "包不靓", "生父", "father"),
        ("单正", "单伯山", "生父", "father"),
        ("单正", "单仲山", "生父", "father"),
        ("单正", "单叔山", "生父", "father"),
        ("单正", "单季山", "生父", "father"),
        ("单正", "单小山", "生父", "father"),
        ("左子穆", "左子穆的幼儿", "生父", "father"),
        ("左子穆", "左山山", "生父", "father"),
        ("耶律重元", "耶律涅鲁古", "生父", "father"),
        ("和哩布", "完颜阿骨打", "生父", "father"),
        ("萧太后", "耶律洪基", "生母", "mother"),
        ("无崖子", "王夫人", "生父", "father"),
        ("李秋水", "王夫人", "生母", "mother"),
    ]
    # These aliases remain separate graph nodes. Only reviewed semantic identity is shared.
    alias_names = {"段夫人": "刀白凤", "玉虚散人": "刀白凤", "钟夫人": "甘宝宝"}
    for n in graph["nodes"]:
        if names[n["id"]] in alias_names:
            n["properties"]["kinship_identity"] = ids[alias_names[names[n["id"]]]]
    for e in graph["edges"]:
        a, b = names[e["source"]], names[e["target"]]
        label = e["type"]
        pair = {a, b}
        done = False
        for p, c, title, role in parents:
            if pair == {p, c} and parent_re.search(label):
                revise(
                    e,
                    p,
                    c,
                    title,
                    "biological_parent",
                    role,
                    external if p in {"无崖子", "李秋水"} else src,
                )
                done = True
                break
        if done:
            continue
        if pair == {"段正淳", "段誉"} and parent_re.search(label):
            revise(
                e,
                "段正淳",
                "段誉",
                "名义父亲",
                "nominal_parent",
                "father",
                src
                + " 原文第8980—8984行及身世揭示段落：生父为段延庆，段正淳是名义父亲。",
            )
        elif pair == {"钟万仇", "钟灵"} and parent_re.search(label):
            revise(e, "钟万仇", "钟灵", "名义父亲", "nominal_parent", "father")
        elif pair == {"乔三槐", "乔峰"} and parent_re.search(label):
            revise(e, "乔三槐", "乔峰", "养父", "adoptive_parent", "father")
        elif (
            "段誉" in pair
            and pair & {"王语嫣", "木婉清", "钟灵", "阿朱", "阿紫"}
            and re.search("兄|妹|亲属", label)
        ):
            other = next(n for n in pair if n != "段誉")
            revise(
                e,
                "段誉",
                other,
                "曾误认的同父兄妹",
                "historical_belief",
                "siblings",
                src + " 该误认以段正淳是段誉生父为前提；身世揭示后不能再当作同父兄妹。",
            )
        elif pair == {"王夫人", "慕容复"} and label in {"姨母", "外甥"}:
            revise(
                e,
                "王夫人",
                "慕容复",
                "舅母",
                "affinal_relative",
                "uncles_wife",
                "原TXT第8899、8992、9062行称“舅母/舅妈”，不是母亲的姐妹；舅母的情人不能推出为舅父。",
            )
        elif pair == {"慕容复", "王语嫣"} and re.search("表|亲属|至亲", label):
            revise(
                e,
                a,
                b,
                "名义表亲",
                "social_relative",
                "cousin",
                src
                + " 称谓来自王家名义家族关系；王语嫣生父为段正淳，不能把这个称谓直接视为生物亲缘边。",
            )
        elif pair == {"段延庆", "刀白凤"} and label in {"夫妻", "配偶", "丈夫", "妻子"}:
            revise(
                e,
                "段延庆",
                "刀白凤",
                "一夜情",
                "romantic",
                "",
                src + " 二人不是夫妻；原文身世揭示段落。",
            )
        elif pair == {"段正淳", "刀白凤"} and label in {"夫妻", "配偶", "丈夫", "妻子"}:
            revise(e, "段正淳", "刀白凤", "丈夫", "spouse", "husband")
        elif pair == {"虚竹", "梦姑"} and label == "丈夫":
            revise(e, "虚竹", "梦姑", "丈夫", "spouse", "husband", external)
        elif pair == {"李秋水", "梦姑"} and label == "祖母":
            revise(
                e,
                "李秋水",
                "梦姑",
                "祖母",
                "biological_grandparent",
                "paternal_grandmother",
                external,
            )
        elif pair == {"段誉", "段正明"} or pair == {"段誉", "保定帝"}:
            if re.search("伯|侄", label):
                other = next(n for n in pair if n != "段誉")
                revise(e, other, "段誉", "名义伯父", "social_relative", "uncle")
        elif pair == {"段延庆", "慕容复"} and label in {"义父", "欲拜义父"}:
            revise(
                e,
                "慕容复",
                "段延庆",
                "请求拜为义父",
                "proposed_relation",
                "",
                src + " 意图/请求不能当作已确认血缘。",
            )
        else:
            teachers = [
                ("无崖子", "虚竹"),
                ("无崖子", "苏星河"),
                ("无崖子", "丁春秋"),
                ("慧轮", "虚竹"),
                ("秦红棉", "木婉清"),
                ("幽谷客", "木婉清"),
                ("段誉", "南海鳄神"),
            ]
            for teacher, student in teachers:
                if pair == {teacher, student} and label in {
                    "师父",
                    "师徒",
                    "师",
                    "师承",
                    "师从",
                    "徒弟",
                }:
                    revise(e, teacher, student, "师父", "mentor", "teacher")
                    break
            if pair == {"虚竹", "苏星河"} and label == "师徒":
                revise(
                    e,
                    "虚竹",
                    "苏星河",
                    "师弟",
                    "fellow_disciple",
                    "junior",
                    src + " 两人均为无崖子弟子，虚竹不是苏星河的弟子。",
                )
            if pair == {"虚竹", "天山童姥"} or pair == {"虚竹", "童姥"}:
                if label in {"师徒", "师承"}:
                    teacher = next(n for n in pair if n != "虚竹")
                    revise(
                        e,
                        teacher,
                        "虚竹",
                        "传授武功",
                        "instruction",
                        "teacher",
                        src + " 指点/传功与正式拜师分开记录。",
                    )
            if (
                a == "虚竹"
                and b
                in {
                    "康广陵",
                    "吴领军",
                    "苟读",
                    "薛神医",
                    "薛慕华",
                    "李傀儡",
                    "石清露",
                    "范百龄",
                    "冯阿三",
                }
                and label == "师侄"
            ):
                revise(
                    e,
                    "虚竹",
                    b,
                    "师叔",
                    "school_relative",
                    "senior",
                    src + " 虚竹是苏星河师弟，苏星河弟子为其师侄。",
                )
    # Collapse identical reviewed assertions, retaining all original IDs and extraction evidence in the audit.
    combined = {}
    out = []
    for e in graph["edges"]:
        sem = e.get("properties", {}).get("relationship")
        key = (e["source"], e["target"], e["type"]) if sem else ("untouched", e["id"])
        if key in combined:
            keep = combined[key]
            keep["properties"].setdefault("merged_assertion_ids", []).append(e["id"])
        else:
            combined[key] = e
            out.append(e)
    graph["edges"] = out
    data = to_graph_data(
        {"dataset_id": "ds_tianlongbabu_v1", "revision": 3, "graph": graph}
    )
    data.save(str(asset))
    with np.load(asset, allow_pickle=True) as content:
        arrays = {k: content[k] for k in content.files}
    np.savez_compressed(asset, **arrays)
    now = datetime.now(timezone.utc).isoformat()
    audit = {
        "schema": "kinship-corrections.v1",
        "base_sha256": BASE,
        "revision": 3,
        "updated_at": now,
        "changed_assertions": len(changes),
        "removed_duplicate_assertions": 795 - len(out),
        "edge_count": len(out),
        "changes": changes,
    }
    (R / "kinship-corrections.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = json.loads((R / "manifest.json").read_text())
    manifest["sha256"] = hashlib.sha256(asset.read_bytes()).hexdigest()
    for k in (
        "edge_count",
        "edge_entry_count",
        "unique_edge_count",
        "num_unique_directed_edges",
        "num_unique_undirected_edges",
    ):
        manifest["dataset"][k] = len(out)
    manifest["dataset"]["updated_at"] = now
    manifest["construction"]["kinship_corrections"] = {
        k: v for k, v in audit.items() if k != "changes"
    }
    (R / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in audit.items() if k != "changes"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()

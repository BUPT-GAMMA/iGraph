import copy
import pytest
from graph_data_service.construction import to_graph_data
from graph_data_service.kinship import inspect_kinship


def sample(edges):
    names = sorted({x for a, b, k, r in edges for x in (a, b)})
    graph = {
        "directed": True,
        "nodes": [
            {"id": n, "type": "person", "properties": {"data": {"name": n}}}
            for n in names
        ],
        "edges": [
            {
                "id": f"e{i}",
                "source": a,
                "target": b,
                "type": k,
                "properties": {
                    "relationship": {"kind": k, "role": r, "review_status": "verified"}
                },
            }
            for i, (a, b, k, r) in enumerate(edges)
        ],
    }
    return to_graph_data({"dataset_id": "test", "revision": 1, "graph": graph})


def test_nominal_romantic_and_mistaken_edges_never_prove_blood():
    gd = sample(
        [
            ("A", "X", "nominal_parent", "father"),
            ("B", "X", "biological_parent", "father"),
            ("A", "Y", "biological_parent", "father"),
            ("X", "Y", "historical_belief", "siblings"),
            ("Y", "Z", "social_relative", "cousin"),
            ("A", "M", "romantic", ""),
            ("M", "Z", "affinal_relative", "aunt"),
        ]
    )
    r = inspect_kinship(gd, ["X", "Y", "Z"])
    assert r["pair_checks"][0]["same_biological_father"] is False
    assert r["pair_checks"][0]["common_ancestors"] == []
    assert r["biological_ancestry"]["Z"]["ancestors"] == []
    assert r["pair_checks"][1]["same_biological_father"] == "not_established"


def test_mentor_and_spouse_do_not_inherit_biological_ancestors():
    gd = sample(
        [
            ("G", "M", "biological_parent", "mother"),
            ("M", "A", "biological_parent", "mother"),
            ("G", "B", "biological_grandparent", "paternal_grandmother"),
            ("X", "B", "spouse", "husband"),
            ("M", "X", "mentor", "teacher"),
        ]
    )
    r = inspect_kinship(gd, ["A", "B", "X"])
    assert r["pair_checks"][0]["common_ancestors"][0]["generations"] == [2, 2]
    assert r["biological_ancestry"]["X"]["ancestors"] == []
    assert r["pair_checks"][1]["common_ancestors"] == []


def test_conflict_and_cycle_are_reported():
    gd = sample(
        [
            ("A", "X", "biological_parent", "father"),
            ("B", "X", "biological_parent", "father"),
            ("X", "A", "biological_parent", "father"),
        ]
    )
    r = inspect_kinship(gd, ["X", "A"])
    assert r["status"] == "CONFLICT"
    assert r["pair_checks"][0]["same_biological_father"] == "not_established"
    assert any(e.get("code") == "ANCESTRY_CYCLE" for e in r["conflicts"])


def test_unreviewed_edges_and_other_graph_are_not_reused():
    gd = sample([("A", "B", "biological_parent", "father")])
    gd.graph_metadata["construction_graph"]["edges"][0]["properties"]["relationship"][
        "review_status"
    ] = "unreviewed"
    assert inspect_kinship(gd, ["B"])["facts"] == []
    from graph_data_service.graph_data import GraphData
    import numpy as np

    gd = GraphData(num_nodes=0, edge_index=np.empty((2, 0), dtype=np.int64))
    assert inspect_kinship(gd)["code"] == "RELATIONSHIP_EVIDENCE_UNAVAILABLE"


@pytest.mark.parametrize("bad", [["X"] * 21, "X", [None], [""]])
def test_invalid_focus(bad):
    assert (
        inspect_kinship(sample([("A", "B", "mentor", "teacher")]), bad)["code"]
        == "INVALID_ARGUMENT"
    )


def test_catalog_and_resource_contract():
    from graph_data_service.tool_catalog import get_all_public_tools
    from graph_engine.tool_execution import execution_policy

    assert any(t["name"] == "inspect_kinship" for t in get_all_public_tools())
    policy = execution_policy("inspect_kinship", {})
    assert policy.read_only and policy.reads == frozenset({"graph"})


def test_preset_truth_and_two_distinct_bridges():
    from graph_data_service.graph_data import GraphData
    from graph_engine.novel_preset import RESOURCES, FILENAME

    gd = GraphData.load(str(RESOURCES / FILENAME))
    r = inspect_kinship(gd, ["王语嫣", "段誉", "慕容复", "虚竹", "梦姑"])
    assert r["status"] == "VERIFIED_ASSERTIONS", r["conflicts"]
    p = r["pair_checks"][0]
    assert p["persons"] == ["王语嫣", "段誉"] and p["same_biological_father"] is False
    assert p["biological_fathers"] == [["段正淳"], ["段延庆"]]
    facts = {
        (e["source_name"], e["target_name"], e["relation"], e["kind"])
        for e in r["facts"]
    }
    assert ("王夫人", "慕容复", "舅母", "affinal_relative") in facts
    assert ("段正淳", "段誉", "名义父亲", "nominal_parent") in facts
    assert ("无崖子", "虚竹", "师父", "mentor") in facts
    assert ("虚竹", "梦姑", "丈夫", "spouse") in facts
    relation = next(p for p in r["pair_checks"] if p["persons"] == ["王语嫣", "梦姑"])
    assert any(
        a["name"] == "李秋水" and a["generations"] == [2, 2]
        for a in relation["common_ancestors"]
    )
    assert (
        next(p for p in r["pair_checks"] if p["persons"] == ["段誉", "慕容复"])[
            "common_ancestors"
        ]
        == []
    )
    g = gd.graph_metadata["construction_graph"]
    names = {n["id"]: n["properties"]["data"]["name"] for n in g["nodes"]}
    assert not any(
        {names[e["source"]], names[e["target"]]} == {"王夫人", "慕容复"}
        and e["type"] == "姨母"
        for e in g["edges"]
    )
    assert not any(
        {names[e["source"]], names[e["target"]]} == {"段正淳", "段誉"}
        and e["type"] in {"父亲", "父子", "生父", "儿子"}
        for e in g["edges"]
    )

def test_grandparent_titles_follow_the_parent_branch():
    gd=sample([("G","M","biological_parent","mother"),("M","A","biological_parent","mother"),
               ("G","B","biological_grandparent","paternal_grandmother")])
    r=inspect_kinship(gd,["A","B"])
    assert r["pair_checks"][0]["common_ancestors"][0]["relationships_to_persons"]==["外祖母","祖母"]
    assert r["coverage"]["age_order"]=="not_established"


def test_custom_properties_cannot_replace_canonical_edge_endpoints():
    gd=sample([("A","B","biological_parent","father"),("C","D","mentor","teacher")])
    graph=gd.graph_metadata["construction_graph"]
    graph["edges"][0]["properties"]["relationship"].update(source="C",source_name="invented",edge_id="fake")
    graph["nodes"][0]["properties"]["kinship_identity"]={"invalid":"identity"}
    r=inspect_kinship(gd,["B"])
    assert r["biological_ancestry"]["B"]["ancestors"][0]["name"]=="A"
    assert r["facts"][0]["edge_id"]=="e0"

"""New questions use molecular evidence without inventing property measurements."""
import copy
from pathlib import Path
from graph_data_service.borylation import analyze_borylation
from graph_data_service.graph_data import GraphData
from backend.borylation_case import BORYLATION_CASE
from graph_engine.borylation_catalog import BORYLATION_STEPS

def graph():
    return GraphData.load(str(Path(__file__).resolve().parents[1] / "graph_engine/resources/borylation/borylation-story.npz"))

def test_properties_have_method_and_no_invented_measurements():
    response = analyze_borylation(graph(), {"step": "properties"})
    assert response["success"]
    r = response["result"]
    assert r["descriptors"]["formula"] == "C15H9ClFN3OS"
    assert 333 < r["descriptors"]["molecular_weight_g_mol"] < 334
    assert r["method"]["library"] == "RDKit"
    assert r["method"]["tpsa_includes_sulfur_phosphorus"] is True
    assert r["solubility"]["measurements"] == r["solubility"]["predictions"] == []
    assert len(r["solubility"]["recorded_reaction_solvents"]) == 8
    assert r["toxicity"]["evidence_status"] == "unknown"
    assert r["toxicity"]["measured_endpoints"] == r["toxicity"]["predictions"] == []

def test_modified_structure_does_not_reuse_cached_descriptors():
    g = graph()
    g = GraphData(graph_metadata=copy.deepcopy(g.graph_metadata))
    g.graph_metadata["borylation_case"]["atoms"][0]["formal_charge"] = -1
    assert analyze_borylation(g, {"step":"properties"})["success"] is False

def test_ring_count_is_separate_from_ring_type_count():
    r = analyze_borylation(graph(), {"step":"rings"})["result"]
    assert (r["ring_count"], r["ring_type_count"]) == (3, 2)
    assert sorted(t["count"] for t in r["ring_types"]) == [1, 2]
    assert r["fused_ring_pairs"] == []
    assert r["rings"][1]["elements"] == {"C":2,"N":2,"S":1}

def test_four_questions_have_matching_localized_steps():
    zh, en = BORYLATION_CASE, BORYLATION_CASE["translations"]["en"]
    for lang in (zh, en):
        assert len(lang["questions"]) == 4
        assert lang["questions"] == [s["question"] for s in lang["story_steps"]]
        assert [s["analysis_step"] for s in lang["story_steps"]] == ["properties","rings","structure","symmetry"]
        assert all(s["analysis_step"] in BORYLATION_STEPS for s in lang["story_steps"])


def test_candidate_substituent_environment_follows_actual_bonds():
    result=analyze_borylation(graph(), {"step":"structure"})["result"]
    atoms={a["display_number"]:a for a in result["candidate_atoms"]}
    assert [n["display_number"] for n in atoms[19]["bonded_neighbors"]]==[18,20]
    assert atoms[19]["adjacent_ring_substituents"]==[]
    assert atoms[18]["adjacent_ring_substituents"]==[{"ring_atom":16,"attached_atom":17,"attached_element":"F"}]
    assert atoms[20]["adjacent_ring_substituents"]==[{"ring_atom":21,"attached_atom":22,"attached_element":"Cl"}]

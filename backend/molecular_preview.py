"""Read-only construction preview for the verified bundled BO-01 snapshot."""
import json
from pathlib import Path

from backend.user_cases import background_for_dataset


def molecular_structure_for_dataset(dataset):
    # Use the same exact-snapshot check as analysis background injection. Names
    # and user-editable case tags alone cannot authorize the bundled drawing.
    background = background_for_dataset(dataset)
    if background is None or background["case_id"] != "borylation":
        return None
    path = Path(__file__).resolve().parents[1] / "graph_engine/resources/borylation/graphs/borylation-story.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    case = snapshot["graph_metadata"]["borylation_case"]
    atoms = case["atoms"]
    candidates = [atom for atom in atoms if atom["element"] == "C" and atom["aromatic"] and atom["total_hydrogens"] > 0]
    return {"success": True, "step": "structure", "dataset_id": dataset.dataset_id,
        "result": {"molecule_id": case["molecule_id"], "smiles": case["smiles"],
            "heavy_atom_count": len(atoms), "chemical_bond_count": len(case["chemical_bonds"]),
            "aromatic_ch_count": len(candidates), "candidate_atoms": candidates,
            "visualization_node_names": list(snapshot["id2node"].values())}}

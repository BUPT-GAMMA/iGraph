"""Run offline with requirements-data.txt; production needs no RDKit installation."""
import json
from pathlib import Path
from rdkit import Chem, rdBase
from rdkit.Chem import Descriptors, Lipinski, rdMolDescriptors
from graph_engine.borylation_chemistry import structure_fingerprint

root = Path(__file__).resolve().parents[1]
case = json.loads((root / "processed/system_case.json").read_text())
mol = Chem.MolFromSmiles(case["smiles"])
assert mol is not None and mol.GetNumAtoms() == len(case["atoms"])
assert mol.GetNumBonds() == len(case["chemical_bonds"])
assert {(min(b.GetBeginAtomIdx(), b.GetEndAtomIdx()), max(b.GetBeginAtomIdx(), b.GetEndAtomIdx()), str(b.GetBondType())) for b in mol.GetBonds()} == {(min(b["begin"], b["end"]), max(b["begin"], b["end"]), b["bond_type"]) for b in case["chemical_bonds"]}
ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
for a in case["atoms"]:
    for b in case["atoms"]:
        assert (a["symmetry_class"] == b["symmetry_class"]) == (ranks[a["atom_index"]] == ranks[b["atom_index"]])
for a in case["atoms"]:
    atom = mol.GetAtomWithIdx(a["atom_index"])
    assert (atom.GetSymbol(), atom.GetTotalNumHs(), atom.GetIsAromatic(), atom.GetFormalCharge()) == (a["element"], a["total_hydrogens"], a["aromatic"], a["formal_charge"])
profile = {
    "structure_sha256": structure_fingerprint(case), "molecule_id": case["molecule_id"], "smiles": case["smiles"],
    "method": {"library": "RDKit", "version": rdBase.rdkitVersion, "evidence_type": "computed_from_structure", "tpsa_includes_sulfur_phosphorus": True},
    "descriptors": {"formula": rdMolDescriptors.CalcMolFormula(mol), "molecular_weight_g_mol": round(Descriptors.MolWt(mol), 3),
        "clogp": round(Descriptors.MolLogP(mol), 3), "tpsa_angstrom2": round(rdMolDescriptors.CalcTPSA(mol, includeSandP=True), 2),
        "h_bond_donors": Lipinski.NumHDonors(mol), "h_bond_acceptors": Lipinski.NumHAcceptors(mol), "formal_charge": Chem.GetFormalCharge(mol)},
    "descriptor_notes": {"clogp": "Calculated octanol/water partition descriptor, not a solvent-specific solubility or logS prediction.",
        "tpsa": "Topological polar surface area; includes sulfur and phosphorus contributions."},
    "sources": ["https://www.rdkit.org/docs/GettingStartedInPython.html", "https://www.rdkit.org/docs/RDKit_Book.html"]
}
(root / "processed/chemistry_profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
print(json.dumps(profile, ensure_ascii=False, indent=2))

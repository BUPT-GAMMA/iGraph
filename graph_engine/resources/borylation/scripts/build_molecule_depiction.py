"""Draw the exact BO-01 substrate and retain RDKit's atom-to-screen mapping."""
import argparse
import hashlib
import json
from pathlib import Path

from rdkit import Chem, rdBase
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D


def build(repo_root: Path):
    bundle = Path(__file__).resolve().parents[1]
    case = json.loads((bundle / 'processed/system_case.json').read_text())
    mol = Chem.MolFromSmiles(case['smiles'])
    assert mol is not None and mol.GetNumAtoms() == len(case['atoms'])
    rdDepictor.Compute2DCoords(mol)
    for atom, source in zip(mol.GetAtoms(), case['atoms']):
        assert atom.GetIdx() == source['atom_index']
        assert atom.GetSymbol() == source['element']
        assert atom.GetIsAromatic() == source['aromatic']
        assert atom.GetTotalNumHs() == source['total_hydrogens']
        assert source['display_number'] == atom.GetIdx() + 1
        atom.SetProp('atomNote', str(source['display_number']))
    bonds = [{'begin': b.GetBeginAtomIdx(), 'end': b.GetEndAtomIdx(),
              'bond_type': str(b.GetBondType())} for b in mol.GetBonds()]
    assert bonds == case['chemical_bonds']
    width, height = 1000, 560
    drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
    options = drawer.drawOptions()
    options.bondLineWidth = 2.5
    options.fixedFontSize = 24
    options.annotationFontScale = .8
    options.padding = .12
    drawer.DrawMolecule(mol)
    coords = [drawer.GetDrawCoords(i) for i in range(mol.GetNumAtoms())]
    rings = mol.GetRingInfo().AtomRings()
    drawer.FinishDrawing()
    svg = drawer.GetDrawingText().encode('utf-8')
    depiction = {
        'version': 'borylation.depiction.v1', 'case_id': case['case_id'],
        'molecule_id': case['molecule_id'], 'input_smiles': case['smiles'],
        'width': width, 'height': height, 'viewBox': [0, 0, width, height],
        'svg_url': '/examples/borylation/BO-01-molecule.svg',
        'svg_sha256': hashlib.sha256(svg).hexdigest(),
        'rdkit_version': rdBase.rdkitVersion,
        'atoms': [{**atom, 'node_id': f"BO-01:atom:{atom['atom_index']}",
                   'x': round(coords[i].x, 6), 'y': round(coords[i].y, 6),
                   'ring_sizes': sorted({len(ring) for ring in rings if i in ring})}
                  for i, atom in enumerate(case['atoms'])],
        'bonds': bonds,
        'candidate_node_ids': [f"BO-01:atom:{a['atom_index']}" for a in case['atoms']
                               if a['element'] == 'C' and a['aromatic'] and a['total_hydrogens'] > 0],
    }
    target = repo_root / 'frontend/public/examples/borylation'
    target.mkdir(parents=True, exist_ok=True)
    for folder in [target, bundle / 'images']:
        (folder / 'BO-01-molecule.svg').write_bytes(svg)
        (folder / 'BO-01-depiction.json').write_text(json.dumps(depiction, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'atoms': len(depiction['atoms']), 'chemical_bonds': len(bonds),
                      'candidate_count': len(depiction['candidate_node_ids']),
                      'svg_sha256': depiction['svg_sha256']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    build(parser.parse_args().repo_root.resolve())

"""Inspect source pairs without treating derived sites as model predictions."""
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from rdkit import Chem

import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'raw')
parser.add_argument('--out-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'validation')
args = parser.parse_args()
SOURCE, OUT = args.data_dir, args.out_dir
OUT.mkdir(parents=True, exist_ok=True)
manifest_path = Path(__file__).resolve().parents[1] / 'source_manifest.json'
if manifest_path.exists():
    for entry in json.loads(manifest_path.read_text())['entries']:
        actual = hashlib.sha256((SOURCE / entry['file']).read_bytes()).hexdigest()
        if actual != entry['sha256']:
            raise ValueError('Source file SHA256 mismatch: ' + entry['file'])

def canonical(mol):
    return Chem.MolToSmiles(mol, isomericSmiles=True)

def extract_sites(substrate, product):
    """Recover exact substrate after removing a boron branch at a carbon."""
    if product.GetNumAtoms() == 0:
        return {'status': 'empty_product', 'atom_indices': [], 'matches': 0}
    if canonical(substrate) == canonical(product):
        return {'status': 'unchanged_product', 'atom_indices': [], 'matches': 0}
    sites = set()
    routes = 0
    for bond in product.GetBonds():
        atoms = (bond.GetBeginAtom(), bond.GetEndAtom())
        if sorted(a.GetAtomicNum() for a in atoms) != [5, 6]:
            continue
        carbon = next(a.GetIdx() for a in atoms if a.GetAtomicNum() == 6)
        boron = next(a.GetIdx() for a in atoms if a.GetAtomicNum() == 5)
        component = {carbon}
        todo = [carbon]
        while todo:
            idx = todo.pop()
            for neigh in product.GetAtomWithIdx(idx).GetNeighbors():
                other = neigh.GetIdx()
                if (idx == carbon and other == boron) or (idx == boron and other == carbon):
                    continue
                if other not in component:
                    component.add(other)
                    todo.append(other)
        if boron in component:
            continue
        keep = sorted(component)
        if len(keep) != substrate.GetNumAtoms():
            continue
        edited = Chem.RWMol(product)
        for idx in reversed(range(product.GetNumAtoms())):
            if idx not in component:
                edited.RemoveAtom(idx)
        scaffold = edited.GetMol()
        try:
            Chem.SanitizeMol(scaffold)
        except Exception:
            continue
        if canonical(scaffold) != canonical(substrate):
            continue
        recovered_carbon = keep.index(carbon)
        matches = substrate.GetSubstructMatches(scaffold, uniquify=False, useChirality=True, maxMatches=10000)
        for match in matches:
            if any((scaffold.GetAtomWithIdx(j).GetAtomicNum(), scaffold.GetAtomWithIdx(j).GetFormalCharge(), scaffold.GetAtomWithIdx(j).GetTotalNumHs(), scaffold.GetAtomWithIdx(j).GetIsAromatic(), scaffold.GetAtomWithIdx(j).GetIsotope()) != (substrate.GetAtomWithIdx(k).GetAtomicNum(), substrate.GetAtomWithIdx(k).GetFormalCharge(), substrate.GetAtomWithIdx(k).GetTotalNumHs(), substrate.GetAtomWithIdx(k).GetIsAromatic(), substrate.GetAtomWithIdx(k).GetIsotope()) for j, k in enumerate(match)):
                continue
            site = match[recovered_carbon]
            atom = substrate.GetAtomWithIdx(site)
            if atom.GetAtomicNum() == 6 and atom.GetTotalNumHs() > 0:
                sites.add(site)
        routes += len(matches)
    return {'status': 'mapped' if sites else 'unresolved', 'atom_indices': sorted(sites), 'matches': routes}

with (SOURCE / 'regioselectivity.csv').open(newline='') as handle:
    pairs = list(csv.DictReader(handle, delimiter='\t'))

records = []
groups = {}
for line, row in enumerate(pairs, start=2):
    sub, prod = Chem.MolFromSmiles(row['SMILES']), Chem.MolFromSmiles(row['product_smiles'])
    record = {'source_line': line, **row}
    if sub is None or prod is None:
        record.update(status='invalid_smiles', atom_indices=[])
    else:
        record.update(extract_sites(sub, prod))
        record['canonical_smiles'] = canonical(sub)
        record['symmetry_classes'] = sorted({list(Chem.CanonicalRankAtoms(sub, breakTies=False))[i] for i in record['atom_indices']})
        group = groups.setdefault(row['SMILES'], {'SMILES': row['SMILES'], 'canonical_smiles': canonical(sub), 'heavy_atoms': sub.GetNumAtoms(), 'candidate_carbons': [a.GetIdx() for a in sub.GetAtoms() if a.GetAtomicNum() == 6 and a.GetTotalNumHs() > 0], 'aromatic_ch': [a.GetIdx() for a in sub.GetAtoms() if a.GetAtomicNum() == 6 and a.GetIsAromatic() and a.GetTotalNumHs() > 0], 'rings': sub.GetRingInfo().NumRings(), 'records': []})
        group['records'].append(record)
    records.append(record)

condition_sets = {}
conditions = []
for name, sep in [('pre_AL_binary_and_yield.csv', ';'), ('AL_1_binary_and_yield.csv', '\t'), ('AL_2_binary_and_yield.csv', '\t'), ('AL_3_binary_and_yield.csv', '\t')]:
    with (SOURCE / name).open(newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter=sep))
    condition_sets[name] = len(rows)
    for line, row in enumerate(rows, 2):
        row['_file'], row['_line'] = name, line
        parsed = Chem.MolFromSmiles(row['startingmat_1_smiles'])
        row['_canonical'] = canonical(parsed) if parsed is not None else None
        conditions.append(row)

by_canonical = defaultdict(list)
for row in conditions:
    by_canonical[row['_canonical']].append(row)
for group in groups.values():
    group['condition_row_count'] = len(by_canonical.get(group['canonical_smiles'], []))
    group['mapped_sites'] = sorted({i for row in group['records'] for i in row['atom_indices']})
    group['mapped_classes'] = sorted({i for row in group['records'] for i in row.get('symmetry_classes', [])})

summary = {
    'regio_rows': len(records), 'unique_source_smiles': len(groups),
    'unique_canonical_smiles': len({g['canonical_smiles'] for g in groups.values()}),
    'site_mapping_counts': dict(Counter(r['status'] for r in records)),
    'mapped_rows_with_multiple_equivalent_atoms': sum(len(r['atom_indices']) > 1 for r in records),
    'mapped_rows_with_multiple_symmetry_classes': sum(len(r.get('symmetry_classes', [])) > 1 for r in records),
    'condition_files': condition_sets, 'condition_rows': len(conditions),
    'condition_unique_source_smiles': len({r['startingmat_1_smiles'] for r in conditions}),
    'condition_unique_canonical_smiles': len(by_canonical),
    'binary_counts': dict(Counter(r['binary'] for r in conditions)),
    'condition_rxn_id_duplicates': len(conditions) - len({r['rxn_id'] for r in conditions}),
    'regio_substrates_with_condition_records': sum(g['condition_row_count'] > 0 for g in groups.values()),
    'unchanged_and_borylated_same_substrate': sum(any(r['status'] == 'unchanged_product' for r in g['records']) and any(r['status'] == 'mapped' for r in g['records']) for g in groups.values()),
    'source_sha256_verification': 'passed' if manifest_path.exists() else 'no manifest',
    'interpretation': 'Sites are recovered from observed product graphs; no model inference. Atom indices use exact source substrate RDKit parsing, zero based. Multiple matches require symmetry-aware display. Empty product fields are preserved separately and do not prove impossibility under all conditions.'
}
for name, value in [('summary.json', summary), ('site_records.json', records), ('substrates.json', list(groups.values()))]:
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False, indent=2))
print('Candidate complex examples:')
for group in sorted(groups.values(), key=lambda x: (len(x['mapped_classes']), x['condition_row_count'] > 0, x['heavy_atoms']), reverse=True)[:18]:
    print(json.dumps({k: group[k] for k in ['SMILES', 'heavy_atoms', 'aromatic_ch', 'mapped_sites', 'mapped_classes', 'condition_row_count']}, ensure_ascii=False))

"""Build eight evidence examples and current graph_data.v1 files; no inference."""
import csv
import hashlib
import json
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D

ROOT = Path(__file__).resolve().parents[1]
choices = [
    ('BO-01', '复杂底物与三个已报道位点', 'O=C(Nc1ccc(-c2csnn2)cc1)c1c(F)cccc1Cl'),
    ('BO-02', '简单的单一已报道位点', 'Brc1ccc2[nH]ccc2c1'),
    ('BO-03', '四个等价原子对应一种产物', 'Brc1ccc(Br)cc1'),
    ('BO-04', '吲哚的多个已报道位点', 'c1ccc2[nH]ccc2c1'),
    ('BO-05', '有产物记录但缺少可匹配条件表', 'CC(COc1ccc(Oc2ccccc2)cc1)Oc1ccccn1'),
    ('BO-06', '杂环匹配需要保留氢信息', 'Cc1nc2ccccc2[nH]1'),
    ('BO-07', '空产物记录与条件表', 'CN(Cc1ccc(F)cc1)C(=O)C1(c2ccccc2)CCN(C(=O)OC(C)(C)C)CC1'),
    ('BO-08', '多硼化产物需要人工核查', 'CC1N(c2ccccc2)C(=O)c2ccccc2N1C(=O)CN1CCOCC1'),
]
by_smiles = {x['SMILES']: x for x in json.loads((ROOT/'validation/substrates.json').read_text())}
for directory in ['processed', 'graphs', 'images']:
    (ROOT/directory).mkdir(exist_ok=True)
examples, condition_rows = [], []
for file, sep in [('pre_AL_binary_and_yield.csv',';'), ('AL_1_binary_and_yield.csv','\t'), ('AL_2_binary_and_yield.csv','\t'), ('AL_3_binary_and_yield.csv','\t')]:
    with (ROOT/'raw'/file).open(newline='') as handle:
        for line, row in enumerate(csv.DictReader(handle,delimiter=sep),2):
            row['_source_file'], row['_source_line'] = file, line
            canonical = Chem.MolToSmiles(Chem.MolFromSmiles(row['startingmat_1_smiles']))
            for ident, title, smiles in choices:
                if canonical == by_smiles[smiles]['canonical_smiles']:
                    condition_rows.append({'molecule_id': ident, **row})
for ident, title, smiles in choices:
    group = by_smiles[smiles]
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    n = mol.GetNumAtoms()
    symmetry = list(Chem.CanonicalRankAtoms(mol,breakTies=False))
    observed = group['mapped_sites']
    atoms = [{'atom_index': a.GetIdx(), 'display_number': a.GetIdx()+1, 'element': a.GetSymbol(), 'candidate_ch': a.GetAtomicNum()==6 and a.GetTotalNumHs()>0, 'aromatic': a.GetIsAromatic(), 'symmetry_class': symmetry[a.GetIdx()], 'observed_site': a.GetIdx() in observed} for a in mol.GetAtoms()]
    example = {'molecule_id':ident, 'title':title, 'input_smiles':smiles, 'canonical_smiles':group['canonical_smiles'], 'atom_index_base':0, 'heavy_atoms':n, 'aromatic_ch_count':len(group['aromatic_ch']), 'candidate_atom_indices':group['candidate_carbons'], 'observed_atom_indices':observed, 'observation_mapping_complete':all(r['status']=='mapped' for r in group['records']), 'product_missing':any(r['status']=='empty_product' for r in group['records']), 'requires_review':any(r['status']=='unresolved' for r in group['records']), 'prediction_status':'not_run', 'atom_scores':None, 'condition_records_count':group['condition_row_count'], 'condition_link_scope':'same canonical substrate only; no product-condition identity established', 'observations':group['records'], 'atoms':atoms}
    examples.append(example)
    bonds = [(b,i,j) for b in mol.GetBonds() for i,j in [(b.GetBeginAtomIdx(),b.GetEndAtomIdx()),(b.GetEndAtomIdx(),b.GetBeginAtomIdx())]]
    graph = {'schema':'graph_data.v1','name':f'{ident} · {title}','num_nodes':n,'directed':False,'semantic_type':'undirected','storage_type':'bidirectional','id2node':{str(a.GetIdx()):f'{ident}:{a.GetSymbol()}{a.GetIdx()}' for a in mol.GetAtoms()},'edge_index':[[i for b,i,j in bonds],[j for b,i,j in bonds]],'edge_weight':[1.0]*len(bonds),'node_type':[a.GetSymbol() for a in mol.GetAtoms()],'node_feat':[[a.GetAtomicNum(),a.GetDegree(),a.GetFormalCharge(),int(a.GetIsAromatic()),a.GetTotalNumHs(),int(a.IsInRing())] for a in mol.GetAtoms()],'edge_type':[str(b.GetBondType()) for b,i,j in bonds],'edge_feat':[[b.GetBondTypeAsDouble(),int(b.GetIsAromatic()),int(b.GetIsConjugated()),int(b.IsInRing())]for b,i,j in bonds],'node_text':[json.dumps(a,ensure_ascii=False) for a in atoms],'graph_metadata':{'molecule_id':ident,'input_smiles':smiles,'atom_index_base':0,'atom_index_order':'RDKit parse order of exact input_smiles, before adding H','source_commit':'c687cb1c52704a249f929c81bff5ca0ba21020f6','observed_atom_indices':observed,'observed_sites_are_partial':not example['observation_mapping_complete'],'prediction_status':'not_run','node_feature_columns':['atomic_number','degree','formal_charge','aromatic','total_hydrogens','in_ring'],'edge_feature_columns':['bond_order','aromatic','conjugated','in_ring'],'display_only':True}}
    (ROOT/'graphs'/f'{ident}.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2)+'\n')
    drawer = rdMolDraw2D.MolDraw2DSVG(1000,380)
    opts=drawer.drawOptions(); opts.addAtomIndices=True; opts.bondLineWidth=2
    drawer.DrawMolecule(mol,highlightAtoms=observed,highlightAtomColors={i:(0.4,0.8,0.6) for i in observed})
    drawer.FinishDrawing(); (ROOT/'images'/f'{ident}.svg').write_text(drawer.GetDrawingText())
    assert len(graph['node_feat'])==n and all(0<=i<n for i in observed)
    assert len(graph['edge_index'][0])==2*mol.GetNumBonds()
    assert len(graph['edge_index'][1])==len(graph['edge_weight'])==len(graph['edge_feat'])
(ROOT/'processed/examples.json').write_text(json.dumps(examples,ensure_ascii=False,indent=2)+'\n')
columns=list(dict.fromkeys(k for r in condition_rows for k in r))
with (ROOT/'processed/example_conditions.csv').open('w',newline='') as handle:
    writer=csv.DictWriter(handle,fieldnames=columns);writer.writeheader();writer.writerows(condition_rows)
model_input='SMILES,ID\n'+''.join(f'"{x["input_smiles"]}",{x["molecule_id"]}\n' for x in examples)
(ROOT/'processed/inference_input.csv').write_text(model_input)
manifest={'status':'passed','example_count':len(examples),'condition_rows':len(condition_rows),'inference_executed':False,'model_input_has_no_products':True,'checks':['graph array dimensions and atom bounds','two directed entries per chemical bond','source-derived sites kept outside node_feat and node_label'],'files':[{'path':str(p.relative_to(ROOT)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}for p in sorted((ROOT/'graphs').glob('*.json'))]}
(ROOT/'validation/examples_validation.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in manifest.items() if k!='files'},ensure_ascii=False))

"""Build the executable, evidence-only iGraph borylation case and its figures."""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D

DATA_ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo-root', type=Path, required=True)
args = parser.parse_args()
repo = args.repo_root.resolve()
sys.path.insert(0, str(repo))
from graph_data_service.construction import to_graph_data
from graph_data_service.graph_data import GraphData

examples = json.loads((DATA_ROOT/'processed/examples.json').read_text())
example = next(e for e in examples if e['molecule_id']=='BO-01')
mol = Chem.MolFromSmiles(example['input_smiles'])
rdDepictor.Compute2DCoords(mol)
atoms = []
for atom in mol.GetAtoms():
    base = dict(example['atoms'][atom.GetIdx()])
    base['total_hydrogens'] = atom.GetTotalNumHs()
    base['formal_charge'] = atom.GetFormalCharge()
    atoms.append(base)
with (DATA_ROOT/'processed/example_conditions.csv').open(newline='') as stream:
    raw_rows = [r for r in csv.DictReader(stream) if r['molecule_id']=='BO-01']
ligand_names = {'72914-19-3':'dtbpy','1134-35-6':'dime-bpy','1660-93-1':'tmphen'}
solvent_names = {'110-82-7':'环己烷','109-99-9':'THF','123-91-1':'1,4-二氧六环','75-05-8':'乙腈','872-50-4':'NMP','96-47-9':'2-MeTHF'}
def components(column, prefix, names):
    values = {r[column+'_cas']:r[column+'_smiles'] for r in raw_rows}
    return [{'id':f'{prefix}{i+1:02}', 'name':names.get(cas, cas), 'cas':cas, 'smiles':values[cas]} for i,cas in enumerate(sorted(values))]
ligands = components('ligand_1','L',ligand_names)
solvents = components('solvent_1','S',solvent_names)
ligand_ids = {x['cas']:x['id'] for x in ligands}; solvent_ids = {x['cas']:x['id'] for x in solvents}
records = []
for r in raw_rows:
    records.append({'rxn_id':r['rxn_id'],'ligand_id':ligand_ids[r['ligand_1_cas']],'solvent_id':solvent_ids[r['solvent_1_cas']],
                    'conversion':float(r['sum_mono_bo_conv']),'binary':int(r['binary']),
                    'source_file':r['_source_file'],'source_line':int(r['_source_line']),
                    'measurement':r['conversion_determination']})
shared_fields = ['temperature_deg_c','time_h','concentration_mol_l','startingmat_1_eq','reagent_1_smiles','reagent_1_eq','catalyst_1_smiles','catalyst_1_eq','ligand_1_eq','scale_mol']
shared = {}
for field in shared_fields:
    values = {r[field] for r in raw_rows}
    assert len(values)==1, (field,values)
    shared[field] = values.pop()
products = [{'product_id':f'BO-01:P{i+1}', 'smiles':r['product_smiles'], 'atom_indices':r['atom_indices'],
             'display_numbers':[a+1 for a in r['atom_indices']], 'source_file':'regioselectivity.csv','source_line':r['source_line']} for i,r in enumerate(example['observations'])]
bonds = [{'begin':b.GetBeginAtomIdx(),'end':b.GetEndAtomIdx(),'bond_type':str(b.GetBondType())} for b in mol.GetBonds()]
source_manifest = json.loads((DATA_ROOT/'source_manifest.json').read_text())
provenance = {'paper_url':'https://www.nature.com/articles/s43588-026-01063-0','data_url':'https://zenodo.org/records/20773622',
              'source_commit':source_manifest['commit'],'license':'CC BY 4.0','source_hashes':{e['file']:e['sha256']for e in source_manifest['entries']},
              'authors':'Mason Minot, Yannick Stenzhorn and coauthors','scope':'公开实验复盘，不是模型预测'}
case = {'case_id':'borylation-bo01-v1','molecule_id':'BO-01','smiles':example['input_smiles'],'atoms':atoms,'chemical_bonds':bonds,
        'products':products,'condition_records':records,'shared_conditions':shared,'ligands':ligands,'solvents':solvents,'provenance':provenance}
nodes,edges = [],[]
def node(identity,kind,properties):
    nodes.append({'id':identity,'type':kind,'properties':properties})
def edge(identity,kind,a,b,properties=None):
    edges.append({'id':identity,'type':kind,'source':a,'target':b,'properties':properties or {}})
node('BO-01','substrate',{'label':'BO-01 底物','smiles':case['smiles']})
for a in atoms:
    identity=f'BO-01:atom:{a["atom_index"]}'
    node(identity,'atom',{'label':f'{a["element"]}{a["display_number"]}',**a})
    edge(f'contains:{a["atom_index"]}','contains_atom','BO-01',identity)
for i,b in enumerate(bonds):
    a,c=f'BO-01:atom:{b["begin"]}',f'BO-01:atom:{b["end"]}'
    edge(f'bond:{i}:forward','chemical_bond',a,c,b)
    edge(f'bond:{i}:reverse','chemical_bond',c,a,b)
for p in products:
    node(p['product_id'],'observed_product',{'label':p['product_id'],**p})
    edge(p['product_id']+':substrate','reported_product','BO-01',p['product_id'])
    for idx in p['atom_indices']:
        edge(p['product_id']+':site:'+str(idx),'observed_at',p['product_id'],f'BO-01:atom:{idx}')
for component,kind in [(ligands,'ligand'),(solvents,'solvent')]:
    for c in component:
        node('BO-01:'+c['id'],kind,{'label':c['name'],**c})
for r in records:
    identity='BO-01:experiment:'+r['rxn_id']
    node(identity,'experiment',{'label':r['rxn_id'].rsplit('_',1)[-1],**r, 'conversion_percent':round(r['conversion']*100,4)})
    edge(identity+':substrate','tested_substrate',identity,'BO-01')
    edge(identity+':ligand','uses_ligand',identity,'BO-01:'+r['ligand_id'])
    edge(identity+':solvent','uses_solvent',identity,'BO-01:'+r['solvent_id'])
data = to_graph_data({'dataset_id':'ds_borylation_story_v1','revision':1,'graph':{'directed':True,'nodes':nodes,'edges':edges}})
data = GraphData(num_nodes=data.num_nodes, edge_index=data.edge_index,
                 id2node=data.id2node, node2id=data.node2id, edge_names=data.edge_names,
                 directed=True, node_type=data.node_type, edge_type=data.edge_type,
                 name='分子硼化反应示例', graph_metadata={**data.graph_metadata,'borylation_case':case},
                 graph_desc='节点包括底物重原子、产物和96组实验。chemical_bond双向条目表示24条化学键；其他边为记录关系。不要将总边数或全图节点度数解释为分子键数或化学价。当前案例是公开实验复盘，非模型预测。')
resources = repo/'graph_engine/resources/borylation'; resources.mkdir(parents=True,exist_ok=True)
filename = 'borylation-story.npz'
temporary = resources/'uncompressed.npz'; data.save(str(temporary))
with np.load(temporary,allow_pickle=True) as archive:
    np.savez_compressed(resources/filename,**{key:archive[key]for key in archive.files})
temporary.unlink()
now=datetime.now(timezone.utc).isoformat(timespec='seconds')
dataset={'dataset_id':'ds_borylation_story_v1','name':data.name,'description':data.graph_desc,'source_type':'borylation_preset',
         'node_count':data.num_nodes,'edge_count':data.num_edges,'is_directed':True,'created_at':now,'updated_at':now,'versions':['v001'],
         'edge_entry_count':data.num_edge_entries,'unique_edge_count':data.num_unique_edges,'semantic_type':data.semantic_type,'storage_type':data.storage_type,
         'is_symmetric':data.is_symmetric,'has_self_loops':data.has_self_loops,'has_duplicate_entries':data.has_duplicate_entries}
manifest={'version':1,'filename':filename,'dataset':dataset,'sha256':hashlib.sha256((resources/filename).read_bytes()).hexdigest(),'provenance':provenance}
(resources/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
(DATA_ROOT/'processed/system_case.json').write_text(json.dumps(case,ensure_ascii=False,indent=2)+'\n')
data.save(str(DATA_ROOT/'graphs/borylation-story.json'))
__import__('shutil').copy2(resources/filename,DATA_ROOT/'graphs'/filename)
image_dir=repo/'frontend/public/examples/borylation';image_dir.mkdir(parents=True,exist_ok=True)
for name,observed in [('BO-01',False),('BO-01-observed',True)]:
    for a in mol.GetAtoms():a.SetProp('atomNote',str(a.GetIdx()+1))
    drawer=rdMolDraw2D.MolDraw2DCairo(1100,420)
    drawer.drawOptions().bondLineWidth=2
    positions=example['observed_atom_indices'] if observed else [a['atom_index']for a in atoms if a['candidate_ch'] and a['aromatic']]
    color=(0.4,0.8,0.6) if observed else (0.55,0.75,0.95)
    drawer.DrawMolecule(mol,highlightAtoms=positions,highlightBonds=[],highlightAtomColors={i:color for i in positions})
    drawer.FinishDrawing();raw=drawer.GetDrawingText()
    (image_dir/(name+'.png')).write_bytes(raw);(DATA_ROOT/'images'/(name+'-system.png')).write_bytes(raw)
for i,p in enumerate(products,1):
    pmol=Chem.MolFromSmiles(p['smiles']);rdDepictor.Compute2DCoords(pmol)
    drawer=rdMolDraw2D.MolDraw2DCairo(900,400);drawer.DrawMolecule(pmol);drawer.FinishDrawing()
    (image_dir/f'BO-01-product-{i}.png').write_bytes(drawer.GetDrawingText())
    (DATA_ROOT/'images'/f'BO-01-product-{i}.png').write_bytes(drawer.GetDrawingText())
print(json.dumps({'nodes':data.num_nodes,'edge_entries':data.num_edge_entries,'nodes_by_type':dict(Counter(n['type']for n in nodes)),'case':manifest['dataset']['dataset_id']},ensure_ascii=False))

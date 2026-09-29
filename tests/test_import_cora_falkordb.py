import importlib
from types import SimpleNamespace

import pytest


def importer():
    try: return importlib.import_module('scripts.import_cora_falkordb')
    except ModuleNotFoundError: pytest.fail('Falkor importer missing')


def test_import_reuses_cora_direction_and_deduplication():
    nodes,edges,manifest=importer().convert_cora(SimpleNamespace(num_nodes=2,edge_index=[[0,1,0],[1,0,1]],y=[0,1]))
    assert edges==[{'source':0,'target':1}]
    assert manifest['node_count']==2 and manifest['edge_count']==1 and manifest['directed'] is False


def test_import_refuses_nonempty_foreign_graph_before_writes():
    commands=[]
    class Admin:
        def execute_command(self,*args):
            commands.append(args)
            if args[0]=='EXISTS': return 1
            if args[0]=='GRAPH.RO_QUERY': return [[b'count'],[[1]],[]]
            pytest.fail('foreign target was mutated')
    with pytest.raises(ValueError,match='incompatible'):
        importer().import_graph(Admin(),'foreign',[{'cora_id':0}],[], 'hash')
    assert all(cmd[0]!='GRAPH.QUERY' for cmd in commands)

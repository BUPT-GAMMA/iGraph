"""Verify case conclusions and that unsuitable graphs do not return invented facts."""
import copy
from pathlib import Path
import unittest

from graph_data_service.borylation import analyze_borylation
from graph_data_service.graph_data import GraphData

ASSET = Path(__file__).resolve().parents[2] / 'graph_engine/resources/borylation/borylation-story.npz'

class BorylationCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = GraphData.load(str(ASSET))

    def result(self, step):
        response = analyze_borylation(self.graph, {'step':step})
        self.assertTrue(response['success'], response)
        self.assertEqual(response['prediction_status'], 'not_run')
        return response['result']

    def test_only_first_molecule_is_loaded_and_evidence_is_preserved(self):
        r = self.result('structure')
        self.assertEqual(self.graph.num_nodes,22)
        self.assertEqual(self.graph.num_edges,48)
        self.assertFalse(self.graph.directed)
        self.assertTrue(all(name.startswith('BO-01:atom:') for name in self.graph.node2id))
        self.assertEqual(set(r['visualization_node_names']), set(self.graph.node2id))
        self.assertEqual((r['heavy_atom_count'],r['chemical_bond_count'],r['aromatic_ch_count']),(22,24,8))

    def test_site_nine_is_a_five_membered_sulfur_nitrogen_ring(self):
        r = self.result('structure')
        by_number = {a['display_number']: a for a in r['candidate_atoms']}
        ring = by_number[9]['ring_memberships'][0]
        self.assertEqual(ring['display_numbers'], [8, 9, 10, 11, 12])
        self.assertEqual(ring['elements'], {'C': 2, 'N': 2, 'S': 1})
        self.assertEqual((ring['size'], ring['kind']), (5, 'aromatic_heterocycle'))
        self.assertEqual(by_number[5]['ring_memberships'][0]['elements'], {'C': 6})
        self.assertEqual(by_number[19]['ring_memberships'][0]['size'], 6)

    def test_symmetry_does_not_count_equivalent_atoms_twice(self):
        r = self.result('symmetry')
        self.assertEqual(r['distinct_site_count'],6)
        self.assertEqual(sorted(tuple(c['display_numbers']) for c in r['site_classes']),[(5,14),(6,13),(9,),(18,),(19,),(20,)])

    def test_recorded_sites_have_sources_but_no_condition_identity(self):
        r = self.result('products')
        self.assertEqual(r['observed_display_numbers'],[9,18,19])
        self.assertEqual({p['source_line']for p in r['products']},{938,939,940})
        self.assertEqual([(p['display_numbers'],p['symmetry_classes']) for p in r['products']], [([19],[3]),([18],[9]),([9],[8])])
        self.assertEqual(r['condition_to_product_link'],'unavailable')

    def test_condition_outcomes_and_contrast(self):
        r = self.result('conditions')
        self.assertEqual((r['record_count'],r['success_count'],r['negative_count']),(96,4,92))
        self.assertEqual((r['nonzero_count'],r['subthreshold_nonzero_count']),(8,4))
        self.assertIn('不等于零转化',r['success_definition'])
        self.assertTrue(any(s['success_count']==0 and s['nonzero_count']>0 for s in r['solvent_summary']))
        self.assertEqual([x['conversion_percent']for x in r['top_conditions']],[26,21,14])
        self.assertEqual(len({(x['ligand_id'],x['solvent_id'])for x in r['matrix']}),96)
        contrast=r['recorded_contrast']
        self.assertEqual(contrast['higher_record']['ligand_id'],contrast['comparison_record']['ligand_id'])
        self.assertEqual(contrast['comparison_record']['rxn_id'],'eln046486-280_1_1_h5')
        self.assertEqual(contrast['comparison_record']['conversion_percent'],0)

    def test_plan_has_eight_runs_and_no_new_experimental_results(self):
        r = self.result('plan')
        self.assertEqual(r['recommended_total_runs'],8)
        self.assertEqual(r['condition_group_count'],4)
        self.assertTrue(all(p['expected_outcome']=='未知，需实验测定' for p in r['experiments']))

    def test_wrong_graph_and_invalid_arguments_fail(self):
        self.assertEqual(analyze_borylation(GraphData(),{'step':'structure'})['code'],'BORYLATION_CASE_NOT_LOADED')
        self.assertEqual(analyze_borylation(self.graph,{'step':'predict'})['code'],'INVALID_BORYLATION_ARGUMENTS')
        metadata=copy.deepcopy(self.graph.graph_metadata)
        metadata['borylation_case']['condition_records'][0]['conversion']=float('nan')
        g=GraphData(graph_metadata=metadata)
        self.assertEqual(analyze_borylation(g,{'step':'conditions'})['code'],'INVALID_BORYLATION_CASE_DATA')

if __name__ == '__main__':
    unittest.main()

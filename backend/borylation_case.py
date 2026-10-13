"""Localized four-question walkthrough for the bundled molecular graph."""

BORYLATION_CASE = {'id': 'borylation',
 'name': '分子硼化反应示例',
 'filename': 'borylation-story.npz',
 'personal_graph': True,
 'description': '以 BO-01 分子为对象，将原子与化学键构成分子结构图，并关联芳香 C–H 位点与已报道的硼化反应记录。',
 'questions': ['这个分子在常见有机溶剂中的溶解性可能如何？是否有毒性证据？请区分实验数据、计算预测与结构推测，并说明不能确定的部分。',
               '这个分子包含几个环、几种类型的环？请分别说明各个环的大小、元素组成、芳香性及主要化学性质。',
               '哪些芳香 C–H 位点可以考虑做硼化反应？请在分子结构图上标注出来。',
               '上述芳香 C–H 位点中，哪些因分子对称性而等价？合并后有哪些不等价位点？请列出等价分组和代表位点。'],
 'story_steps': [{'id': 'properties',
                  'title': '属性判断',
                  'analysis_step': 'properties',
                  'question': '这个分子在常见有机溶剂中的溶解性可能如何？是否有毒性证据？请区分实验数据、计算预测与结构推测，并说明不能确定的部分。'},
                 {'id': 'rings',
                  'title': '环系分析',
                  'analysis_step': 'rings',
                  'question': '这个分子包含几个环、几种类型的环？请分别说明各个环的大小、元素组成、芳香性及主要化学性质。'},
                 {'id': 'structure',
                  'title': '位点识别',
                  'analysis_step': 'structure',
                  'question': '哪些芳香 C–H 位点可以考虑做硼化反应？请在分子结构图上标注出来。'},
                 {'id': 'symmetry',
                  'title': '对称等价',
                  'analysis_step': 'symmetry',
                  'question': '上述芳香 C–H 位点中，哪些因分子对称性而等价？合并后有哪些不等价位点？请列出等价分组和代表位点。'}],
 'translations': {'en': {'name': 'Molecular borylation example',
                         'description': 'A molecular graph of BO-01: atoms and chemical bonds, linked to aromatic '
                                        'C–H sites and reported borylation records.',
                         'questions': ['How soluble might this molecule be in common organic solvents, and is '
                                       'there evidence of toxicity? Distinguish experimental data, computational '
                                       'predictions, and structural hypotheses, and explain what remains unknown.',
                                       'How many rings and ring types does this molecule contain? Describe each '
                                       'ring’s size, elemental composition, aromaticity, and main chemical '
                                       'properties.',
                                       'Which aromatic C–H sites are candidates for borylation? Mark them on the '
                                       'molecular structure.',
                                       'Which of these aromatic C–H sites are equivalent by molecular symmetry? '
                                       'List the equivalence groups and a representative for each distinct site '
                                       'class.'],
                         'story_steps': [{'id': 'properties',
                                          'title': 'Properties',
                                          'analysis_step': 'properties',
                                          'question': 'How soluble might this molecule be in common organic '
                                                      'solvents, and is there evidence of toxicity? Distinguish '
                                                      'experimental data, computational predictions, and '
                                                      'structural hypotheses, and explain what remains unknown.'},
                                         {'id': 'rings',
                                          'title': 'Ring systems',
                                          'analysis_step': 'rings',
                                          'question': 'How many rings and ring types does this molecule contain? '
                                                      'Describe each ring’s size, elemental composition, '
                                                      'aromaticity, and main chemical properties.'},
                                         {'id': 'structure',
                                          'title': 'Borylation sites',
                                          'analysis_step': 'structure',
                                          'question': 'Which aromatic C–H sites are candidates for borylation? '
                                                      'Mark them on the molecular structure.'},
                                         {'id': 'symmetry',
                                          'title': 'Site symmetry',
                                          'analysis_step': 'symmetry',
                                          'question': 'Which of these aromatic C–H sites are equivalent by '
                                                      'molecular symmetry? List the equivalence groups and a '
                                                      'representative for each distinct site class.'}],
                         'question_titles': ['Properties', 'Ring systems', 'Borylation sites', 'Site symmetry']}},
 'question_titles': ['属性判断', '环系分析', '位点识别', '对称等价']}

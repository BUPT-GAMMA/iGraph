"""Localized walkthrough for the bundled molecular graph."""

BORYLATION_CASE = {'id': 'borylation',
 'name': '分子硼化反应示例',
 'filename': 'borylation-story.npz',
 'description': '从一个底物出发，看清可选反应位点，再比较已报道产物和 96 组实验条件，安排下一轮验证。',
 'questions': ['先把这个底物画出来：有哪些芳香 C–H 位点可以考虑做硼化？请在图上标出来。',
               '这些位点里有没有因为分子对称性而等价的？去掉重复后，还有几类不同的位置？',
               '论文里实际报道了哪几个反应位点？把产物和底物上的位置对应起来。',
               '这个底物试过多少组反应条件，真正做成的有几组？候选位点多，是不是就意味着反应容易做？',
               '把成功的几组条件放在一起比较，看看配体和溶剂有什么共同点。这些数据能说明哪个条件更适合某个位点吗？',
               '如果下一轮只能做一小批实验，你会怎么选位点和条件？给出一个计划，把已有证据和待验证的假设分开说。'],
 'story_steps': [{'id': 'structure',
                  'title': '从底物开始',
                  'analysis_step': 'structure',
                  'question': '先把这个底物画出来：有哪些芳香 C–H 位点可以考虑做硼化？请在图上标出来。'},
                 {'id': 'symmetry',
                  'title': '排除等价位点',
                  'analysis_step': 'symmetry',
                  'question': '这些位点里有没有因为分子对称性而等价的？去掉重复后，还有几类不同的位置？'},
                 {'id': 'products',
                  'title': '看看已有产物',
                  'analysis_step': 'products',
                  'question': '论文里实际报道了哪几个反应位点？把产物和底物上的位置对应起来。'},
                 {'id': 'condition_screen',
                  'title': '反应有多难做',
                  'analysis_step': 'conditions',
                  'question': '这个底物试过多少组反应条件，真正做成的有几组？候选位点多，是不是就意味着反应容易做？'},
                 {'id': 'condition_comparison',
                  'title': '比较成功条件',
                  'analysis_step': 'conditions',
                  'question': '把成功的几组条件放在一起比较，看看配体和溶剂有什么共同点。这些数据能说明哪个条件更适合某个位点吗？'},
                 {'id': 'experiment_plan',
                  'title': '安排下一轮实验',
                  'analysis_step': 'plan',
                  'question': '如果下一轮只能做一小批实验，你会怎么选位点和条件？给出一个计划，把已有证据和待验证的假设分开说。'}],
 'translations': {'en': {'name': 'Molecular borylation example',
                         'description': 'Explore candidate sites on one substrate, compare '
                                        'reported products and 96 experimental conditions, and '
                                        'plan follow-up validation.',
                         'questions': ['Draw this substrate first: which aromatic C–H sites are '
                                       'candidates for borylation? Mark them on the structure.',
                                       'Are any of these sites equivalent because of molecular '
                                       'symmetry? How many distinct site classes remain after '
                                       'removing duplicates?',
                                       'Which reaction sites were actually reported in the paper? '
                                       'Map the products back to their positions on the substrate.',
                                       'How many reaction conditions were tested for this '
                                       'substrate, and how many met the success threshold? Does '
                                       'having many candidate sites mean the reaction is easy?',
                                       'Compare the successful conditions: what do their ligands '
                                       'and solvents have in common? Can these data tell us which '
                                       'conditions suit a particular site?',
                                       'If the next round allows only a small batch of '
                                       'experiments, how would you choose sites and conditions? '
                                       'Propose a plan that separates existing evidence from '
                                       'hypotheses to test.'],
                         'story_steps': [{'id': 'structure',
                                          'title': 'Start with the substrate',
                                          'analysis_step': 'structure',
                                          'question': 'Draw this substrate first: which aromatic '
                                                      'C–H sites are candidates for borylation? '
                                                      'Mark them on the structure.'},
                                         {'id': 'symmetry',
                                          'title': 'Identify equivalent sites',
                                          'analysis_step': 'symmetry',
                                          'question': 'Are any of these sites equivalent because '
                                                      'of molecular symmetry? How many distinct '
                                                      'site classes remain after removing '
                                                      'duplicates?'},
                                         {'id': 'products',
                                          'title': 'Inspect reported products',
                                          'analysis_step': 'products',
                                          'question': 'Which reaction sites were actually reported '
                                                      'in the paper? Map the products back to '
                                                      'their positions on the substrate.'},
                                         {'id': 'condition_screen',
                                          'title': 'Assess the condition screen',
                                          'analysis_step': 'conditions',
                                          'question': 'How many reaction conditions were tested '
                                                      'for this substrate, and how many met the '
                                                      'success threshold? Does having many '
                                                      'candidate sites mean the reaction is easy?'},
                                         {'id': 'condition_comparison',
                                          'title': 'Compare successful conditions',
                                          'analysis_step': 'conditions',
                                          'question': 'Compare the successful conditions: what do '
                                                      'their ligands and solvents have in common? '
                                                      'Can these data tell us which conditions '
                                                      'suit a particular site?'},
                                         {'id': 'experiment_plan',
                                          'title': 'Plan the next experiments',
                                          'analysis_step': 'plan',
                                          'question': 'If the next round allows only a small batch '
                                                      'of experiments, how would you choose sites '
                                                      'and conditions? Propose a plan that '
                                                      'separates existing evidence from hypotheses '
                                                      'to test.'}]}}}

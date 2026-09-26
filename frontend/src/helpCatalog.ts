import toolLabels from './toolLabels.json';

export type HelpText = { en: string; zh: string };
export type HelpTool = { name: string; category: string; description: HelpText };
export type HelpModel = { name: string; title: string; description: HelpText; tasks: string[]; kind: 'backbone' | 'embedding' };
const text = (en: string, zh: string): HelpText => ({ en, zh });
const tool = (name: string, category: string, en: string, zh: string): HelpTool => ({ name, category, description: text(en, zh) });

export const helpCategories: Record<string, HelpText> = {
  inspection: text('Nodes & relationships', '节点与关系查询'),
  paths: text('Paths, flow & spanning trees', '路径、流与生成树'),
  structure: text('Structure & connectivity', '结构与连通性'),
  metrics: text('Centrality & graph metrics', '中心性与图指标'),
  communities: text('Communities & subgraphs', '社区与子图'),
  largeGraph: text('Large-graph computation', '大图计算'),
  finance: text('Ownership & control', '股权与控制关系'),
  learning: text('Graph learning tools', '图学习工具'),
  visualization: text('Visualization & reports', '可视化与报告'),
};

/** User-facing descriptions of the registered Graph Engine analysis and learning tools.
 * tests/test_help_catalog.py checks coverage against the actual tool catalog.
 */
export const helpTools: HelpTool[] = [
  tool('get_graph_info', 'inspection', 'Inspect graph size, direction, attributes and learning readiness.', '查看图规模、方向、属性及图学习数据准备情况。'),
  tool('get_node_info', 'inspection', 'Inspect a node’s properties and local structure using its exact ID.', '使用精确节点 ID 查看节点属性和局部结构。'),
  tool('get_edge_info', 'inspection', 'Inspect directed relationships and properties between two nodes.', '查看两个节点之间的有向关系与边属性。'),
  tool('check_edge_exists', 'inspection', 'Check whether a directed edge exists between exact endpoints.', '检查指定起终点之间是否存在有向边。'),
  tool('get_neighbors', 'inspection', 'Find one-hop neighbors in the selected direction.', '查询指定方向的一跳邻居节点。'),
  tool('search_node', 'inspection', 'Find nodes by names, descriptions, attributes or structural constraints.', '按名称、自然语言描述、属性或结构约束检索节点。'),
  tool('search_edge', 'inspection', 'Find relationships by edge descriptions, attributes or endpoint descriptions.', '按边描述、属性或起终点描述检索关系。'),
  tool('shortest_path', 'paths', 'Find a minimum-cost or minimum-hop path, or the first k shortest simple paths.', '计算最低成本或最少跳数路径，也可查询前 k 条最短简单路径。'),
  tool('all_shortest_paths', 'paths', 'Enumerate equal-length shortest paths between two nodes.', '枚举两个节点之间全部等长最短路径。'),
  tool('path_analysis', 'paths', 'Check reachability, enumerate simple paths or calculate average shortest-path length.', '检查可达性、枚举简单路径或计算平均最短路径长度。'),
  tool('reachability', 'paths', 'Find reachable ancestors and descendants or inspect transitive closure.', '查询可达祖先、后代，或查看传递闭包。'),
  tool('maximum_flow', 'paths', 'Treat edge weights as capacities to find maximum source-to-sink flow.', '将边权视为容量，计算源点到汇点的最大流。'),
  tool('minimum_cut', 'paths', 'Find the minimum-capacity cut separating a source and sink.', '寻找分离源点与汇点的最小容量割，定位网络瓶颈。'),
  tool('global_minimum_cut', 'paths', 'Find a global minimum cut in an undirected calculation view.', '在无向计算视图中寻找全局最小割。'),
  tool('minimum_spanning_tree', 'paths', 'Find a minimum-weight spanning tree, or a forest for disconnected graphs.', '寻找最小权重生成树；不连通图返回生成森林。'),
  tool('graph_connectivity', 'structure', 'Analyze connected components or directed strong and weak connectivity.', '分析连通分量，或有向图的强连通与弱连通性。'),
  tool('connectivity_analysis', 'structure', 'Inspect vertex/edge connectivity, cut vertices, bridges and biconnected components.', '分析点/边连通度、割点、桥、双连通分量与缩点图。'),
  tool('find_cycles', 'structure', 'Find simple cycles to inspect feedback loops and cyclic dependencies.', '查找简单环，检查反馈回路与循环依赖。'),
  tool('maximum_cycle', 'structure', 'Search for the longest or highest-weight simple cycle within an enumeration limit.', '在枚举上限内查找最长或最高权重简单环；结果会说明是否穷尽。'),
  tool('topological_sort', 'structure', 'Order a directed acyclic graph by dependency precedence.', '按依赖先后关系对有向无环图进行拓扑排序。'),
  tool('clique_analysis', 'structure', 'Find maximal cliques, maximum clique size and node participation.', '查找极大团、最大团大小及节点参与情况。'),
  tool('graph_structure_analysis', 'structure', 'Inspect graph types, BFS/DFS traversals, planarity and Weisfeiler–Lehman hashes.', '判定树、森林、DAG、二部图等结构，执行 BFS/DFS、平面性检查与图哈希。'),
  tool('tree_dag_analysis', 'structure', 'Analyze common ancestors, dominators, DAG longest paths and spanning trees.', '分析最近公共祖先、支配关系、DAG 最长路及最大/随机生成树。'),
  tool('matching_coloring_analysis', 'structure', 'Calculate matching, edge cover and greedy or equitable graph coloring.', '计算匹配、边覆盖、贪心着色与均衡着色。'),
  tool('node_centrality', 'metrics', 'Rank node importance using degree, betweenness, random walks and other centralities.', '用度、介数、随机游走等中心性指标对节点重要性排序。'),
  tool('degree_analysis', 'metrics', 'Inspect in-degree, out-degree and total-degree distributions.', '分析入度、出度和总度分布，发现枢纽和孤立节点。'),
  tool('global_metrics', 'metrics', 'Calculate density, diameter, radius, clustering and assortativity.', '计算密度、直径、半径、聚类系数与同配系数。'),
  tool('distance_measures', 'metrics', 'Measure centers, peripheries, efficiency, reciprocity and other structural distances.', '计算中心、边缘、离心率、效率、互惠性等扩展距离与结构指标。'),
  tool('community_detection', 'communities', 'Identify densely connected communities and inspect modularity.', '识别连接紧密的社区，查看社区规模与模块度。'),
  tool('subgraph_extraction', 'communities', 'Preview or save an ego neighborhood, core, truss or node-induced subgraph.', '预览或保存 ego 邻域、核、truss、corona 及节点诱导子图。'),
  tool('link_prediction_score', 'communities', 'Rank potential edges by neighborhood similarity, without training a model.', '使用邻域相似度和节点度为候选边评分，无需训练模型。'),
  tool('run_large_graph_algorithm', 'largeGraph', 'Run a supported algorithm on a prepared partitioned graph snapshot.', '在已准备的分区大图快照上运行受支持的算法。'),
  tool('get_large_graph_artifact', 'largeGraph', 'Inspect bounded previews and download metadata for large-graph results.', '查看大图计算结果的有界预览与分区下载信息。'),
  tool('retrieve_context', 'inspection', 'Retrieve supporting passages from documents uploaded to the session.', '从当前会话上传的文档中检索相关背景片段。'),
  tool('nebula_get_node_info', 'inspection', 'Read node properties directly from configured NebulaGraph storage.', '从已配置的 NebulaGraph 存储直接读取节点属性。'),
  tool('nebula_get_neighbors', 'inspection', 'Query one-hop neighbors directly in configured NebulaGraph storage.', '从已配置的 NebulaGraph 存储直接查询一跳邻居。'),
  tool('nebula_shortest_path', 'paths', 'Find the minimum-hop path in NebulaGraph; edge weights are not used.', '在 NebulaGraph 中查询最少跳数路径；不使用边权。'),
  tool('visualize_subgraph', 'visualization', 'Show a graph or node subset with node, path and subgraph highlights.', '展示图或节点子集，支持节点、路径与子图高亮。'),
  tool('create_analysis_report', 'visualization', 'Organize computed analysis results into charts, tables and narrative summaries.', '将已计算的分析结果组织为图表、表格和文字摘要；可由部署配置关闭。'),
  tool('finance_inspect_ownership', 'finance', 'Inspect the loaded ownership snapshot, find entities, and check shares and data conflicts.', '检查当前股权快照、检索主体，并核对持股比例和数据冲突。'),
  tool('finance_first_holder', 'finance', 'Identify first-holder candidates with direct ownership, cumulative interests, ties and rule coverage.', '依据直接持股和累计经济权益识别第一大股东候选，保留并列与规则缺失状态。'),
  tool('finance_control_chain', 'finance', 'Trace control chains using strictly more than 50% controllable voting rights and supporting facts.', '按严格超过 50% 的可支配表决权追踪控股链，并保留持股事实和贡献证明。'),
  tool('finance_group_tree', 'finance', 'Show the strictly controlled members of a selected root and their hierarchy, with review and coverage limits.', '展示指定根主体的严格控制成员与集团层级，同时说明复核状态和规则覆盖范围。'),
  tool('finance_economic_interest', 'finance', 'Accumulate economic interests for boundary roots or a specified upstream owner, with cycle and evidence status.', '累计边界根或指定上游主体的经济权益，保留环路求解与证据状态；经济权益不能直接认定控制关系。'),
  tool('load_graph_classification_dataset', 'learning', 'Load an uploaded HIV or PCBA multi-graph PT dataset for graph classification.', '加载已上传的 HIV 或 PCBA 多图 PT 数据集，用于图分类。'),
  tool('load_graph_classification_graph', 'learning', 'Select a graph-classification sample as the active graph for exploration.', '选取图分类数据集中的一张图，载入画布进行分析与可视化。'),
  tool('visualize_model_architecture', 'learning', 'Inspect a trained model’s layer summary and architecture diagram.', '查看已训练模型的层级摘要和结构图。'),
  tool('train', 'learning', 'Train node classification, link prediction, graph classification or pretraining models.', '训练节点分类、链接预测、图分类或预训练模型。'),
  tool('infer', 'learning', 'Run a saved model on selected nodes, node pairs or graph samples.', '使用已保存模型对指定节点、节点对或图样本执行推理。'),
];

export const helpTasks: Record<string, HelpText> = {
  node_classification: text('Node classification', '节点分类'),
  link_prediction: text('Link prediction', '链接预测'),
  graph_classification: text('Graph classification', '图分类'),
  pretrain: text('Pretraining', '预训练'),
};
const backboneTasks = ['node_classification', 'link_prediction', 'graph_classification', 'pretrain'];
const backbone = (name: string, title: string, en: string, zh: string): HelpModel => ({ name, title, description: text(en, zh), tasks: backboneTasks, kind: 'backbone' });

/** Names match graph_data_service/graph_ml/models/_BACKBONE_REGISTRY. */
export const helpModels: HelpModel[] = [
  backbone('APPNP', 'Approximate Personalized Propagation', 'Combines feature prediction with personalized PageRank-style propagation.', '将特征预测与个性化 PageRank 风格的传播结合。'),
  backbone('GCN', 'Graph Convolutional Network', 'Aggregates normalized neighbor features with graph convolutions.', '通过图卷积聚合归一化的邻居特征。'),
  backbone('GAT', 'Graph Attention Network', 'Learns attention weights to combine neighboring node features.', '学习注意力权重以组合邻居节点特征。'),
  backbone('GATV2', 'Graph Attention Network v2', 'Uses dynamic graph attention to weight neighboring features.', '使用动态图注意力对邻居特征加权。'),
  backbone('SAGE', 'GraphSAGE', 'Learns node representations by aggregating neighborhood features.', '通过聚合邻域特征学习节点表示。'),
  backbone('TRANSFORMER', 'Graph Transformer', 'Uses TransformerConv attention for graph message passing.', '使用 TransformerConv 注意力进行图消息传递。'),
  backbone('GEN', 'Generalized Graph Convolution', 'Uses GENConv generalized message aggregation from the DeeperGCN family.', '使用 DeeperGCN 系列 GENConv 的广义消息聚合。'),
  backbone('GIN', 'Graph Isomorphism Network', 'Uses neighborhood aggregation and an MLP to learn structural representations.', '通过邻域聚合和多层感知机学习图结构表示。'),
  { name: 'node2vec', title: 'Node2Vec', kind: 'embedding', tasks: ['pretrain'], description: text('Learns node embeddings from biased random walks. Set task=pretrain and method=node2vec; no GNN backbone is required.', '通过有偏随机游走学习节点嵌入。设置 task=pretrain、method=node2vec，无需选择 GNN 主干。') },
];

export const helpPretraining = [
  { name: 'contrastive_learning', alias: 'graph_contrastive_learning', description: text('Accepted pretraining method name. The current trainer uses a shared positive/negative edge-reconstruction objective with the selected GNN backbone.', '支持的预训练方法名称。当前训练器使用所选 GNN 主干和统一的正负边重建目标。') },
  { name: 'mask_autoencoding', alias: 'graph_mask_autoencoding', description: text('Accepted pretraining method name. The current trainer uses the same edge-reconstruction objective; it does not implement a separate masked-feature loss.', '支持的预训练方法名称。当前训练器使用相同的边重建目标，尚未实现独立的掩码特征损失。') },
];

export function helpToolTitle(name: string, language: 'en' | 'zh') {
  return language === 'zh' ? (toolLabels as Record<string, string>)[name] || name : name.split('_').join(' ');
}

export function matchesHelpQuery(query: string, ...values: string[]) {
  const terms = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const content = values.join(' ').toLocaleLowerCase();
  return terms.every(term => content.includes(term));
}

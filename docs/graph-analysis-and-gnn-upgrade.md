# 图分析算子与现代 GNN 升级

本轮升级以 YiGraph 公布的 21 类、212 个图算法目录为对标基线，优先补齐
iGraph 原先缺少、同时能够稳定映射到当前 NetworkX/PyG 运行栈的能力。

## 新增图分析能力

新增 5 个聚合工具、55 个细分算子：

| 工具 | 数量 | 主要能力 |
| --- | ---: | --- |
| `graph_structure_analysis` | 13 | 树/森林/DAG/二部图/锦标赛判定，BFS/DFS，拓扑序，平面性，WL 哈希 |
| `connectivity_analysis` | 10 | 点边连通度、最小割集、割点、桥、双连通分量、强连通缩点 |
| `tree_dag_analysis` | 11 | 最大/随机生成树、LCA、支配关系、DAG 最长路、反链、v-structure |
| `matching_coloring_analysis` | 7 | 最大/最小权匹配、极大匹配、边覆盖、匹配校验、图着色 |
| `distance_measures` | 14 | 中心/边缘/离心率、Wiener、效率、富俱乐部、互惠性、Estrada、围长 |

每个工具都注册为只读图计算，带有输出预览上限；指数级的全部拓扑序和反链枚举
额外保留 200 条硬上限。当前未完成语义审查的平行边图会明确返回不支持，而不会
静默折叠关系。

## 新增图机器学习主干

在原有 `GCN`、`GAT`、`SAGE` 基础上新增：

| `model` 值 | PyG 实现 | 选择原因 |
| --- | --- | --- |
| `GATV2` | `GATv2Conv` | 动态注意力修复原始 GAT 的静态排序限制，参数量级相近 |
| `TRANSFORMER` | `TransformerConv` | 提供带门控跳连的多头图 Transformer 消息传递 |
| `GEN` | `GENConv` | DeeperGCN 的广义聚合算子，适合更深的残差/归一化堆叠 |
| `GIN` | `GINConv` | 与 WL 图同构测试相当的经典消息传递表达能力，适合图分类 |
| `APPNP` | `APPNP` | MLP 预测与 Personalized PageRank 传播解耦，扩大节点分类感受野 |

五个主干复用现有节点分类、链接预测、图分类、预训练、推理、模型持久化和架构
可视化链路，不引入新的运行时依赖。注意力模型继续使用 `algo_config.gat_heads`。
APPNP 使用 `algo_config.appnp_k`（默认 10）和 `algo_config.appnp_alpha`（默认 0.1）。

调用示例：

```json
{
  "task": "node_classification",
  "model": "GATV2",
  "algo_config": {
    "hidden_dim": 64,
    "embedding_dim": 64,
    "num_layers": 3,
    "gat_heads": 4,
    "use_residual_connection": true,
    "use_normalization": true
  }
}
```

## 调研依据与取舍

- [YiGraph 算法文档](http://idc-neu.com/YiGraphDocs/docs/intro)：用于算子类别和缺口对标。
- [GATv2 论文](https://arxiv.org/abs/2105.14491)与
  [PyG GATv2Conv](https://pytorch-geometric.readthedocs.io/en/2.8.0/generated/torch_geometric.nn.conv.GATv2Conv.html)。
- [Unified Message Passing / Graph Transformer 论文](https://arxiv.org/abs/2009.03509)与
  [PyG TransformerConv](https://pytorch-geometric.readthedocs.io/en/2.8.0/generated/torch_geometric.nn.conv.TransformerConv.html)。
- [DeeperGCN 论文](https://arxiv.org/abs/2006.07739)与
  [PyG GENConv](https://pytorch-geometric.readthedocs.io/en/2.8.0/generated/torch_geometric.nn.conv.GENConv.html)。
- [GIN 论文](https://arxiv.org/abs/1810.00826)与
  [PyG GINConv](https://pytorch-geometric.readthedocs.io/en/2.8.0/generated/torch_geometric.nn.conv.GINConv.html)。
- [APPNP 论文](https://arxiv.org/abs/1810.05997)与
  [PyG APPNP](https://pytorch-geometric.readthedocs.io/en/2.8.0/generated/torch_geometric.nn.conv.APPNP.html)。
- [GraphGPS](https://arxiv.org/abs/2205.12454)和近年的 Graph Mamba 路线被纳入调研，
  但没有在本轮直接接入：前者需要图级 batch 隔离、位置/结构编码和全局注意力配置，
  后者还需要稳定的节点排序及额外状态空间模型依赖。直接套进当前二参数 backbone
  接口会产生跨图注意力泄漏或不可复现实验，因此留作下一阶段的完整数据管线升级。

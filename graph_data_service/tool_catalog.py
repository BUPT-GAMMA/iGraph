"""
图工具规格唯一真相源（tool catalog）。

所有下游消费方（MCP Server、TextProtocolDriver、NativeFCDriver、execute_tool 分发）
都从这里读取工具规格，不再各自维护副本。

新增工具步骤：
  1. 在 graph_data_service/compute.py 实现算法
  2. 在此文件 GRAPH_TOOLS 列表末尾添加一条规格
  3. 完成 —— MCP 注册、system prompt、native FC 参数全部自动生效
"""

from __future__ import annotations

from typing import List

# ─────────────────────────────────────────────────────────────────
# 图分析工具（对外暴露给 LLM）
# ─────────────────────────────────────────────────────────────────

GRAPH_TOOLS: List[dict] = [
    {
        "name": "load_graph",
        "description": (
            "从边列表文本加载图到当前会话。\n\n"
            "参数:\n"
            "- edge_text: 边列表文本，每行格式为 `src dst [weight]`。\n"
            "- append(可选, 默认 false):\n"
            "  - false: 覆盖当前图；\n"
            "  - true: 追加到当前图。\n\n"
            "返回:\n"
            "- JSON 字符串，包含加载状态、图版本和提示信息。\n"
            "- 图规模/结构详情请调用 `get_graph_info`。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "edge_text": {
                    "type": "string",
                    "description": "边列表文本，每行 `src dst [weight]`",
                },
                "append": {
                    "type": "boolean",
                    "description": "false: 覆盖；true: 追加",
                    "default": False,
                },
            },
            "required": ["edge_text"],
        },
        "category": "data",
    },
    {
        "name": "load_graph_from_upload",
        "description": (
            "从当前会话已注册的上传文件中加载图数据，**自动检测格式**并路由处理：\n"
            "- 二进制/JSON 图文件（.npz / .pt / .json graph_data.v1）→ 结构化直接加载；\n"
            "- HIV/PCBA 等多图 .pt 文件 → 自动路由到 load_graph_classification_dataset 异步加载；\n"
            "- 边列表（每行 `src dst [weight]`，空格/Tab/逗号分隔）→ 直接解析；\n"
            "- 三元组（每行 `source relation target`）→ 直接解析；\n"
            "- 自然语言文本（散文、文章、文档）→ LLM 预检提取 extraction_goal 后提交异步 text2graph 作业，返回 job_id，需用 job_status/job_result 轮询。\n\n"
            "格式由 LLM 预检（FileFormatPreCheckSubAgent）自动判断，无需手动指定；LLM 不可用时降级为启发式检测。\n\n"
            "参数:\n"
            "- filename: 会话中已注册的上传文件名。\n"
            "- append(可选, 默认 false): false 覆盖当前图；true 追加到当前图。\n\n"
            "返回:\n"
            "- 同步格式（binary/edge_list/triple）：加载状态、检测格式、图版本；\n"
            "- 异步格式（freetext/graph_classification_dataset）：async=true、job_id、status='queued'；\n"
            "- 图规模/结构详情请调用 `get_graph_info`。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "会话中已注册的上传文件名",
                },
                "append": {
                    "type": "boolean",
                    "description": "false: 覆盖；true: 追加",
                    "default": False,
                },
            },
            "required": ["filename"],
        },
        "category": "data",
    },
    {
        "name": "clear_graph",
        "description": (
            "清空当前会话中的图数据。\n\n"
            "返回:\n"
            "- JSON 字符串，成功时会重置图状态并返回新的图版本号。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "category": "data",
    },
    {
        "name": "undo_graph",
        "description": (
            "将当前图回滚到上一版本快照。\n\n"
            "返回:\n"
            "- JSON 字符串，包含回滚是否成功和新的图版本号。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "category": "data",
    },
    {
        "name": "list_graph_versions",
        "description": (
            "列出当前会话可用的图版本。\n\n"
            "每个版本包含 graph_version、描述、创建时间、节点/边数量、是否为当前版本。"
            "当用户需要回退、比较完整图和子图版本时，先调用此工具。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "category": "data",
    },
    {
        "name": "load_graph_version",
        "description": (
            "切换当前会话到指定图版本。\n\n"
            "参数:\n"
            "- graph_version: list_graph_versions 返回的版本号。\n\n"
            "返回:\n"
            "- JSON 字符串，包含切换结果、当前图版本、节点数和边数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "graph_version": {"type": "integer", "description": "要加载的图版本号"},
            },
            "required": ["graph_version"],
        },
        "category": "data",
    },
    {
        "name": "discard_graph_version",
        "description": "内部工具：删除指定图版本；若删除当前版本，则切回指定 fallback 版本。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "graph_version": {"type": "integer", "description": "要删除的图版本号"},
                "fallback_graph_version": {"type": "integer", "description": "删除当前版本时切回的版本号"},
            },
            "required": ["graph_version"],
        },
        "category": "internal",
    },
    {
        "name": "to_undirected",
        "description": (
            "将当前图转换为无向图。\n\n"
            "返回:\n"
            "- JSON 字符串，包含转换后的图信息（节点、边、图版本等）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "category": "data",
    },
    {
        "name": "get_graph_info",
        "description": (
            "查询当前会话图的全局摘要信息。\n\n"
            "返回:\n"
            "- JSON 字符串，包含图概要、节点/边预览、图数据元信息（标签/类型/特征）及图机器学习可行性字段。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "category": "data",
    },
    {
        "name": "get_node_info",
        "description": (
            "查询指定节点的详细信息。\n\n"
            "参数:\n"
            "- node_name: 节点名称或节点 ID（字符串）。\n\n"
            "返回:\n"
            "- JSON 字符串，包含入度/出度、邻居、前驱、关联边、标签/类型等信息；"
            "若图含节点文本（文本属性图），还会返回 text 字段。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "节点名称或 ID"},
            },
            "required": ["node_name"],
        },
        "category": "compute",
    },
    {
        "name": "get_edge_info",
        "description": (
            "查询指定有向边的详细信息。\n\n"
            "参数:\n"
            "- src_name: 起点节点名称。\n"
            "- dst_name: 终点节点名称。\n\n"
            "返回:\n"
            "- JSON 字符串，包含边是否存在以及边权重、名称、标签、类型等信息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src_name": {"type": "string", "description": "起点节点名称"},
                "dst_name": {"type": "string", "description": "终点节点名称"},
            },
            "required": ["src_name", "dst_name"],
        },
        "category": "compute",
    },
    {
        "name": "check_edge_exists",
        "description": (
            "检查一条有向边是否存在。\n\n"
            "参数:\n"
            "- src_name: 起点节点名称。\n"
            "- dst_name: 终点节点名称。\n\n"
            "返回:\n"
            "- JSON 字符串，包含 exists（是否存在）和 directed（图是否有向）等信息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src_name": {"type": "string", "description": "起点节点名称"},
                "dst_name": {"type": "string", "description": "终点节点名称"},
            },
            "required": ["src_name", "dst_name"],
        },
        "category": "compute",
    },
    {
        "name": "search_node",
        "description": (
            "根据节点名称、文本内容、属性约束或自然语言描述搜索图中的节点，返回可用于其他工具的精确 node_name。"
            "当用户没有给出精确节点名，只提供描述、文本片段、类型、标签或结构约束时，应先调用此工具；"
            "若返回 ambiguous=true，应先向用户确认候选，再调用 get_node_info、shortest_path、subgraph_extraction 等精确工具。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "节点名称、文本片段或自然语言描述。",
                },
                "top_k": {"type": "integer", "default": 8},
                "filters": {
                    "type": "object",
                    "properties": {
                        "node_type": {"type": "string"},
                        "label": {"type": "string"},
                        "min_degree": {"type": "integer"},
                        "max_degree": {"type": "integer"},
                        "min_in_degree": {"type": "integer"},
                        "max_in_degree": {"type": "integer"},
                        "min_out_degree": {"type": "integer"},
                        "max_out_degree": {"type": "integer"},
                    },
                },
                "sort": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": ["score", "node_id", "in_degree", "out_degree", "total_degree"],
                        },
                        "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    },
                },
                "rerank": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
        "category": "compute",
    },
    {
        "name": "search_edge",
        "description": (
            "根据边名称、端点描述、边文本、边属性或自然语言描述搜索图中的边，返回可用于其他工具的精确 src_name / dst_name。"
            "当用户没有给出精确边端点，只描述关系、边文本、边类型或端点语义时，应先调用此工具；"
            "支持用 src_query / dst_query 同时约束端点。query、src_query、dst_query 至少提供一个即可。"
            "若返回 ambiguous=true，应先向用户确认候选。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "边描述、边名称或关系描述。"},
                "src_query": {"type": "string", "description": "可选，起点节点的名称或描述。"},
                "dst_query": {"type": "string", "description": "可选，终点节点的名称或描述。"},
                "top_k": {"type": "integer", "default": 8},
                "filters": {
                    "type": "object",
                    "properties": {
                        "edge_type": {"type": "string"},
                        "label": {"type": "string"},
                        "min_weight": {"type": "number"},
                        "max_weight": {"type": "number"},
                    },
                },
                "sort": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": ["score", "edge_idx", "weight"]},
                        "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    },
                },
                "rerank": {"type": "boolean", "default": True},
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "shortest_path",
        "description": (
            "计算 source 到 target 的最短路径。\n\n"
            "参数:\n"
            "- source: 起点节点。\n"
            '- target: 终点节点；当值为 `"all"` 时，返回 source 到所有可达节点的最短路径。\n'
            "- use_weight(可选, 默认 true):\n"
            "  - true: 按边权重计算（Dijkstra）；\n"
            "  - false: 按跳数计算（BFS）。\n"
            "- algorithm(可选, 默认 \"default\"):\n"
            "  - \"default\": 返回单条最短路径；\n"
            "  - \"yen_k_shortest\": 返回前 k 条不重复的最短简单路径。\n"
            "- k(可选, 默认 3): 仅在 algorithm=\"yen_k_shortest\" 时生效，指定返回的候选路径数。\n\n"
            "返回:\n"
            "- JSON 字符串，包含最短路径节点序列、距离/代价及状态信息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "起点节点"},
                "target": {"type": "string", "description": '终点节点，或 "all" 表示到所有可达节点'},
                "use_weight": {
                    "type": "boolean",
                    "description": "true: 按边权重计算（Dijkstra）；false: 按跳数计算（BFS）",
                    "default": True,
                },
                "algorithm": {
                    "type": "string",
                    "enum": ["default", "yen_k_shortest"],
                    "description": "default: 单条最短路径；yen_k_shortest: 前 k 条候选最短简单路径",
                    "default": "default",
                },
                "k": {
                    "type": "integer",
                    "description": "仅 algorithm=yen_k_shortest 时生效，返回的候选路径数",
                    "default": 3,
                },
            },
            "required": ["source", "target"],
        },
        "category": "compute",
    },
    {
        "name": "all_shortest_paths",
        "description": (
            "计算 source 到 target 的所有等长最短路径。\n\n"
            "参数:\n"
            "- source: 起点节点。\n"
            "- target: 终点节点。\n\n"
            "返回:\n"
            "- JSON 字符串，包含路径数量、最短距离及所有最短路径列表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "起点节点"},
                "target": {"type": "string", "description": "终点节点"},
            },
            "required": ["source", "target"],
        },
        "category": "compute",
    },
    {
        "name": "maximum_flow",
        "description": (
            "计算 source 到 sink 的最大流。\n\n"
            "参数:\n"
            "- source: 源点节点。\n"
            "- sink: 汇点节点。\n\n"
            "返回:\n"
            "- JSON 字符串，包含最大流值和各边上的非零流量分配。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源点节点"},
                "sink": {"type": "string", "description": "汇点节点"},
            },
            "required": ["source", "sink"],
        },
        "category": "compute",
    },
    {
        "name": "minimum_cut",
        "description": (
            "计算 source 到 sink 的最小割。\n\n"
            "参数:\n"
            "- source: 源点节点。\n"
            "- sink: 汇点节点。\n\n"
            "返回:\n"
            "- JSON 字符串，包含最小割值、分区结果和割边列表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源点节点"},
                "sink": {"type": "string", "description": "汇点节点"},
            },
            "required": ["source", "sink"],
        },
        "category": "compute",
    },
    {
        "name": "global_minimum_cut",
        "description": (
            "计算当前图的全局最小割。\n\n"
            "默认将有向图按无向图处理，并使用边权作为割容量；若图本身不连通，返回割值 0。"
            "适合回答“不指定源点/汇点时整张图最脆弱的割在哪里”。\n\n"
            "参数:\n"
            "- treat_as_undirected(可选, 默认 true): 有向图是否按无向图处理；false 时有向图会返回不支持。\n"
            "- limit(可选): 分区/割边预览数量上限，默认 20，最大 200；传 \"all\" 返回全部预览。\n\n"
            "返回:\n"
            "- JSON 字符串，包含全局最小割值、两个分区大小、较小分区预览、割边预览和是否截断。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "treat_as_undirected": {
                    "type": "boolean",
                    "description": "有向图是否按无向图处理，默认 true。",
                    "default": True,
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "返回的分区/割边预览条数上限，默认 20，最大 200；传 \"all\" 返回全部。",
                    "default": 20,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "minimum_spanning_tree",
        "description": (
            "计算当前图的最小生成树（或最小生成森林）。\n\n"
            "返回:\n"
            "- JSON 字符串。\n"
            "  - 连通图: 返回 `mst_edges` 与 `total_weight`；\n"
            "  - 非连通图: 返回各连通分量的最小生成森林信息。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "community_detection",
        "description": (
            "对当前图执行社区检测。\n\n"
            "参数:\n"
            '- algorithm(可选, 默认 "louvain"):\n'
            '  - "louvain": 基于模块度优化的贪心合并，速度快，适合大图；\n'
            '  - "greedy_modularity": 贪心模块度优化（与 louvain 类似，实现不同）；\n'
            '  - "label_propagation": 同步标签传播，速度极快但结果不稳定；\n'
            '  - "label_propagation_asyn": 异步标签传播，支持加权图；\n'
            '  - "leiden": Leiden 算法，模块度通常优于 Louvain；\n'
            '  - "girvan_newman": 基于边介数的分裂算法，精度高但速度慢；\n'
            '  - "asyn_fluidc": 异步流体社区发现，适合小图。\n\n'
            "返回:\n"
            "- JSON 字符串，包含社区数量、模块度（若可计算）及每个社区的节点集合。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "algorithm": {
                    "type": "string",
                    "enum": [
                        "louvain",
                        "greedy_modularity",
                        "label_propagation",
                        "leiden",
                        "girvan_newman",
                        "asyn_fluidc",
                        "label_propagation_asyn",
                    ],
                    "description": (
                        "louvain: 模块度贪心合并(快); greedy_modularity: 贪心模块度; "
                        "label_propagation: 同步标签传播(极快); label_propagation_asyn: 异步标签传播(支持加权); "
                        "leiden: Leiden算法(高质量); girvan_newman: 边介数分裂(慢但精确); "
                        "asyn_fluidc: 流体社区(小图)"
                    ),
                    "default": "louvain",
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "展示的社区数上限，默认 5，最大 200；传 \"all\" 返回全部社区（大图慎用）。",
                    "default": 5,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "node_centrality",
        "description": (
            "计算并排序节点中心性指标。\n\n"
            "参数:\n"
            '- centrality_type(可选, 默认 "degree"):\n'
            '  - "degree": 度中心性，按连接数排序，最简单直观；\n'
            '  - "betweenness": 介数中心性，衡量节点在最短路径上的桥接作用；\n'
            '  - "closeness": 接近中心性，衡量到其他节点的平均距离；\n'
            '  - "pagerank": PageRank，基于随机游走的全局重要性排序；\n'
            '  - "eigenvector": 特征向量中心性，连接重要节点的节点更重要；\n'
            '  - "harmonic": 调和中心性，适合非连通图的可达性度量；\n'
            '  - "katz": Katz 中心性，考虑长路径衰减的全局影响力；\n'
            '  - "load": 负载中心性，基于流量负载的 betweenness 变体；\n'
            '  - "voterank": 迭代投票选取关键传播节点（返回排名列表而非分数）；\n'
            '  - "percolation": 渗透中心性，衡量信息扩散能力。\n\n'
            "返回:\n"
            "- JSON 字符串，包含所选指标类型与节点中心性排名列表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "centrality_type": {
                    "type": "string",
                    "enum": [
                        "degree",
                        "betweenness",
                        "closeness",
                        "pagerank",
                        "eigenvector",
                        "harmonic",
                        "katz",
                        "load",
                        "voterank",
                        "percolation",
                    ],
                    "description": (
                        "degree: 连接数; betweenness: 最短路桥接; closeness: 平均距离; "
                        "pagerank: 随机游走重要性; eigenvector: 邻居质量; harmonic: 非连通图可达性; "
                        "katz: 长路径衰减影响力; load: 流量负载; voterank: 迭代投票; percolation: 扩散能力"
                    ),
                    "default": "degree",
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "返回的 top 节点数，默认 20，最大 200；传 \"all\" 返回全部节点（大图慎用）。",
                    "default": 20,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "graph_connectivity",
        "description": (
            "分析图的连通性结构。\n\n"
            "返回:\n"
            "- JSON 字符串。\n"
            "  - 有向图: 返回强连通/弱连通组件统计；\n"
            "  - 无向图: 返回连通分量统计。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": ["integer", "string"],
                    "description": "展示的连通分量数上限，默认 5，最大 200；传 \"all\" 返回全部分量。",
                    "default": 5,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "find_cycles",
        "description": (
            "检测图中的环。\n\n"
            "返回:\n"
            "- JSON 字符串，包含是否存在环、环总数及部分环示例（超限时截断）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": ["integer", "string"],
                    "description": "返回的环数上限，默认 20，最大 200；传 \"all\" 返回全部环（密集图慎用，枚举可能指数级）。",
                    "default": 20,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "maximum_cycle",
        "description": (
            "查找当前图中的最大简单环。\n\n"
            "默认按环长度（边数/节点数）最大查找；也可按边权总和最大查找。"
            "一般图上的最长简单环需要枚举简单环，可能指数级，因此使用 max_cycles 控制扫描上限。\n\n"
            "返回:\n"
            "- JSON 字符串，包含最大环节点序列、环长度、环总权重、环边列表、已扫描环数和是否截断。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "optimize_by": {
                    "type": "string",
                    "enum": ["length", "weight"],
                    "description": "length: 返回最长环；weight: 返回边权总和最大的环。",
                    "default": "length",
                },
                "max_cycles": {
                    "type": "integer",
                    "description": "最多扫描的简单环数量，默认 10000，最大 100000。密集图慎调大。",
                    "default": 10000,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "topological_sort",
        "description": (
            "对当前图执行拓扑排序。\n\n"
            "返回:\n"
            "- JSON 字符串。\n"
            "  - DAG: 返回 `topological_order`；\n"
            "  - 非 DAG 或无向图: 返回失败原因及可能的环示例。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": ["integer", "string"],
                    "description": "拓扑序预览的节点数上限，默认 20，最大 200；传 \"all\" 返回完整拓扑序。",
                    "default": 20,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "degree_analysis",
        "description": (
            "统计图中各节点的入度/出度分布。\n\n"
            "返回:\n"
            "- JSON 字符串，包含每个节点的度信息、最大入/出度节点和平均入/出度。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": ["integer", "string"],
                    "description": "返回的 top/low 度节点数上限，默认 20，最大 200；传 \"all\" 返回全部节点。",
                    "default": 20,
                },
            },
            "required": [],
        },
        "category": "compute",
    },
    {
        "name": "subgraph_extraction",
        "description": (
            "从当前图中提取子图。\n\n"
            "默认会创建一个新的当前图版本，只包含提取出的子图；之后的图计算都在该子图版本上执行。"
            "如只想预览而不切换当前图，请设置 create_version=false。\n\n"
            "参数:\n"
            '- method:\n'
            '  - "ego_graph": 提取指定节点的 k 跳邻域子图（需要 node 和 k）；\n'
            '  - "k_core": 提取所有度 >= k 的节点构成的子图；\n'
            '  - "k_truss": 提取所有边至少在 k-2 个三角形中的子图（k >= 2）；\n'
            '  - "k_corona": 提取 k-core 中恰好有 k 个核内邻居的节点子图；\n'
            '  - "node_subset": 提取指定节点集合的诱导子图（需要 nodes）。\n'
            "- node(可选): 仅 ego_graph 需要，指定中心节点。\n"
            "- k(可选, 默认 2): ego_graph 中为邻域半径；k_core/k_truss/k_corona 中为度/三角形阈值。\n"
            "- nodes(可选): 仅 node_subset 需要，指定节点名称列表。\n\n"
            "返回:\n"
            "- JSON 字符串，包含子图规模、节点/边预览，以及新图版本号。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["ego_graph", "k_core", "k_truss", "k_corona", "node_subset"],
                    "description": (
                        "ego_graph: k跳邻域(需node+k); k_core: 度>=k子图; "
                        "k_truss: 边在>=k-2个三角形中(k>=2); k_corona: k-core中恰有k个核内邻居; "
                        "node_subset: 指定节点诱导子图(需nodes)"
                    ),
                },
                "node": {"type": "string", "description": "ego_graph 的中心节点（仅 ego_graph 需要）"},
                "k": {"type": "integer", "default": 2, "description": "ego_graph: 邻域半径; k_core/k_truss/k_corona: 阈值"},
                "nodes": {"type": "array", "items": {"type": "string"}, "description": "node_subset 的节点名称列表（仅 node_subset 需要）"},
                "create_version": {
                    "type": "boolean",
                    "description": "是否创建并切换到子图版本，默认 true；false 仅返回预览。",
                    "default": True,
                },
                "description": {
                    "type": "string",
                    "description": "新图版本描述；create_version=true 时生效。",
                },
                "limit": {"type": ["integer", "string"], "description": "返回的节点/边预览条数上限，默认 20，最大 200；传 \"all\" 返回全部。", "default": 20},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "link_prediction_score",
        "description": (
            "计算潜在边的链路预测打分。\n\n"
            "参数:\n"
            '- method:\n'
            '  - "jaccard": Jaccard 系数，共同邻居数 / 邻居并集数；\n'
            '  - "adamic_adar": Adamic-Adar 指数，对共同邻居按度的倒数对数加权；\n'
            '  - "resource_allocation": 资源分配指数，共同邻居度数倒数之和；\n'
            '  - "preferential_attachment": 优先连接，两节点度数之积。\n'
            "- source/target(可选): 同时提供时仅计算该节点对的分数；不传则返回全图 top_k 非边预测结果。\n"
            "- top_k(可选, 默认 10): 不指定 source/target 时返回的候选边数。\n\n"
            "返回:\n"
            "- JSON 字符串，包含节点对及对应预测分数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["jaccard", "adamic_adar", "resource_allocation", "preferential_attachment"],
                    "description": (
                        "jaccard: 共同邻居/并集; adamic_adar: 共同邻居按度倒数对数加权; "
                        "resource_allocation: 共同邻居度倒数之和; preferential_attachment: 度数之积"
                    ),
                },
                "source": {"type": "string", "description": "源节点（可选，与 target 同时提供时仅计算该对）"},
                "target": {"type": "string", "description": "目标节点（可选，与 source 同时提供时仅计算该对）"},
                "top_k": {"type": "integer", "default": 10, "description": "不指定 source/target 时返回的候选边数"},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "path_analysis",
        "description": (
            "路径分析工具。\n\n"
            "参数:\n"
            '- method:\n'
            '  - "all_simple_paths": 枚举 source 到 target 的所有简单路径（需要 source、target，受 cutoff 限制）；\n'
            '  - "has_path": 判断 source 到 target 是否可达（需要 source、target）；\n'
            '  - "average_shortest_path_length": 计算图的平均最短路径长度（不需要 source/target，自动取最大连通分量）。\n'
            "- source(条件必填): all_simple_paths 和 has_path 需要。\n"
            "- target(条件必填): all_simple_paths 和 has_path 需要。\n"
            "- cutoff(可选, 默认 5): 仅 all_simple_paths 生效，限制路径最大长度。\n\n"
            "返回:\n"
            "- JSON 字符串，包含路径列表、可达性布尔值或平均路径长度。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["all_simple_paths", "has_path", "average_shortest_path_length"],
                    "description": (
                        "all_simple_paths: 枚举简单路径(需source+target+cutoff); "
                        "has_path: 判断可达性(需source+target); "
                        "average_shortest_path_length: 图平均最短路径长度(不需source/target)"
                    ),
                },
                "source": {"type": "string", "description": "起点节点（all_simple_paths 和 has_path 必填）"},
                "target": {"type": "string", "description": "终点节点（all_simple_paths 和 has_path 必填）"},
                "cutoff": {"type": "integer", "default": 5, "description": "仅 all_simple_paths 生效，路径最大长度"},
                "limit": {"type": ["integer", "string"], "description": "all_simple_paths 返回路径数上限，默认 10，最大 200；传 \"all\" 返回全部路径（慎用，可能指数级）。", "default": 10},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "clique_analysis",
        "description": (
            "团（clique）分析工具。\n\n"
            "参数:\n"
            '- method:\n'
            '  - "find_cliques": 枚举图中所有极大团（完全子图）；\n'
            '  - "clique_number": 返回最大团的大小（不需要 node）；\n'
            '  - "number_of_cliques": 计算节点参与的团数量。\n'
            "- node(可选): 仅在 method=\"number_of_cliques\" 时有效。\n"
            "  - 提供时: 返回该节点参与的团数量；\n"
            "  - 不提供时: 返回所有节点各自参与的团数量。\n\n"
            "返回:\n"
            "- JSON 字符串，包含团列表、最大团大小或节点团计数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["find_cliques", "clique_number", "number_of_cliques"],
                    "description": (
                        "find_cliques: 枚举所有极大团; "
                        "clique_number: 最大团大小; "
                        "number_of_cliques: 节点参与的团数量(可选node)"
                    ),
                },
                "node": {
                    "type": "string",
                    "description": "仅 number_of_cliques 时有效：指定节点则返回该节点的团数，不指定则返回所有节点的团数",
                },
                "limit": {"type": ["integer", "string"], "description": "返回的团/节点数上限，默认 20，最大 200；传 \"all\" 返回全部（密集图慎用）。", "default": 20},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "reachability",
        "description": (
            "可达性分析工具。\n\n"
            "参数:\n"
            '- method:\n'
            '  - "transitive_closure": 计算传递闭包（返回采样边，最多 200 条，不需要 node）；\n'
            '  - "descendants": 查询从指定节点可达的所有后代节点（需要 node）；\n'
            '  - "ancestors": 查询所有能到达指定节点的祖先节点（需要 node）。\n'
            "- node(条件必填): descendants 和 ancestors 必须提供，transitive_closure 不需要。\n\n"
            "返回:\n"
            "- JSON 字符串，包含传递闭包边列表或后代/祖先节点集合。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["transitive_closure", "descendants", "ancestors"],
                    "description": (
                        "transitive_closure: 传递闭包(不需node,最多200条边); "
                        "descendants: 后代节点(需node); "
                        "ancestors: 祖先节点(需node)"
                    ),
                },
                "node": {
                    "type": "string",
                    "description": "descendants 和 ancestors 必填，transitive_closure 不需要",
                },
                "limit": {"type": ["integer", "string"], "description": "返回节点/边数上限，默认 20，最大 200；传 \"all\" 返回全部（transitive_closure 慎用）。", "default": 20},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "global_metrics",
        "description": (
            "计算图级别统计指标。\n\n"
            "参数:\n"
            '- metric:\n'
            '  - "density": 图密度，实际边数 / 可能的最大边数；\n'
            '  - "diameter": 图直径，最大连通分量中最长的最短路径长度；\n'
            '  - "radius": 图半径，最大连通分量中最小的离心率；\n'
            '  - "transitivity": 全局传递性（聚类系数），三角形密度；\n'
            '  - "average_clustering": 平均局部聚类系数；\n'
            '  - "degree_assortativity": 度同配性，相连节点度数的相关系数。\n\n'
            "注意: diameter 和 radius 在非连通图上自动取最大连通分量计算。\n\n"
            "返回:\n"
            "- JSON 字符串，包含指标名称和计算结果。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["density", "diameter", "radius", "transitivity", "average_clustering", "degree_assortativity"],
                    "description": (
                        "density: 边密度; diameter: 最长最短路径(取最大连通分量); "
                        "radius: 最小离心率(取最大连通分量); transitivity: 三角形密度; "
                        "average_clustering: 平均局部聚类系数; degree_assortativity: 度相关性"
                    ),
                }
            },
            "required": ["metric"],
        },
        "category": "compute",
    },
    {
        "name": "list_datasets",
        "description": "列出所有已注册数据集。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "category": "data",
    },
    {
        "name": "load_dataset",
        "description": "从数据集加载图到当前会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string"},
                "version": {"type": "string", "default": "latest"},
            },
            "required": ["dataset_id"],
        },
        "category": "data",
    },
    {
        "name": "save_as_dataset",
        "description": "将当前会话图保存为一个数据集。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
        "category": "data",
    },
    {
        "name": "load_graph_from_text",
        "description": (
            "从自然语言文本中通过 LLM 抽取实体关系并构建图到当前会话。\n"
            "支持长文本自动分块、逐块抽取、去重和规范化。\n"
            "可传入 text（内联文本）或 filename（已上传文件名）之一，避免 LLM 重复输出文件内容。\n"
            "返回摘要统计，不返回中间完整三元组。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "直接传入的文本内容（与 filename 二选一）。",
                },
                "filename": {
                    "type": "string",
                    "description": "已上传文件名（与 text 二选一），工具自行读取文件内容，避免 LLM 重复输出大段文本。",
                },
                "directed": {"type": "boolean", "default": True},
                "append": {"type": "boolean", "default": False},
                "extraction_goal": {
                    "type": "string",
                    "description": "可选，指导抽取方向，例如「只抽取人物和公司关系」。",
                },
            },
            "required": [],
        },
        "category": "data",
    },
    {
        "name": "load_graph_from_triples",
        "description": "从结构化实体关系三元组构建图并加载到当前会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "triples": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "relation": {"type": "string"},
                            "target": {"type": "string"},
                            "confidence": {"type": "number"},
                            "evidence": {"type": "string"},
                            "chunk_id": {"type": "integer"},
                        },
                        "required": ["source", "relation", "target"],
                    },
                },
                "directed": {"type": "boolean", "default": True},
                "append": {"type": "boolean", "default": False},
            },
            "required": ["triples"],
        },
        "category": "data",
    },
    {
        "name": "upload_document",
        "description": "将文档加入当前会话的轻量知识库。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": [],
        },
        "category": "data",
    },
    {
        "name": "retrieve_context",
        "description": "对当前会话已上传文档执行语义检索。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        "category": "compute",
    },
    {
        "name": "get_job_status",
        "description": "查询异步作业状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        "category": "system",
    },
    {
        "name": "cancel_job",
        "description": "取消一个异步作业。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        "category": "system",
    },
    # ── 第 4 批新增（图可视化）─────────────────────────────────────────
    {
        "name": "visualize_subgraph",
        "description": (
            "获取图的可视化数据，支持高亮指定节点、路径和子图。\n\n"
            "当用户意图涉及图可视化、图展示、查看图结构时，应调用此工具。\n"
            "模型可根据上下文自动填充 highlights，例如：\n"
            "- 用户问最短路径时，highlights.paths 中填入最短路径节点序列\n"
            "- 用户问图直径时，将 graph_diameter 返回的 diameter_path 填入 highlights.paths\n"
            "- 用户问某子图/官能团时，highlights.subgraphs 中填入相关节点\n"
            "- 用户问某节点特征时，highlights.nodes 中填入该节点\n\n"
            "参数:\n"
            "- node_names(可选): 指定节点列表；为空则返回全图。\n"
            "- include_community(可选): 是否包含社区信息，默认 false。\n"
            "- max_nodes(可选): 返回的最大节点数，默认 200。超出时优先保留高亮节点，\n"
            "  其余随机采样补足，结果中含 truncated=true 及 total_nodes 字段。\n"
            "  图规模较大时，可减小该值以避免上下文膨胀；需完整展示时可适当调大。\n"
            "- highlights(可选): 高亮信息，统一格式:\n"
            "  {\n"
            '    "nodes": [{"ids": ["A","B"], "color": "#hex", "label": "关键节点"}],\n'
            '    "paths": [{"node_sequence": ["A","B","C"], "color": "#hex", "label": "最短路径"}],\n'
            '    "subgraphs": [{"nodes": ["A","B","C","D"], "color": "#hex", "label": "官能团"}]\n'
            "  }\n"
            "  color 和 label 可选，系统会自动分配颜色。\n\n"
            "返回:\n"
            "- JSON 字符串，包含节点、边、高亮信息及元数据。\n"
            "- 若节点被截断，结果含 truncated=true、total_nodes（图总节点数）、truncated_to（实际返回上限）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "节点名称列表（可选，为空则返回全图）",
                },
                "include_community": {
                    "type": "boolean",
                    "description": "是否包含社区信息（默认 false）",
                    "default": False,
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "返回的最大节点数，默认 200。超出时优先保留高亮节点，其余随机采样。",
                    "default": 200,
                },
                "highlights": {
                    "type": "object",
                    "description": "高亮信息（可选），包含 nodes/paths/subgraphs 三类",
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "description": "高亮的节点组",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "要高亮的节点 ID 列表",
                                    },
                                    "color": {"type": "string", "description": "高亮颜色（可选，如 #ef4444）"},
                                    "label": {"type": "string", "description": "高亮标签说明（可选）"},
                                },
                                "required": ["ids"],
                            },
                        },
                        "paths": {
                            "type": "array",
                            "description": "高亮的路径",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "node_sequence": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "路径上的节点序列（按顺序）",
                                    },
                                    "color": {"type": "string", "description": "高亮颜色（可选）"},
                                    "label": {"type": "string", "description": "路径标签说明（可选）"},
                                },
                                "required": ["node_sequence"],
                            },
                        },
                        "subgraphs": {
                            "type": "array",
                            "description": "高亮的子图",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "nodes": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "子图包含的节点列表",
                                    },
                                    "color": {"type": "string", "description": "高亮颜色（可选）"},
                                    "label": {"type": "string", "description": "子图标签说明（可选）"},
                                },
                                "required": ["nodes"],
                            },
                        },
                    },
                },
            },
            "required": [],
        },
        "category": "data",
    },
]

# ─────────────────────────────────────────────────────────────────
# GraphML 工具（由 GraphDataService 内置 graph_ml 子模块执行）
# ─────────────────────────────────────────────────────────────────

_GNN_ALGO_CONFIG_SCHEMA: dict = {
    "type": "object",
    "description": "算法配置（可选）。GNN 任务支持结构参数；pretrain method=node2vec 时使用 Node2Vec 参数。",
    "properties": {
        "device": {
            "type": "string",
            "enum": ["auto", "cpu", "cuda"],
            "description": "训练设备，默认 auto。",
            "default": "auto",
        },
        "hidden_dim": {"type": "integer", "description": "GNN 隐藏层维度。", "default": 64},
        "node_embedding_dim": {
            "type": "integer",
            "description": "图分类节点类别 Embedding 维度。",
            "default": 64,
        },
        "embedding_dim": {"type": "integer", "description": "输出 embedding 维度。", "default": 64},
        "num_layers": {"type": "integer", "description": "GNN 层数，最少 2。", "default": 2},
        "dropout": {"type": "number", "description": "dropout 比例。", "default": 0.2},
        "gat_heads": {
            "type": "integer",
            "description": "GAT 多头数，仅 model=GAT 时生效。",
            "default": 2,
        },
        "use_residual_connection": {
            "type": "boolean",
            "description": "是否在每个 GNN 层启用残差连接；维度不一致时自动线性投影。",
            "default": False,
        },
        "use_normalization": {
            "type": "boolean",
            "description": "是否在每个 GNN 层启用归一化。",
            "default": False,
        },
        "norm_pos": {
            "type": "string",
            "enum": ["pre", "post"],
            "description": "归一化位置；pre 表示卷积前，post 表示卷积/残差后。",
            "default": "post",
        },
        "norm_type": {
            "type": "string",
            "enum": ["rms_norm", "layer_norm", "batch_norm"],
            "description": "归一化类型。",
            "default": "layer_norm",
        },
        "walk_length": {"type": "integer", "description": "Node2Vec 随机游走长度。", "default": 20},
        "context_size": {"type": "integer", "description": "Node2Vec 上下文窗口大小。", "default": 10},
        "walks_per_node": {"type": "integer", "description": "Node2Vec 每节点游走次数。", "default": 10},
        "num_negative_samples": {"type": "integer", "description": "Node2Vec 负采样数。", "default": 1},
        "batch_size": {"type": "integer", "description": "Node2Vec loader batch size。", "default": 128},
        "p": {"type": "number", "description": "Node2Vec 返回参数 p。", "default": 1.0},
        "q": {"type": "number", "description": "Node2Vec 入出参数 q。", "default": 1.0},
    },
    "additionalProperties": True,
}

_TRAIN_DATA_CONFIG_SCHEMA: dict = {
    "type": "object",
    "description": "训练数据配置。",
    "properties": {
        "feature_source": {
            "type": "string",
            "enum": ["graph", "embedding"],
            "description": "特征来源，默认 graph；使用 embedding 时 upstream_model_id 必填。",
            "default": "graph",
        },
        "target_nodes": {
            "type": "array",
            "description": "节点分类训练节点子集（可选）。",
            "items": {"type": "string"},
        },
        "target_edges": {
            "type": "array",
            "description": "链接预测正样本边子集（可选）。",
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "dataset_id": {
            "type": "string",
            "description": (
                "图分类数据集 ID；未传时使用当前会话通过 "
                "load_graph_classification_dataset 绑定的数据集。"
            ),
        },
    },
    "additionalProperties": False,
}

_TRAIN_CONFIG_SCHEMA: dict = {
    "type": "object",
    "description": "训练超参配置。",
    "properties": {
        "split": {
            "type": "object",
            "description": "训练/验证划分比例。",
            "properties": {
                "train": {"type": "number", "description": "训练集比例。", "default": 0.8},
                "val": {"type": "number", "description": "验证集比例。", "default": 0.2},
                "test": {
                    "type": "number",
                    "description": "兼容旧请求字段；当前训练流程不使用 test 划分。",
                },
            },
            "additionalProperties": True,
        },
        "epochs": {"type": "integer", "description": "训练 epoch 数。", "default": 200},
        "lr": {"type": "number", "description": "学习率。", "default": 0.05},
        "weight_decay": {"type": "number", "description": "权重衰减。", "default": 0.0},
        "seed": {"type": "integer", "description": "随机种子。", "default": 42},
        "early_stop_patience": {
            "type": "integer",
            "description": "早停耐心轮次；<=0 表示关闭。",
            "default": 0,
        },
        "early_stop_min_delta": {
            "type": "number",
            "description": "早停最小提升阈值。",
            "default": 0.0,
        },
        "batch_size": {
            "type": "integer",
            "description": "图分类 mini-batch 大小；现有单图任务忽略。",
            "default": 64,
            "minimum": 1,
        },
        "num_workers": {
            "type": "integer",
            "description": "图分类 DataLoader worker 数，默认 0。",
            "default": 0,
            "minimum": 0,
        },
    },
    "additionalProperties": True,
}

_INFER_QUERY_SCHEMA: dict = {
    "type": "object",
    "description": "推理输入。不同任务使用不同字段，未使用字段可省略。",
    "properties": {
        "nodes": {
            "type": "array",
            "description": "节点分类或 embedding 查询的节点名列表。",
            "items": {"type": "string"},
        },
        "split": {
            "type": "string",
            "enum": ["train", "val", "test"],
            "description": "节点分类或图分类按划分集合推理。",
        },
        "graph_indices": {
            "type": "array",
            "description": "图分类指定数据集内的图索引。",
            "items": {"type": "integer", "minimum": 0},
        },
        "edges": {
            "type": "array",
            "description": "链接预测指定边对。",
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "source_node": {"type": "string", "description": "链接预测源节点。"},
        "candidate_nodes": {
            "type": "array",
            "description": "链接预测候选目标节点列表。",
            "items": {"type": "string"},
        },
        "topk": {"type": "integer", "description": "返回前 k 个候选。", "default": 10},
    },
    "additionalProperties": True,
}

_INFER_CONFIG_SCHEMA: dict = {
    "type": "object",
    "description": "推理配置。不同任务使用不同字段，未使用字段可省略。",
    "properties": {
        "return_prob": {"type": "boolean", "description": "是否返回概率。", "default": True},
        "batch_size": {
            "type": "integer",
            "description": "图分类推理 mini-batch 大小。",
            "default": 128,
            "minimum": 1,
        },
        "device": {
            "type": "string",
            "enum": ["auto", "cpu", "cuda"],
            "description": "图分类推理设备，默认 auto。",
            "default": "auto",
        },
        "threshold": {
            "type": "number",
            "description": "图分类二分类概率阈值。",
            "default": 0.5,
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "top_k_tasks": {
            "type": "integer",
            "description": "PCBA 每张图默认返回的最高概率任务数。",
            "default": 5,
            "minimum": 1,
        },
        "return_all_task_probs": {
            "type": "boolean",
            "description": "PCBA 是否返回完整 128 维概率、预测和真实标签数组。",
            "default": False,
        },
        "allow_version_mismatch": {
            "type": "boolean",
            "description": "是否允许模型图版本与当前图版本不一致。",
            "default": False,
        },
        "sample_size": {
            "type": "integer",
            "description": "节点分类 query 为空时随机返回的节点预测样例数量。",
            "default": 20,
        },
        "sample_seed": {"type": "integer", "description": "抽样随机种子。", "default": 42},
        "result_limit": {
            "type": "integer",
            "description": "指定节点或 split 推理时最多返回的预测明细数。",
            "default": 20,
            "minimum": 0,
        },
        "result_offset": {
            "type": "integer",
            "description": "指定节点或 split 推理时的分页偏移量。",
            "default": 0,
            "minimum": 0,
        },
        "topk": {
            "type": "integer",
            "description": "链接预测默认返回候选数量。",
            "default": 20,
        },
        "max_candidate_pairs": {
            "type": "integer",
            "description": "链接预测 query 为空时最多评估的候选非边数量。",
            "default": 200000,
        },
        "limit": {
            "type": "integer",
            "description": "pretrain/embedding 推理未指定 nodes 时的返回条数上限。",
            "default": 200,
        },
    },
    "additionalProperties": True,
}

GRAPH_ML_TOOLS: List[dict] = [
    {
        "name": "load_graph_classification_dataset",
        "description": (
            "从当前会话已注册的上传文件中加载多图图分类数据集。\n\n"
            "该工具专用于 HIV/PCBA 等多图 `.pt` 文件；普通单图仍使用 "
            "`load_graph_from_upload`。\n\n"
            "参数:\n"
            "- filename: 当前会话中已注册的上传文件名，不接受任意路径。\n"
            "- dataset_profile: auto / hiv / pcba，默认 auto。\n\n"
            "返回:\n"
            "- 异步 job_id；完成后结果包含数据集摘要和图分类 readiness。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "当前会话中已注册的多图 .pt 文件名",
                    "minLength": 1,
                },
                "dataset_profile": {
                    "type": "string",
                    "enum": ["auto", "hiv", "pcba"],
                    "description": "数据集契约，默认自动识别。",
                    "default": "auto",
                },
            },
            "required": ["filename"],
            "additionalProperties": False,
        },
        "category": "graph_ml",
    },
    {
        "name": "visualize_model_architecture",
        "description": (
            "可视化模型结构（返回 Mermaid 与层级摘要）。\n\n"
            "参数:\n"
            "- model_id: 已训练模型 ID，使用模型 meta 进行可视化。\n\n"
            "返回:\n"
            "- JSON 字符串，包含 Mermaid 图、结构摘要与关键超参。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "已训练模型 ID"},
            },
            "required": ["model_id"],
        },
        "category": "graph_ml",
    },
    {
        "name": "train",
        "description": (
            "发起图机器学习训练任务（GraphML）。\n\n"
            "参数:\n"
            "- task: 任务类型，可选 `node_classification` / `link_prediction` / "
            "`graph_classification` / `pretrain`。\n"
            "- model(可选): 模型名称。\n"
            "- method(可选): 训练方法或 pipeline 名称。\n"
            "- graph_version(可选): 指定图版本；不传则由服务端决定默认版本。\n"
            "- upstream_model_id(可选): 上游模型 ID，用于增量训练或迁移学习。\n"
            "- data(可选): 训练数据配置（字典）。\n"
            "- train_config(可选): 训练超参配置（字典）。\n"
            "- algo_config(可选): 算法特定配置（字典）。\n\n"
            "返回:\n"
            "- JSON 字符串，包含任务提交结果、模型信息、状态或错误详情。\n"
            "图分类必须先调用 load_graph_classification_dataset，不能把多图 .pt "
            "交给 load_graph_from_upload，也不接受自定义 split/metric。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "enum": [
                        "node_classification",
                        "link_prediction",
                        "graph_classification",
                        "pretrain",
                    ],
                    "description": "任务类型",
                },
                "model": {"type": "string", "description": "模型名称（可选）"},
                "method": {"type": "string", "description": "训练方法（可选）"},
                "graph_version": {"type": "integer", "description": "图版本（可选）"},
                "upstream_model_id": {"type": "string", "description": "上游模型 ID（可选）"},
                "data": _TRAIN_DATA_CONFIG_SCHEMA,
                "train_config": _TRAIN_CONFIG_SCHEMA,
                "algo_config": _GNN_ALGO_CONFIG_SCHEMA,
            },
            "required": ["task"],
        },
        "category": "graph_ml",
    },
    {
        "name": "infer",
        "description": (
            "使用已训练模型执行推理（GraphML）。\n\n"
            "参数:\n"
            "- model_id: 已训练模型 ID。\n"
            "- graph_version(可选): 指定推理使用的图版本。\n"
            "- query(可选): 推理输入内容（字典，如目标节点/边或批量查询参数）。\n"
            "- infer_config(可选): 推理配置（字典，如阈值、批大小、后处理选项）。\n\n"
            "返回:\n"
            "- 节点分类若 query 指定 nodes/split，返回该集合 evaluation 指标，并最多返回 20 条 predictions；超出会给出截断提示。\n"
            "- 链接预测若 query 指定 edges/source_node，返回指定对象的 predictions。\n"
            "- 若 query 为空，返回 evaluation 指标摘要和少量 sample/top 预测结果。"
            "\n- 图分类支持 query.split 或 query.graph_indices；完整 split 指标不受"
            " result_limit 分页影响，PCBA 完整 128 维概率需显式开启"
            " return_all_task_probs。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "已训练模型 ID"},
                "graph_version": {"type": "integer", "description": "图版本（可选）"},
                "query": _INFER_QUERY_SCHEMA,
                "infer_config": _INFER_CONFIG_SCHEMA,
            },
            "required": ["model_id"],
        },
        "category": "graph_ml",
    },
]

# ─────────────────────────────────────────────────────────────────
# 内部工具（不暴露给 LLM，仅用于会话管理）
# ─────────────────────────────────────────────────────────────────

INTERNAL_TOOLS: List[dict] = [
    {
        "name": "register_session",
        "description": "将当前 MCP 会话绑定到业务会话 ID，并在 GraphData 侧创建会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_session_id": {"type": "string", "description": "业务会话 ID"},
            },
            "required": ["agent_session_id"],
        },
        "category": "internal",
    },
    {
        "name": "delete_graph_session",
        "description": "删除当前 MCP 会话关联的 GraphData 会话并解除绑定。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "category": "internal",
    },
]


def get_all_public_tools(include_graph_ml: bool = True) -> List[dict]:
    """返回对 LLM 暴露的工具规格列表。"""
    tools = list(GRAPH_TOOLS)
    if include_graph_ml:
        tools.extend(GRAPH_ML_TOOLS)
    return tools

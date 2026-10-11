"""
图工具规格唯一真相源（tool catalog）。

所有下游消费方（MCP Server、TextProtocolDriver、NativeFCDriver、execute_tool 分发）
都从这里读取工具规格，不再各自维护副本。

新增工具步骤：
  1. 在 graph_data_service/compute.py 实现算法
  2. 在此文件 GRAPH_TOOLS 列表末尾添加一条规格
  3. 在 graph_engine/tool_execution.py 声明读写资源；未经分类的工具按屏障执行
  4. 完成 —— MCP 注册、system prompt、native FC 参数全部自动生效
"""

from __future__ import annotations

import os
from typing import List

from graph_engine.construction_catalog import CONSTRUCTION_TOOLS
from graph_engine.finance_catalog import FINANCE_TOOLS
from graph_engine.tool_execution import get_tool_execution_metadata
from graph_engine.analysis_policy import MULTIGRAPH_POLICIES

_REPORTING_TOOLS_ENV = "MCP_ENABLE_REPORTING_TOOLS"
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}
_APPEND_COMPATIBILITY = (
    "\n追加模式仅支持普通图合并。当前图或待合并图包含构图属性图时，"
    "返回 UNSUPPORTED_PROPERTY_GRAPH_APPEND 并保留原图。"
    "此时向用户说明限制；覆盖或清空图需要用户明确要求。"
)
try:
    _LARGE_GRAPH_MAX_SAMPLE_SOURCES = max(
        1, int(os.getenv("LARGE_GRAPH_MAX_SAMPLE_SOURCES", "10000"))
    )
except (TypeError, ValueError, OverflowError):
    _LARGE_GRAPH_MAX_SAMPLE_SOURCES = 10_000

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
            "- 图规模/结构详情请调用 `get_graph_info`。" + _APPEND_COMPATIBILITY
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
            "- 达到大图字节、边数或节点基数阈值的边列表/三元组 → 在全量读取前自动提交流式大图准备；\n"
            "- 自然语言文本（散文、文章、文档）→ LLM 预检提取 extraction_goal 后提交异步 text2graph 作业；\n"
            "- GRAPH_STORAGE_BACKEND=nebula 时，NPZ/PT/JSON 解析及大图 Importer 导入也作为后台作业执行。\n\n"
            "格式由 LLM 预检（FileFormatPreCheckSubAgent）自动判断，无需手动指定；LLM 不可用时降级为启发式检测。\n\n"
            "参数:\n"
            "- filename: 会话中已注册的上传文件名。\n"
            "- directed(可选, 默认 true): 文本边表按有向图或无向图加载。\n"
            "- append(可选, 默认 false): false 覆盖当前图；true 追加到当前图。\n\n"
            "返回:\n"
            "- local 模式的 binary/edge_list/triple：加载状态、检测格式、图版本；\n"
            "- freetext、graph_classification_dataset、超大图准备或 nebula 模式的 binary："
            "async=true、job_id、status='queued'；Agent 会在后端自动监控，直接 API 调用方可低频"
            "使用 get_job_status 查询到终态，再用 get_job_result 读取有界结果，期间不得重复提交；\n"
            "- 图规模/结构详情请调用 `get_graph_info`。" + _APPEND_COMPATIBILITY
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
                "directed": {
                    "type": "boolean",
                    "description": "文本边表是否表示有向图",
                    "default": True,
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
        "name": "get_neighbors",
        "description": (
            "查询指定节点的一跳邻居。Nebula 主存储模式使用原生 GO 查询，"
            "本地模式读取当前 GraphData。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "节点名称或 ID"},
                "direction": {
                    "type": "string",
                    "enum": ["out", "in", "both"],
                    "default": "out",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "返回上限，服务端限制为 1 到 200",
                },
            },
            "required": ["node_name"],
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
            "  - false: 按跳数计算（单目标大图使用双向 BFS）。\n"
            "  - 大图快照没有显式权重时，所有边成本视为 1，单目标自动使用双向 BFS。\n"
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
                    "description": (
                        "true: 按边权重计算；大图没有显式权重时按单位成本使用双向 BFS；"
                        "false: 按跳数计算，单目标大图使用双向 BFS"
                    ),
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
                "direction": {
                    "type": "string",
                    "enum": ["out", "in", "both"],
                    "default": "out",
                    "description": "Nebula 主存储且 use_weight=false 时的遍历方向。",
                },
                "max_steps": {
                    "type": "integer",
                    "default": 10,
                    "description": "Nebula 原生非加权查询的最大跳数，限制为 1 到 20。",
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
                "direction": {"type": "string", "enum": ["out", "in", "both"], "default": "out"},
                "max_steps": {"type": "integer", "default": 10, "description": "Nebula 模式物化的最大跳数，限制为 1 到 20。"},
                "max_nodes": {"type": "integer", "description": "Nebula 子图物化节点上限。"},
                "max_edges": {"type": "integer", "description": "Nebula 子图物化边上限。"},
            },
            "required": ["source", "target"],
        },
        "category": "compute",
    },
    {
        "name": "maximum_flow",
        "description": (
            "计算 source 到 sink 的最大流。\n\n"
            "边权作为容量，容量必须为非负有限数值。\n\n"
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
            "边权作为容量，容量必须为非负有限数值。\n\n"
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
            "默认将有向图按无向图处理，并使用非负边权作为割容量；若图本身不连通，返回割值 0。"
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
            "传入 focus_nodes 时，还会返回这些节点的社区 ID、社区规模和社区代表节点；"
            "适合把高入度/高中心性节点与一次全图社区检测结果直接关联。"
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
                "focus_nodes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 200,
                    "description": (
                        "需要查询社区归属的节点列表；不会为每个节点重复运行社区检测。"
                    ),
                },
                "max_iterations": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 5,
                    "description": "大图加权标签传播的最大迭代次数。",
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
                "node": {
                    "type": "string",
                    "description": "可选；Nebula 主存储下指定节点时直接查询该节点入度/出度，不物化整图。",
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "返回的 top/low 度节点数上限，默认 20，最大 200；传 'all' 返回全部节点。",
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
                "direction": {"type": "string", "enum": ["out", "in", "both"], "default": "out", "description": "ego_graph 在 Nebula 中的遍历方向。"},
                "max_nodes": {"type": "integer", "description": "Nebula 子图物化节点上限。"},
                "max_edges": {"type": "integer", "description": "Nebula 子图物化边上限。"},
                "limit": {"type": ["integer", "string"], "description": "返回的节点/边预览条数上限，默认 20，最大 200；传 'all' 返回全部。", "default": 20},
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
            '  - "average_shortest_path_length": 计算图的平均最短路径长度（不需要 source/target；有向图要求强连通，无向非连通图取最大连通分量）。\n'
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
                "direction": {"type": "string", "enum": ["out", "in", "both"], "default": "out"},
                "max_steps": {"type": "integer", "default": 10, "description": "Nebula 模式物化的最大跳数，限制为 1 到 20。"},
                "max_nodes": {"type": "integer", "description": "Nebula 子图物化节点上限。"},
                "max_edges": {"type": "integer", "description": "Nebula 子图物化边上限。"},
                "limit": {"type": ["integer", "string"], "description": "all_simple_paths 返回路径数上限，默认 10，最大 200；传 'all' 返回全部路径（慎用，可能指数级）。", "default": 10},
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
                "max_steps": {"type": "integer", "default": 10, "description": "descendants/ancestors 在 Nebula 中遍历的最大跳数，限制为 1 到 20。"},
                "max_nodes": {"type": "integer", "description": "Nebula 子图物化节点上限。"},
                "max_edges": {"type": "integer", "description": "Nebula 子图物化边上限。"},
                "limit": {"type": ["integer", "string"], "description": "返回节点/边数上限，默认 20，最大 200；传 'all' 返回全部（transitive_closure 慎用）。", "default": 20},
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
            "- JSON 字符串，包含指标名称和计算结果。\n"
            "- success=true 且 defined=false 表示指标在当前图上数学未定义，"
            "不表示引擎不支持；必须依据 undefined_reason 解释，禁止表述为"
            "“当前不支持”。度同配性常见原因是边端点度数方差为 0。"
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
        "name": "graph_structure_analysis",
        "description": (
            "图结构判定与遍历工具。支持树/森林/DAG/AT-free/锦标赛/二部图判定，"
            "BFS/DFS 树、字典序及全部拓扑序、平面性检查、竞赛得分序列和 Weisfeiler-Lehman 图哈希。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": [
                        "is_tree", "is_forest", "is_directed_acyclic_graph",
                        "bfs_tree", "dfs_tree", "lexicographical_topological_sort",
                        "all_topological_sorts", "is_at_free", "check_planarity",
                        "is_tournament", "score_sequence", "is_bipartite",
                        "weisfeiler_lehman_graph_hash",
                    ],
                },
                "source": {"type": "string", "description": "BFS/DFS 的起点。"},
                "depth_limit": {"type": "integer", "minimum": 0, "description": "BFS/DFS 最大深度。"},
                "iterations": {"type": "integer", "minimum": 1, "default": 3, "description": "WL 哈希迭代次数。"},
                "limit": {"type": ["integer", "string"], "default": 20, "description": "预览上限，最大 200；可传 all。"},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "connectivity_analysis",
        "description": (
            "高级连通性分析。支持点/边连通度、最小点割/边割、割点、桥、局部桥、"
            "点双连通边分量、桥分量和有向图强连通缩点图。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": [
                        "node_connectivity", "edge_connectivity", "minimum_node_cut",
                        "minimum_edge_cut", "articulation_points", "bridges", "local_bridges",
                        "biconnected_component_edges", "bridge_components", "condensation",
                    ],
                },
                "source": {"type": "string", "description": "局部连通度/割集起点；与 target 同时提供。"},
                "target": {"type": "string", "description": "局部连通度/割集终点；与 source 同时提供。"},
                "limit": {"type": ["integer", "string"], "default": 20, "description": "预览上限，最大 200；可传 all。"},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "tree_dag_analysis",
        "description": (
            "树、DAG 与支配关系分析。支持最大/随机生成树、最近公共祖先、即时支配点、"
            "支配边界、DAG 最长路、反链、非周期性以及 collider/v-structure 检测。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": [
                        "maximum_spanning_tree", "random_spanning_tree", "lowest_common_ancestor",
                        "immediate_dominators", "dominance_frontiers", "dag_longest_path",
                        "dag_longest_path_length", "antichains", "is_aperiodic", "colliders",
                        "v_structures",
                    ],
                },
                "source": {"type": "string", "description": "支配关系分析的起点。"},
                "node1": {"type": "string", "description": "最近公共祖先的第一个节点。"},
                "node2": {"type": "string", "description": "最近公共祖先的第二个节点。"},
                "seed": {"type": "integer", "default": 42, "description": "随机生成树种子。"},
                "limit": {"type": ["integer", "string"], "default": 20, "description": "预览上限，最大 200；可传 all。"},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "matching_coloring_analysis",
        "description": "匹配、边覆盖和图着色工具，支持最大/最小权匹配、极大匹配、最小边覆盖、匹配校验、贪心着色与均衡着色。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": [
                        "max_weight_matching", "min_weight_matching", "maximal_matching",
                        "min_edge_cover", "is_matching", "greedy_color", "equitable_color",
                    ],
                },
                "edges": {"type": "array", "items": {"type": ["array", "object"]}, "description": "is_matching 待校验边集。"},
                "maxcardinality": {"type": "boolean", "default": False, "description": "最大权匹配是否优先最大基数。"},
                "strategy": {"type": "string", "default": "largest_first", "description": "greedy_color 的 NetworkX 着色策略。"},
                "num_colors": {"type": "integer", "minimum": 1, "description": "equitable_color 颜色数。"},
                "limit": {"type": ["integer", "string"], "default": 20, "description": "预览上限，最大 200；可传 all。"},
            },
            "required": ["method"],
        },
        "category": "compute",
    },
    {
        "name": "distance_measures",
        "description": (
            "扩展距离与结构度量。支持中心/边缘/离心率、Wiener 指数、全局/局部效率、"
            "平均邻居度、平均度连通性、富俱乐部系数、互惠性、流层级、重心、Estrada 指数和围长。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": [
                        "center", "periphery", "eccentricity", "wiener_index",
                        "global_efficiency", "local_efficiency", "average_neighbor_degree",
                        "average_degree_connectivity", "rich_club_coefficient", "reciprocity",
                        "flow_hierarchy", "barycenter", "estrada_index", "girth",
                    ],
                },
                "limit": {"type": ["integer", "string"], "default": 20, "description": "预览上限，最大 200；可传 all。"},
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
            "返回摘要统计，不返回中间完整三元组。" + _APPEND_COMPATIBILITY
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
        "description": "从结构化实体关系三元组构建图并加载到当前会话。" + _APPEND_COMPATIBILITY,
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
        "description": (
            "查询异步作业的有界状态与进度。传 after_seq=0 开始独立读取完整进度，再用 "
            "progress_cursor 读取后续页；progress_has_more=true 时继续读取。省略游标保留旧增量队列行为。"
            "大图作业状态不内嵌完整结果，完成后使用 get_job_result。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "after_seq": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                "progress_limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["job_id"],
        },
        "category": "system",
    },
    {
        "name": "get_job_result",
        "description": (
            "获取已完成异步作业的结果。大图作业只返回 Summary、Preview 和 Artifact 引用，"
            "不会把全量节点结果加载进 Agent 上下文。"
        ),
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
    # ── 超大图分区计算路径 ──────────────────────────────────────────
    {
        "name": "prepare_large_graph",
        "description": (
            "将当前会话已注册的边列表/三元组上传文件分块准备为版本化大图快照。"
            "任务会持久化排队，在独立 Worker 进程中并行读取、校验和跨分区去重；"
            "不会创建完整 GraphData、节点列表、边列表或 NetworkX 图。\n\n"
            "返回 async=true、job_id 和唯一 graph_version；用 get_job_status 查看进度，"
            "用 get_job_result 获取质量摘要。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "已注册上传文件名"},
                "graph_id": {"type": "string", "description": "逻辑图标识；默认当前 session_id"},
                "graph_version": {"type": "integer", "minimum": 1, "description": "可选显式版本；不可覆盖"},
                "input_format": {
                    "type": "string", "enum": ["edgelist", "triples"], "default": "edgelist"
                },
                "delimiter": {
                    "type": "string", "description": "auto/whitespace/comma/tab 或单字符", "default": "auto"
                },
                "directed": {"type": "boolean", "default": True},
                "partitions": {"type": "integer", "minimum": 1, "maximum": 128},
                "workers": {"type": "integer", "minimum": 1},
                "quality_policy": {
                    "type": "object",
                    "description": "显式节点规范化、边唯一键、权重冲突、自环和无效记录策略",
                    "properties": {
                        "node_normalization": {"type": "string", "enum": ["nfkc_casefold", "exact"]},
                        "edge_key": {
                            "type": "string",
                            "enum": ["endpoints", "endpoints_relation"],
                            "default": "endpoints",
                            "description": (
                                "算法兼容快照使用 endpoints；endpoints_relation 保留多关系并行边，"
                                "当前经典算法会明确拒绝该语义。"
                            ),
                        },
                        "weight_conflict": {"type": "string", "enum": ["first", "last", "max", "min", "reject"]},
                        "self_loops": {"type": "string", "enum": ["allow", "skip", "reject"]},
                        "invalid_records": {"type": "string", "enum": ["skip", "reject"]},
                    },
                },
            },
            "required": ["filename"],
        },
        "category": "data",
    },
    {
        "name": "run_large_graph_algorithm",
        "description": (
            "在已发布的分区快照上提交算法。能力目录跟踪小图计算目录的全部算法，另保留"
            " degree/in_degree/out_degree/degree_centrality 快速入口；每个方法的"
            " 参数、条件必填项和子模式见 parameters Schema，也可先调用"
            " get_large_graph_capabilities 获取运行时能力。不支持的模式或资源不足会"
            "明确拒绝，不回退到无界 NetworkX 路径。dev 新增的结构、连通性、树/DAG、"
            "匹配/着色和扩展距离分组算子均已接入；结果会按能力标记"
            " exact/approximate/bounded/mixed；"
            "可物化的全量节点/边结果写入 Artifact，Agent 仅接收有界预览。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "graph_id": {"type": "string", "description": "逻辑图标识；默认当前 session_id"},
                "graph_version": {"type": "integer", "minimum": 1},
                "algorithm": {
                    "type": "string",
                    "enum": [
                        "degree", "in_degree", "out_degree", "degree_centrality",
                        "shortest_path", "all_shortest_paths", "maximum_flow",
                        "minimum_cut", "global_minimum_cut", "minimum_spanning_tree",
                        "community_detection", "node_centrality", "graph_connectivity",
                        "find_cycles", "maximum_cycle", "topological_sort",
                        "degree_analysis", "subgraph_extraction",
                        "link_prediction_score", "path_analysis", "clique_analysis",
                        "reachability", "global_metrics",
                        "graph_structure_analysis", "connectivity_analysis",
                        "tree_dag_analysis", "matching_coloring_analysis",
                        "distance_measures"
                    ],
                },
                "parameters": {
                    "type": "object",
                    "description": (
                        "算法参数。条件必填：shortest_path/all_shortest_paths 需要 source+target；"
                        "maximum_flow/minimum_cut 需要 source+sink；各 method/metric 型算法需要"
                        "相应选择器；ego_graph 另需 node，node_subset 另需 nodes；"
                        "path_analysis 的 all_simple_paths/has_path 需 source+target；"
                        "reachability 的 descendants/ancestors 需 node；链路预测的 source/target"
                        "必须同时提供或同时省略；分组结构遍历需 source，局部连通度/割集需"
                        " source+target，LCA 需 node1+node2，支配分析需 source，均衡着色需"
                        " num_colors。保留 additionalProperties 以兼容后续引擎参数。"
                    ),
                    "additionalProperties": True,
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": (
                                "起点/源点。最短路与成对路径分析必填；最大流/最小割与 sink 配合；"
                                "链路预测中必须与 target 同时提供。"
                            ),
                        },
                        "target": {
                            "type": "string",
                            "description": (
                                "终点。最短路与成对路径分析必填；shortest_path 可传 'all'"
                                "生成从 source 出发的距离 Artifact；链路预测中必须与 source 同时提供。"
                            ),
                        },
                        "sink": {
                            "type": "string",
                            "description": "maximum_flow 和 minimum_cut 的汇点，与 source 同时必填。",
                        },
                        "algorithm": {
                            "type": "string",
                            "enum": [
                                "default", "yen_k_shortest",
                                "louvain", "greedy_modularity", "label_propagation",
                                "label_propagation_asyn", "leiden", "girvan_newman",
                                "asyn_fluidc",
                            ],
                            "description": (
                                "shortest_path 子模式为 default/yen_k_shortest（默认 default）；"
                                "community_detection 子模式为其余七项（默认 louvain，当前均为"
                                "可扩展加权同步标签传播近似）。"
                            ),
                        },
                        "centrality_type": {
                            "type": "string",
                            "enum": [
                                "degree", "betweenness", "closeness", "pagerank",
                                "eigenvector", "harmonic", "katz", "load", "voterank",
                                "percolation",
                            ],
                            "default": "degree",
                            "description": (
                                "node_centrality 的指标。betweenness 可按 sample_sources 采样；"
                                "load/percolation 使用显式标记的 Brandes 近似。"
                            ),
                        },
                        "method": {
                            "type": "string",
                            "enum": [
                                "ego_graph", "k_core", "k_truss", "k_corona", "node_subset",
                                "jaccard", "adamic_adar", "resource_allocation",
                                "preferential_attachment", "all_simple_paths", "has_path",
                                "average_shortest_path_length", "find_cliques", "clique_number",
                                "number_of_cliques", "transitive_closure", "descendants",
                                "ancestors", "is_tree", "is_forest",
                                "is_directed_acyclic_graph", "bfs_tree", "dfs_tree",
                                "lexicographical_topological_sort", "all_topological_sorts",
                                "is_at_free", "check_planarity", "is_tournament",
                                "score_sequence", "is_bipartite",
                                "weisfeiler_lehman_graph_hash", "node_connectivity",
                                "edge_connectivity", "minimum_node_cut", "minimum_edge_cut",
                                "articulation_points", "bridges", "local_bridges",
                                "biconnected_component_edges", "bridge_components",
                                "condensation", "maximum_spanning_tree",
                                "random_spanning_tree", "lowest_common_ancestor",
                                "immediate_dominators", "dominance_frontiers",
                                "dag_longest_path", "dag_longest_path_length", "antichains",
                                "is_aperiodic", "colliders", "v_structures",
                                "max_weight_matching", "min_weight_matching",
                                "maximal_matching", "min_edge_cover", "is_matching",
                                "greedy_color", "equitable_color",
                            ],
                            "description": (
                                "subgraph_extraction、link_prediction_score、path_analysis、"
                                "clique_analysis、reachability 或 dev 扩展分析算子的子模式；"
                                "必须选择与顶层 algorithm 对应的一项。"
                            ),
                        },
                        "metric": {
                            "type": "string",
                            "enum": [
                                "density", "diameter", "radius", "transitivity",
                                "average_clustering", "degree_assortativity",
                                "center", "periphery", "eccentricity", "wiener_index",
                                "global_efficiency", "local_efficiency",
                                "average_neighbor_degree", "average_degree_connectivity",
                                "rich_club_coefficient", "reciprocity", "flow_hierarchy",
                                "barycenter", "estrada_index", "girth",
                            ],
                            "description": "global_metrics 或 distance_measures 必填的图级指标。",
                        },
                        "node": {
                            "type": "string",
                            "description": (
                                "ego_graph 的中心节点、descendants/ancestors 的查询节点，或"
                                "number_of_cliques 的可选指定节点。"
                            ),
                        },
                        "nodes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "subgraph_extraction/method=node_subset 必填的节点标识数组；"
                                "运行时默认控制面上限为 10000 项，可由部署配置收紧。"
                            ),
                        },
                        "focus_nodes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 200,
                            "description": (
                                "community_detection 完成后需要直接查询社区归属的节点数组；"
                                "用于把前一步 Top-K 节点与同一次全图社区结果关联。"
                            ),
                        },
                        "k": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "yen_k_shortest 的路径数（1..200，默认 3），或 ego_graph 半径及"
                                "k_core/k_truss/k_corona 阈值（默认 2）。"
                            ),
                        },
                        "cutoff": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10000,
                            "default": 5,
                            "description": "all_simple_paths 的最大边数/深度。",
                        },
                        "limit": {
                            "anyOf": [
                                {"type": "integer", "minimum": 1, "maximum": 200},
                                {"const": "all"},
                            ],
                            "description": (
                                "列表/路径/环/团/分区等结果的控制面上限；整数为 1..200。"
                                "字符串仅支持 'all'，大图路径仍映射到 200 条硬上限；完整或有界"
                                "物化结果以 Artifact 为准。"
                            ),
                        },
                        "top_k": {
                            "anyOf": [
                                {"type": "integer", "minimum": 1, "maximum": 200},
                                {"const": "all"},
                            ],
                            "description": (
                                "degree 快速入口和全图链路预测的 top-k，整数为 1..200；"
                                "字符串仅支持 'all' 且映射到 200 条控制面上限。省略时"
                                "degree 默认为 20，link_prediction_score 默认为 10。"
                            ),
                        },
                        "max_cycles": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100000,
                            "default": 10000,
                            "description": "maximum_cycle 最多检查的简单环数量。",
                        },
                        "optimize_by": {
                            "type": "string",
                            "enum": ["length", "weight"],
                            "default": "length",
                            "description": "maximum_cycle 按环长度或边权总和优化。",
                        },
                        "treat_as_undirected": {
                            "type": "boolean",
                            "default": True,
                            "description": "global_minimum_cut 在有向快照上是否按无向图计算。",
                        },
                        "max_iterations": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2000,
                            "default": 5,
                            "description": (
                                "社区传播或迭代中心性的迭代上限；社区内核实际最多 500 次，"
                                "中心性最多 2000 次。"
                            ),
                        },
                        "sample_sources": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _LARGE_GRAPH_MAX_SAMPLE_SOURCES,
                            "description": (
                                "betweenness/load/percolation 等 Brandes 类中心性的源点采样数；"
                                "小于节点总数时结果会标记 approximate。部署硬上限由 "
                                "LARGE_GRAPH_MAX_SAMPLE_SOURCES 配置（当前为 "
                                f"{_LARGE_GRAPH_MAX_SAMPLE_SOURCES}），且不能由 "
                                "allow_expensive 绕过。"
                            ),
                        },
                        "damping": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "exclusiveMaximum": 1,
                            "default": 0.85,
                            "description": "PageRank 的有限阻尼系数。",
                        },
                        "alpha": {
                            "type": "number",
                            "default": 0.1,
                            "description": "Katz 中心性的有限衰减系数（不接受 NaN/Infinity）。",
                        },
                        "beta": {
                            "type": "number",
                            "default": 1.0,
                            "description": "Katz 中心性的有限外源偏置（不接受 NaN/Infinity）。",
                        },
                        "tolerance": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "default": 1e-6,
                            "description": "PageRank/eigenvector/Katz 的有限正收敛容差。",
                        },
                        "use_weight": {
                            "type": "boolean",
                            "default": True,
                            "description": (
                                "shortest_path 是否使用边权；all_shortest_paths 当前固定使用权重，"
                                "其他算法会忽略该参数。"
                            ),
                        },
                        "create_version": {
                            "type": "boolean",
                            "default": True,
                            "description": (
                                "仅用于 subgraph_extraction。默认 true：在同一持久作业中基于"
                                "筛选结果发布新的分区快照版本；该版本不会物化为 GraphData，也不会"
                                "静默切换当前小图表示，后续请显式使用返回的 new_graph_version。"
                                "设为 false 时只生成子图 Artifact。"
                            ),
                        },
                        "allow_expensive": {
                            "type": "boolean",
                            "default": False,
                            "description": "允许绕过 O(V²)/指数级算法的默认安全准入；仅在确认单机资源足够时使用。",
                        },
                        "strict_semantics": {
                            "type": "boolean",
                            "default": False,
                            "description": "拒绝大图路径中的近似算法映射，要求与小图算法语义一致。",
                        },
                    },
                },
                "workers": {"type": "integer", "minimum": 1},
            },
            "required": ["graph_version", "algorithm"],
        },
        "category": "compute",
    },
    {
        "name": "list_large_graph_versions",
        "description": "列出当前会话逻辑图的分区快照版本、发布状态和质量摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {"graph_id": {"type": "string"}},
            "required": [],
        },
        "category": "data",
    },
    {
        "name": "get_large_graph_artifact",
        "description": (
            "获取大图算法 Artifact 的元数据和有界预览；"
            "不会读取或返回全量分区结果。Artifact 会返回结果文件总数、有界的文件路径/"
            "下载序号预览及完整序号范围；download_url_template 中的 {partition} 是该扁平"
            "结果文件列表的下载序号，可据此逐文件流式下载。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "include_preview": {"type": "boolean", "default": True},
            },
            "required": ["artifact_id"],
        },
        "category": "compute",
    },
    {
        "name": "get_large_graph_capabilities",
        "description": (
            "返回大图并行引擎当前具有明确语义映射的算法、引擎版本、可用子模式、"
            "exact/approximate/bounded 执行语义、复杂度等级、并行策略及结果类型。"
            "提交 run_large_graph_algorithm 前可用它核对 mode/近似语义；具体字段名、"
            "条件必填项和数值边界见该工具的 parameters Schema。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
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
            "- include_community(可选): 是否包含社区信息，默认 true；仅在用户要求关闭社区着色时设为 false。\n"
            "- max_nodes(可选): 返回的最大节点数，默认 800。超出时生成确定性的智能概览，\n"
            "  保留高亮、节点类型与社区代表，优先高连接度节点并补齐代表间路径；\n"
            "  结果中含 truncated=true、total_nodes 与 overview 字段。\n"
            "  图规模较大时可减小该值；交互式 SVG 画布最高展示 800 个节点、2000 条边。\n"
            "- highlights(可选): 高亮信息，统一格式:\n"
            "  {\n"
            '    "nodes": [{"ids": ["A","B"], "color": "#hex", "label": "关键节点"}],\n'
            '    "paths": [{"node_sequence": ["A","B","C"], "color": "#hex", "label": "最短路径"}],\n'
            '    "subgraphs": [{"nodes": ["A","B","C","D"], "color": "#hex", "label": "官能团"}]\n'
            "  }\n"
            "  color 和 label 可选，系统会自动分配颜色。\n\n"
            "返回:\n"
            "- JSON 字符串，包含节点、边、高亮信息及元数据。\n"
            "- 若节点或边被截断，结果含 truncated=true、全图计数及实际返回上限。"
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
                    "description": "是否包含社区信息（默认 true，仅在用户要求关闭社区着色时设为 false）",
                    "default": True,
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "返回的最大节点数，默认 800。超出时生成稳定、可复现的拓扑智能概览。",
                    "default": 800,
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
    {
        "name": "nebula_status",
        "description": (
            "检查 NebulaGraph 配置、连接、Schema 以及当前会话图的同步状态。"
            "该工具不会修改图数据。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "category": "data",
    },
    {
        "name": "nebula_sync_graph",
        "description": (
            "本地存储模式下，将当前 GraphData 显式同步到 NebulaGraph 并返回异步 job_id。"
            "GRAPH_STORAGE_BACKEND=nebula 时当前图已经原生存储在 NebulaGraph，该工具直接返回 already_primary=true。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "category": "data",
    },
    {
        "name": "nebula_get_node_info",
        "description": "使用 NebulaGraph FETCH PROP 原生查询当前已同步图中的节点属性。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "当前图中的节点名称"},
            },
            "required": ["node_name"],
        },
        "category": "compute",
    },
    {
        "name": "nebula_get_neighbors",
        "description": "使用 NebulaGraph GO 原生查询当前已同步图中的一跳邻居。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "当前图中的节点名称"},
                "direction": {
                    "type": "string",
                    "enum": ["out", "in", "both"],
                    "default": "out",
                    "description": "出边、入边或双向遍历",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "返回上限，服务端限制为 1 到 200",
                },
            },
            "required": ["node_name"],
        },
        "category": "compute",
    },
    {
        "name": "nebula_shortest_path",
        "description": (
            "使用 NebulaGraph FIND SHORTEST PATH 查询按跳数计算的最短路径。"
            "该工具不计算加权最短路径；加权场景继续使用现有 shortest_path 工具。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "起点节点名称"},
                "target": {"type": "string", "description": "终点节点名称"},
                "direction": {
                    "type": "string",
                    "enum": ["out", "in", "both"],
                    "default": "out",
                },
                "max_steps": {
                    "type": "integer",
                    "default": 10,
                    "description": "最大跳数，服务端限制为 1 到 20",
                },
            },
            "required": ["source", "target"],
        },
        "category": "compute",
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
            "description": "注意力头数，仅 model=GAT/GATV2/TRANSFORMER 时生效。",
            "default": 2,
        },
        "appnp_k": {
            "type": "integer",
            "minimum": 1,
            "description": "APPNP 的传播迭代次数，仅 model=APPNP 时生效。",
            "default": 10,
        },
        "appnp_alpha": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 1,
            "description": "APPNP 的 teleport/保留概率，仅 model=APPNP 时生效。",
            "default": 0.1,
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
        "name": "load_graph_classification_graph",
        "description": (
            "从当前会话已加载的 HIV/PCBA 多图图分类数据集中按索引导出一张图，"
            "并设置为当前会话单图。\n\n"
            "典型用法：用户要求查看/可视化/分析某个 graph_index（例如 5）时，"
            "先调用本工具，然后继续调用 get_graph_info、get_node_info、"
            "visualize_subgraph 或 subgraph_extraction。\n\n"
            "参数:\n"
            "- graph_index: 0-based 图索引，范围为 [0, num_graphs-1]。\n"
            "- dataset_id: 可选；默认使用当前会话绑定的图分类数据集。\n\n"
            "返回:\n"
            "- 导出的单图摘要、graph_version、split、graph_label、节点/边预览，"
            "以及当前图分析 readiness。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "graph_index": {
                    "type": "integer",
                    "description": "0-based 图索引，例如 5 表示第 6 张图。",
                    "minimum": 0,
                },
                "dataset_id": {
                    "type": "string",
                    "description": "可选；指定当前会话中已绑定的图分类数据集 ID。",
                },
            },
            "required": ["graph_index"],
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
                "model": {
                    "type": "string",
                    "description": "模型名称（可选）。GNN 主干支持 APPNP、GCN、GAT、GATV2、GEN、GIN、SAGE、TRANSFORMER。",
                },
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
            "- query(可选): 推理输入内容（字典或等价 JSON 字符串）。精确格式示例："
            "节点分类 `{\"nodes\":[\"0\",\"1\"]}`；链接预测的 edges 必须是"
            "二元数组列表 `{\"edges\":[[\"0\",\"8\"],[\"1\",\"344\"]]}`，"
            "不接受 `{source,target}` 对象；按源节点预测可用 "
            "`{\"source_node\":\"0\",\"candidate_nodes\":[\"8\",\"1\"],\"topk\":10}`；"
            "图分类使用 `{\"graph_indices\":[0,1,2]}`。\n"
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
# 报告生成工具（默认不暴露，待执行逻辑接入后开启）
# ─────────────────────────────────────────────────────────────────

REPORTING_TOOLS: List[dict] = [
    {
        "name": "create_analysis_report",
        "description": (
            "基于前序图分析工具结果生成图分析可视化报告规格。"
            "适用于整体理解、统计总结、结构解释、模式发现、对比分析、诊断判断或报告生成等请求。"
            "报告图表数据必须来自 source_results、图状态或用户明确提供的数据。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "report_intent": {
                    "type": "string",
                    "enum": ["overview", "analysis", "diagnosis", "comparison", "custom"],
                    "description": "报告意图。",
                    "default": "analysis",
                },
                "analysis_goal": {
                    "type": "string",
                    "description": "用户原始分析目标或 Agent 归纳后的分析目标。",
                },
                "title": {
                    "type": "string",
                    "description": "报告标题。",
                },
                "source_results": {
                    "type": "object",
                    "description": "前序工具结果，按工具名组织，例如 get_graph_info、degree_analysis、node_centrality 或 global_metrics。",
                    "additionalProperties": True,
                },
                "options": {
                    "type": "object",
                    "description": "报告生成选项。",
                    "properties": {
                        "top_k": {
                            "type": "integer",
                            "description": "图表和表格展示上限。",
                            "default": 10,
                        },
                        "include_narrative": {
                            "type": "boolean",
                            "description": "是否生成规则化文字洞察。",
                            "default": True,
                        },
                        "max_sections": {
                            "type": "integer",
                            "description": "报告 section 数量上限。",
                            "default": 6,
                        },
                    },
                    "additionalProperties": True,
                },
            },
            "required": ["report_intent", "analysis_goal", "source_results"],
        },
        "category": "reporting",
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


from graph_data_service.kinship import TOOL as KINSHIP_TOOL
GRAPH_TOOLS.append(KINSHIP_TOOL)
GRAPH_TOOLS.extend(FINANCE_TOOLS)

for _tool_spec in [*GRAPH_TOOLS, *GRAPH_ML_TOOLS, *REPORTING_TOOLS, *INTERNAL_TOOLS]:
    _tool_spec["execution"] = get_tool_execution_metadata(_tool_spec["name"])
    if _tool_spec["name"] in MULTIGRAPH_POLICIES:
        _tool_spec["multigraph_support"] = MULTIGRAPH_POLICIES[_tool_spec["name"]]


def _reporting_tools_enabled_from_env() -> bool:
    value = os.getenv(_REPORTING_TOOLS_ENV, "").strip().lower()
    if value in _FALSE_ENV_VALUES:
        return False
    return True


def get_all_public_tools(
    include_graph_ml: bool = True,
    include_reporting: bool | None = None,
) -> List[dict]:
    """返回对 LLM 暴露的工具规格列表。"""
    tools = [*GRAPH_TOOLS, *CONSTRUCTION_TOOLS]
    if include_reporting is None:
        include_reporting = _reporting_tools_enabled_from_env()
    if include_reporting:
        tools.extend(REPORTING_TOOLS)
    if include_graph_ml:
        tools.extend(GRAPH_ML_TOOLS)
    return tools

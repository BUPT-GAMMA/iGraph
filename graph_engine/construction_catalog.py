"""Public construction tools shared by Graph Engine and its MCP adapter.

Raw source registration/build is deliberately available only to the trusted
dataset API. Agents use dataset IDs, never host filesystem paths.
"""

from __future__ import annotations

from graph_engine.tool_execution import get_tool_execution_metadata


_DATASET = {
    "type": "string",
    "description": "用户可访问的数据集 ID；从当前会话 data_state 或数据集列表获取，不得猜测。",
    "minLength": 1,
}

_OPERATION_TEXT = {"type": "string", "minLength": 1, "maxLength": 1024, "pattern": r"\S"}
_OPERATION_RULES = {
    "add_node": (("node_name", "node_type"), ("properties",)),
    "add_edge": (("source", "target", "edge_type"), ("properties",)),
    "collapse_node": (("node_id", "edge_type"), ()),
    "node_property_to_edge": (("node_id", "property", "edge_ids"), ("target_property", "remove_from_node")),
    "extract_property_node": (("node_ids", "property", "node_type", "edge_type"), ("remove_from_node",)),
    "delete": (("target", "ids"), ("property", "cascade")),
    "reverse_edges": (("edge_ids",), ()),
    "rename_type": (("target", "old_type", "new_type"), ()),
}
_OPERATION_PROPERTIES = {
    "op": {"type": "string", "enum": list(_OPERATION_RULES)},
    "node_id": _OPERATION_TEXT,
    "node_name": _OPERATION_TEXT,
    "source": _OPERATION_TEXT,
    "properties": {"type": "object"},
    "node_ids": {"type": "array", "items": _OPERATION_TEXT, "minItems": 1,
                 "maxItems": 200_000, "uniqueItems": True},
    "edge_ids": {"type": "array", "items": _OPERATION_TEXT, "minItems": 1,
                 "maxItems": 1_000_000, "uniqueItems": True},
    "ids": {"type": "array", "items": _OPERATION_TEXT, "minItems": 1,
            "maxItems": 1_000_000, "uniqueItems": True},
    "property": _OPERATION_TEXT,
    "target_property": _OPERATION_TEXT,
    "remove_from_node": {"type": "boolean"},
    "node_type": _OPERATION_TEXT,
    "edge_type": _OPERATION_TEXT,
    "target": _OPERATION_TEXT,
    "cascade": {"type": "boolean"},
    "old_type": _OPERATION_TEXT,
    "new_type": _OPERATION_TEXT,
}


def _operation_schema(name, required, optional):
    return {
        "type": "object",
        "properties": {"op": {"type": "string", "enum": [name]},
                       **{field: ({"type": "string", "enum": ["node", "edge"]}
                                  if field == "target" and name in {"delete", "rename_type"}
                                  else _OPERATION_PROPERTIES[field]) for field in (*required, *optional)}},
        "required": ["op", *required],
        "additionalProperties": False,
    }


_OPERATIONS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 100,
    "items": {
        "type": "object",
        # Keep the common property catalog for clients that inspect op.enum;
        # each branch supplies the actual operation-specific argument contract.
        "properties": _OPERATION_PROPERTIES,
        "required": ["op"],
        "additionalProperties": False,
        "oneOf": [_operation_schema(name, required, optional)
                  for name, (required, optional) in _OPERATION_RULES.items()],
    },
}

_OPERATION_HELP = (
    "operations 中每项用 op 指定规则："
    "add_node(node_name,node_type,properties?) 手动补充实体；"
    "add_edge(source,target,edge_type,properties?) 在已有实体之间补充关系，source/target 为真实节点 ID；"
    "新增事实仅用于用户明确要求且有来源核验的补充，在 properties 中保留出处；不得凭推测补边。"
    "collapse_node(node_id,edge_type) 将连接中间节点转为边；"
    "node_property_to_edge(node_id,property,edge_ids,target_property?,remove_from_node?) 将节点属性移到边；"
    "extract_property_node(node_ids,property,node_type,edge_type,remove_from_node?) 将属性提取为节点；"
    "delete(target=node|edge,ids,property?,cascade?) 删除指定元素或属性；"
    "reverse_edges(edge_ids) 反转方向；"
    "rename_type(target=node|edge,old_type,new_type) 重命名类型。"
    "必须先读取图以取得真实 ID 和属性；remove_from_node/cascade 必须按用户意图显式指定。"
)


def _tool(name: str, description: str, *, optimization: bool = False, dataset: bool = True) -> dict:
    properties: dict = {"dataset_id": _DATASET} if dataset else {}
    required = ["dataset_id"] if dataset else []
    if optimization:
        properties = {
            **properties,
            "base_revision": {"type": "integer", "minimum": 1, "description": "读取或预览时的版本号。"},
            "operations": _OPERATIONS,
        }
        required = [*required, "base_revision", "operations"]
        description += "\n" + _OPERATION_HELP
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required,
                        "additionalProperties": False},
        "category": "data",
        "execution": get_tool_execution_metadata(name),
    }


CONSTRUCTION_TOOLS = [
    _tool("get_graph_construction_capabilities",
          "查看 SQL/CSV/TXT 构图算法可用性与支持的优化规则。算法尚未接入时如实报告 algorithm_unavailable；"
          "原始文件构图由数据集面板的‘构图’按钮发起，不允许改走旧边表解析或文本抽取。", dataset=False),
    _tool("get_graph_construction_status",
          "仅查询 SQL/CSV/TXT 数据源的构图状态、revision、source_type 和规模。ready 表示可复用已保存图；"
          "not_built/algorithm_unavailable/failed 不表示已构图成功。"
          "PT 等已有图文件请用 get_graph_info；本工具对这类数据集返回 not_applicable，不能据此判断图未加载。"),
    _tool("get_constructed_graph",
          "只读取得数据集已保存的图和版本：graph 包含 directed、nodes(id,type,properties)、"
          "edges(id,source,target,type,properties)。用于检查真实元素及优化前的数据。"
          "大结果仅返回有界样本并标记 inspection.truncated，不能以样本推断完整图。"
          "股权分析应使用 finance_inspect_ownership 等专用工具在服务端计算，不读取整图到上下文。"),
    _tool("load_constructed_graph",
          "把已构图数据集的保存版本加载到当前会话，供现有图分析、可视化和训练工具使用。"
          "不会重复构图；会替换当前会话的活动图。加载后调用 get_graph_info 核验，展示时使用 visualize_subgraph。"),
    _tool("preview_graph_optimization",
          "按 base_revision 预览六类图优化，返回变更后图与差异，不保存或修改数据集。"
          "先向用户展示受影响节点、边、属性和规模变化，再进入提交确认。", optimization=True),
    _tool("commit_graph_optimization",
          "将已预览并经用户确认的同一批 operations 持久化到数据集，生成新 revision。"
          "必须先 preview，保持相同 base_revision 与 operations，并通过工具确认流程。"
          "冲突时重新读取和预览，不能静默覆盖。保存后用 load_constructed_graph 更新当前会话。", optimization=True),
]

CONSTRUCTION_TOOL_NAMES = frozenset(tool["name"] for tool in CONSTRUCTION_TOOLS)

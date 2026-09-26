"""Bounded, read-only finance tool contracts shared by MCP and HTTP dispatch.

These tools consume the current session's original construction records on
the server. Neither graph documents nor dataset/session selectors are inputs.
"""
from __future__ import annotations

import copy
import math
import re


FINANCE_OPERATIONS = {
    "finance_inspect_ownership": "snapshot",
    "finance_first_holder": "first_holder",
    "finance_control_chain": "control_chain",
    "finance_group_tree": "group_tree",
    "finance_economic_interest": "economic_interest",
}
FINANCE_TOOL_NAMES = frozenset(FINANCE_OPERATIONS)
FINANCE_MAX_RESULT_BYTES = 128 * 1024

_ID = {"type": "string", "minLength": 1, "maxLength": 258,
       "description": "原始业务主体 ID（最长256字符）或检查工具返回的 I:/P: 类型主体 ID（最长258字符）；先用 finance_inspect_ownership 消歧，不使用 row_ 哈希或猜测姓名。"}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 256}
_IDS = {"type": "array", "items": _TEXT, "maxItems": 100}
_ENTITY_IDS = {"type": "array", "items": _ID, "maxItems": 100}
_POLICY = {
    "type": "object", "additionalProperties": False,
    "description": "仅使用有来源的明确规则；不得猜测权威身份、特殊主体名单或声称规则完整。GP/法人关系无可靠来源时保持 rule_coverage=missing。",
    "properties": {
        "mode": {"type": "string", "enum": ["equity_only", "configured_rules"], "default": "equity_only"},
        "version": {"type": "string", "minLength": 1, "maxLength": 128, "default": "equity-only-v1"},
        "ratio_format": {"type": "string", "enum": ["percent_string", "percent_number", "fraction"], "default": "percent_string"},
        "conflict_strategy": {"type": "string", "enum": ["reject_conflict", "max_ratio_per_business_fact"], "default": "reject_conflict"},
        "stop_statuses": _IDS,
        "stop_entity_ids": _ENTITY_IDS,
        "exclude_entity_ids": _ENTITY_IDS,
        "min_edge_ratio": {"type": "string", "maxLength": 60, "pattern": r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$", "default": "0",
                           "description": "默认不删除小股比。非零过滤仅用于明确选定的兼容口径，不能宣称全量根排名。"},
        "rule_relations": {
            "type": "array", "maxItems": 100,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["type", "target_id", "holder_id", "snapshot_id", "authority", "evidence_refs"],
                      "properties": {
                          "type": {"type": "string", "enum": ["GP", "LEGAL_PERSON"]},
                          "target_id": _ID, "holder_id": _ID, "snapshot_id": _TEXT,
                          "authority": _TEXT,
                          "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 10,
                                            "items": {"type": "string", "minLength": 1, "maxLength": 512}},
                      }},
        },
        "rule_priority": {"type": "array", "maxItems": 2, "uniqueItems": True,
                          "items": {"type": "string", "enum": ["GP", "LEGAL_PERSON"]}},
        "rule_coverage": {"type": "string", "enum": ["missing", "complete"], "default": "missing",
                          "description": "complete 仅限权威规则已齐备且显式设置 configured_rules/version，不能由模型自行推断。"},
    },
}
_COMMON = {
    "policy": _POLICY,
    "evidence_top_k": {"type": "integer", "minimum": 0, "maximum": 30, "default": 5,
                       "description": "证据路径展示上限，不影响完整累计和排名。"},
    "display_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100,
                      "description": "主体、成员及排名展示上限；展示截断与计算完整性分别返回。"},
    "max_nodes": {"type": "integer", "minimum": 1, "maximum": 200000, "default": 100000,
                  "description": "投影输入及计算访问的节点预算；不足时明确返回不完整。"},
    "max_edges": {"type": "integer", "minimum": 1, "maximum": 2000000, "default": 1000000,
                  "description": "投影输入及计算访问的边预算；不足时明确返回不完整。"},
    "max_seconds": {"type": "number", "minimum": 0.05, "maximum": 60, "default": 10,
                    "description": "投影和业务计算的共同时间预算（秒）。"},
}
_DESCRIPTIONS = {
    "finance_inspect_ownership": "检查当前已加载三表构图中的股权快照、主体数量、持股方向、比例和质量问题。可按业务 ID/名称查找有界候选，重名不得自动合并。",
    "finance_first_holder": "计算第一大股东：区分直接过半首大、全部根经济权益累计及并列；缺权威 GP/法人规则时只报告明确股权口径，不能宣称全部业务规则完成。",
    "finance_control_chain": "计算严格 >50% 可支配表决权固定点及目标控制链；受控持有人贡献只计一次，返回原始持股和分支贡献证据。控制不等于路径股比乘积。",
    "finance_group_tree": "从业务根主体展开严格控制集团，区分确认成员、额外贡献关系和树展示；可达/PATH/AFFINITY 不能冒充控制集团，遇环或冲突保留复核状态。",
    "finance_economic_interest": "完整累计目标的根经济权益并保留并列和 SCC 数值状态；Top-K 仅控制证据展示，不把最大单路径当作全部根累计，不据经济权益确认控制。",
}


def _tool(name: str) -> dict:
    properties = copy.deepcopy(_COMMON)
    required = []
    if name == "finance_inspect_ownership":
        properties["query"] = {"type": "string", "minLength": 1, "maxLength": 200,
                               "description": "可选业务 ID 或主体名称查询；候选有界返回，重名由用户选择。"}
        properties["target_id"] = copy.deepcopy(_ID)
    else:
        field = "root_id" if name == "finance_group_tree" else "target_id"
        properties[field] = copy.deepcopy(_ID)
        required = [field]
        if name == "finance_economic_interest":
            properties["root_id"] = {**copy.deepcopy(_ID), "description": "可选指定上游主体，另返回其到 target_id 的经济权益；不要求其为输入图边界根。"}
    return {
        "name": name,
        "description": _DESCRIPTIONS[name] + (
            " 只读当前授权会话图，服务端直接读取原始记录，无需先调用 get_constructed_graph。"
            "普通边列表或缺失原始三表返回明确 schema 错误；所有响应有字节上限，"
            "预算、数据质量、规则缺失和展示截断不能被解释为完整业务确认。"
        ),
        "inputSchema": {"type": "object", "properties": properties,
                        "required": required, "additionalProperties": False},
        "category": "compute",
    }


FINANCE_TOOLS = [_tool(name) for name in FINANCE_OPERATIONS]
_SCHEMAS = {spec["name"]: spec["inputSchema"] for spec in FINANCE_TOOLS}


def _validate(value, schema, path):
    kind = schema["type"]
    valid = {
        "object": lambda: isinstance(value, dict),
        "string": lambda: isinstance(value, str),
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) is int or (type(value) is float and math.isfinite(value)),
        "boolean": lambda: type(value) is bool,
        "array": lambda: isinstance(value, list),
    }[kind]()
    if not valid:
        raise ValueError(f"{path} must be {kind}")
    if kind == "object":
        properties = schema["properties"]
        if set(value) - set(properties):
            raise ValueError(f"{path} contains unsupported fields")
        if set(schema.get("required", ())) - set(value):
            raise ValueError(f"{path} is missing required fields")
        for key, item in value.items():
            _validate(item, properties[key], f"{path}.{key}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ValueError(f"{path} has an invalid item count")
        for item in value:
            _validate(item, schema["items"], f"{path}[]")
        if schema.get("uniqueItems") and len(value) != len(set(value)):
            raise ValueError(f"{path} contains duplicate values")
    elif kind == "string":
        if not value.strip() or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 200):
            raise ValueError(f"{path} has an invalid length")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"{path} has an invalid format")
    elif kind in {"integer", "number"}:
        if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise ValueError(f"{path} is outside its supported range")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} has an unsupported value")


def validate_finance_arguments(tool_name: str, arguments: dict) -> dict:
    """Apply the same constraints to direct HTTP calls as to MCP calls."""
    schema = _SCHEMAS[tool_name]
    _validate(arguments, schema, "arguments")
    return {**{key: value["default"] for key, value in schema["properties"].items()
               if "default" in value}, **copy.deepcopy(arguments)}

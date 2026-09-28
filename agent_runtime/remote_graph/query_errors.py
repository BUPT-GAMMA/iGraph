"""Application-authored query hints; never expose arbitrary exception text."""
from agent_runtime.execution_store import ControlError


class UnsupportedMapError(ControlError):
    """Falkor's expression envelope excludes every curly-brace map form."""

    safe_message = (
        "当前 FalkorDB 适配器不支持 map 构造、投影或模式内属性 map。"
        "筛选属性请使用 MATCH (n:Label) WHERE n.property = $value RETURN n.property；"
        "返回多个属性请使用独立列，复杂结果可导出后在工作区处理。"
    )

    def __init__(self):
        super().__init__('REMOTE_GRAPH_QUERY_REJECTED', 422)

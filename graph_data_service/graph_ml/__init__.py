"""GraphData in-process GraphML 模块。"""

__all__ = ["GraphMLModule"]


def __getattr__(name: str):
    if name == "GraphMLModule":
        from .module import GraphMLModule

        return GraphMLModule
    raise AttributeError(name)

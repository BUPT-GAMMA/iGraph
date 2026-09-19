"""Keep the built-in help page aligned with implemented tools and GNN models."""
import ast
import re
from pathlib import Path

from graph_engine.tool_catalog import get_all_public_tools

ROOT = Path(__file__).resolve().parents[1]


def test_help_lists_all_analysis_and_learning_tools_without_invented_names():
    source = (ROOT / "frontend/src/helpCatalog.ts").read_text(encoding="utf-8")
    displayed = re.findall(r"\btool\('([^']+)'", source)
    assert len(displayed) == len(set(displayed))
    specs = get_all_public_tools(include_graph_ml=True, include_reporting=True)
    registered = {item["name"] for item in specs}
    expected = {item["name"] for item in specs if item["category"] in {"compute", "graph_ml", "reporting"}}
    expected.update({"get_graph_info", "visualize_subgraph"})
    assert set(displayed) == expected
    assert set(displayed) <= registered


def test_help_model_names_match_actual_backbone_registry_without_loading_torch():
    source = (ROOT / "frontend/src/helpCatalog.ts").read_text(encoding="utf-8")
    displayed = set(re.findall(r"\bbackbone\('([^']+)'", source))
    tree = ast.parse((ROOT / "graph_data_service/graph_ml/models/__init__.py").read_text(encoding="utf-8"))
    registry = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name) and node.target.id == "_BACKBONE_REGISTRY")
    assert displayed == {ast.literal_eval(key) for key in registry.keys}
    assert "name: 'node2vec'" in source


def test_help_pretraining_names_match_supported_request_methods():
    source = (ROOT / "frontend/src/helpCatalog.ts").read_text(encoding="utf-8")
    tree = ast.parse((ROOT / "graph_data_service/graph_ml/tasks/pre_train.py").read_text(encoding="utf-8"))
    methods = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "PRETRAIN_METHODS_REQUIRE_MODEL" for target in node.targets))
    for method in methods:
        assert f"'{method}'" in source

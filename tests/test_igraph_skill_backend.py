from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from benchmark_platform.igraph_skill_backend import (
    BenchmarkSkillClient,
    install_benchmark_skill_client,
)
from benchmark_platform.tool_names import CANONICAL_TOOL_GROUPS
from benchmark_platform.virtual_ml import (
    VIRTUAL_ML_BROKER_ENDPOINT_ENV,
    VIRTUAL_ML_BROKER_TOKEN_ENV,
)


def _function_lines(actions: tuple[str, ...], first: str = "") -> str:
    prefix = f"{first}, " if first else ""
    return "\n".join(
        f"def {action}({prefix}value=None):\n    return {{'action': '{action}', 'value': value}}"
        for action in actions
    )


class BenchmarkSkillClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skills = self.root / "skills"
        self.work = self.root / "attempt"
        self.skills.mkdir()
        self.work.mkdir()
        self.dataset = self.work / "dataset.pt"
        self.dataset.write_bytes(b"fixture")
        self._write_fake_skills()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_fake_skills(self) -> None:
        dispatch = """
if __name__ == '__main__':
    import json, sys
    action = sys.argv[1]
    arguments = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    print(json.dumps({'action': action, 'arguments': arguments}))
"""
        for group in ("theory", "insight"):
            (self.skills / f"graph_{group}_tools.py").write_text(
                _function_lines(CANONICAL_TOOL_GROUPS[group], "graph") + dispatch,
                encoding="utf-8",
            )

        retrieval = """
import json, sys
if __name__ == '__main__':
    print(json.dumps({'action': sys.argv[1], 'path': sys.argv[2], 'tail': sys.argv[3:]}))
"""
        (self.skills / "graph_retrieval_tools.py").write_text(retrieval, encoding="utf-8")
        (self.skills / "graph_io_tools.py").write_text(retrieval, encoding="utf-8")

        ml = (
            _function_lines(CANONICAL_TOOL_GROUPS["ml"])
            + "\n# VIRTUAL_ML_BROKER_ENDPOINT_ENV "
            + "VIRTUAL_ML_BROKER_TOKEN_ENV _invoke_virtual_ml_broker\n"
            + """
if __name__ == '__main__':
    import json, os, sys
    action = sys.argv[1]
    payload = {
        'action': sys.argv[1],
        'broker': bool(os.environ.get('IGRAPH_VIRTUAL_ML_BROKER_ENDPOINT')),
        'provider_secret_forwarded': bool(os.environ.get('LLM_API_KEY')),
    }
    if action.endswith('_train'):
        if 'link_prediction' in action:
            task = 'link_prediction'
        elif 'graph_embedding' in action:
            task = 'graph_classification'
        else:
            task = 'node_classification'
        payload.update({'status': 'trained', 'task': task, 'in_channels': 1})
    else:
        payload.update({'status': 'success', 'predictions': [1]})
    print(json.dumps(payload))
"""
        )
        (self.skills / "graph_ml_tools.py").write_text(ml, encoding="utf-8")
        (self.skills / "graph_task_solver.py").write_text(
            "import json, sys\nprint(json.dumps({'ok': True, 'argv': sys.argv[1:]}))\n",
            encoding="utf-8",
        )

    def _client(self) -> BenchmarkSkillClient:
        return BenchmarkSkillClient(
            self.skills,
            self.dataset,
            environment={
                "PATH": os.environ.get("PATH", ""),
                "LLM_API_KEY": "must-not-be-forwarded",
                VIRTUAL_ML_BROKER_ENDPOINT_ENV: "http://127.0.0.1:12345/invoke",
                VIRTUAL_ML_BROKER_TOKEN_ENV: "opaque-test-token",
            },
            work_dir=self.work,
        )

    def test_lists_every_canonical_action(self) -> None:
        client = self._client()
        tools = asyncio.run(client.list_tools())
        expected = {
            action
            for actions in CANONICAL_TOOL_GROUPS.values()
            for action in actions
        }
        self.assertEqual({tool["name"] for tool in tools}, expected)
        link_train = next(
            tool
            for tool in tools
            if tool["name"] == "structural_node_embedding_link_prediction_train"
        )
        self.assertEqual(link_train["inputSchema"]["required"], [])
        self.assertNotIn("node_features", link_train["inputSchema"]["properties"])

    def test_uses_bundled_real_perturbation_when_server_snapshot_lacks_it(self) -> None:
        insight_actions = tuple(
            action
            for action in CANONICAL_TOOL_GROUPS["insight"]
            if action != "simulate_edge_perturbation"
        )
        (self.skills / "graph_insight_tools.py").write_text(
            _function_lines(insight_actions, "graph"),
            encoding="utf-8",
        )
        client = self._client()
        self.assertEqual(
            client._scripts["simulate_edge_perturbation"].name,
            "graph_insight_extensions.py",
        )

    def test_executes_real_script_and_uses_selected_dataset(self) -> None:
        client = self._client()
        result = asyncio.run(client.call_tool("get_density", {}))
        self.assertTrue(result["success"])
        self.assertEqual(result["result"]["action"], "get_density")

    def test_forwards_only_broker_not_provider_secret(self) -> None:
        client = self._client()
        result = asyncio.run(
            client.call_tool(
                "structural_node_embedding_classification_train",
                {},
            )
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["result"]["broker"])
        self.assertFalse(result["result"]["provider_secret_forwarded"])

    def test_virtual_ml_train_state_is_reused_by_infer(self) -> None:
        client = self._client()
        trained = asyncio.run(
            client.call_tool("structural_node_embedding_link_prediction_train", {})
        )
        inferred = asyncio.run(
            client.call_tool(
                "structural_node_embedding_link_prediction_infer",
                {"source_node": 3, "target_node": 7},
            )
        )
        self.assertTrue(trained["success"])
        self.assertTrue(inferred["success"])
        self.assertEqual(inferred["result"]["predictions"], [1])

    def test_virtual_ml_infer_requires_matching_train(self) -> None:
        client = self._client()
        result = asyncio.run(
            client.call_tool("structural_node_embedding_link_prediction_infer", {})
        )
        self.assertFalse(result["success"])
        self.assertIn("train action", result["error"]["message"])

    def test_rejects_old_ml_script_when_broker_is_active(self) -> None:
        (self.skills / "graph_ml_tools.py").write_text(
            _function_lines(CANONICAL_TOOL_GROUPS["ml"]),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Exception, "does not implement"):
            self._client()

    def test_rejects_file_escape(self) -> None:
        client = self._client()
        result = asyncio.run(
            client.call_tool("inspect_pt_without_pyg", {"filepath": "../secret.pt"})
        )
        self.assertFalse(result["success"])

    def test_install_replaces_unconnected_legacy_client(self) -> None:
        class Agent:
            _mcp_connected = False
            _mcp_client = object()
            _mcp_loop = object()
            _tool_specs = [1]
            _visible_tool_specs = [1]
            _mcp_visible_tool_specs = [1]
            _tool_map = {"old": 1}
            _tool_selector = object()

        class Runtime:
            _agent = Agent()

        runtime = Runtime()
        client = install_benchmark_skill_client(
            runtime,
            skills_dir=self.skills,
            dataset_path=self.dataset,
            environment={},
            work_dir=self.work,
        )
        self.assertIs(runtime._agent._mcp_client, client)
        self.assertEqual(runtime._agent._tool_map, {})


if __name__ == "__main__":
    unittest.main()

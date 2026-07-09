from __future__ import annotations

import asyncio
import os
import threading
import tempfile
import time
import unittest
from unittest import mock
import numpy as np

from fastapi.testclient import TestClient

from graph_data_service.app import GraphDataService, create_app
from graph_data_service.graph_data import GraphData


class GraphDataAppTest(unittest.TestCase):
    def setUp(self) -> None:
        async def fake_text_graph_llm(_prompt: str) -> str:
            return (
                "<triples>\n"
                "Alice | knows | Bob\n"
                "Bob | works at | OpenAI\n"
                "</triples>"
            )

        self.service = GraphDataService(text_graph_llm_factory=fake_text_graph_llm)
        self.client = TestClient(create_app(self.service))

    def test_health_and_load_graph(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

        sid = "s-graph-data"
        created = self.client.post(f"/sessions/{sid}")
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()["success"])

        loaded = self.client.post(
            f"/sessions/{sid}/graph/load_text",
            json={"edge_text": "A B 1\nB C 2"},
        )
        self.assertEqual(loaded.status_code, 200)
        body = loaded.json()
        self.assertTrue(body["success"])
        self.assertIn("graph_version", body)
        self.assertIn("message", body)

        info = self.client.get(f"/sessions/{sid}/graph/info")
        self.assertEqual(info.status_code, 200)
        body = info.json()
        self.assertTrue(body["loaded"])
        self.assertLessEqual(len(body.get("nodes", [])), 10)
        self.assertIn("preview", body)
        self.assertIn("graph_info", body)
        self.assertIn("graph_ml_readiness", body)
        self.assertFalse(body["graph_info"]["has_node_feat"])
        self.assertFalse(body["graph_ml_readiness"]["node_classification"]["supported"])
        self.assertTrue(body["graph_ml_readiness"]["link_prediction"]["supported"])

    def test_graph_info_contains_metadata_and_ml_readiness(self) -> None:
        sid = "s-graph-meta"
        self.client.post(f"/sessions/{sid}")
        session = self.service.get_required(sid)

        gd = GraphData(
            num_nodes=4,
            edge_index=np.asarray([[0, 1, 2], [1, 2, 3]], dtype=np.int64),
            id2node={0: "A", 1: "B", 2: "C", 3: "D"},
            node2id={"A": 0, "B": 1, "C": 2, "D": 3},
            node_feat=np.asarray(
                [[0.1, 1.0], [0.2, 0.8], [0.4, 0.3], [0.9, 0.2]],
                dtype=np.float32,
            ),
            node_label=np.asarray(["male", "female", None, "male"], dtype=object),
            node_type=np.asarray(["person", "person", "person", "person"], dtype=object),
            edge_type=np.asarray(["friend", "friend", "colleague"], dtype=object),
            directed=True,
        )
        session.set_graph(gd)

        info = self.client.get(f"/sessions/{sid}/graph/info")
        self.assertEqual(info.status_code, 200)
        body = info.json()
        self.assertTrue(body["loaded"])
        self.assertEqual(body["graph_info"]["node_feat_dim"], 2)
        self.assertEqual(body["graph_info"]["node_label_stats"]["visible"], 3)
        self.assertEqual(body["graph_info"]["node_label_stats"]["missing"], 1)
        self.assertIn("male", body["graph_info"]["node_label_values"])
        self.assertIn("female", body["graph_info"]["node_label_values"])
        self.assertIn("person", body["graph_info"]["node_type_values"])
        self.assertIn("friend", body["graph_info"]["edge_type_values"])
        self.assertTrue(body["graph_ml_readiness"]["node_classification"]["supported"])
        self.assertTrue(body["graph_ml_readiness"]["link_prediction"]["supported"])
        self.assertTrue(body["graph_ml_readiness"]["pretrain"]["supported"])

    def test_graph_ml_tools_are_available_from_graph_data(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_ENABLE_REPORTING_TOOLS": ""}):
            tools_resp = self.client.get("/tools")
        self.assertEqual(tools_resp.status_code, 200)
        tools = tools_resp.json()
        names = {str(t.get("name")) for t in tools if isinstance(t, dict)}
        self.assertIn("train", names)
        self.assertIn("infer", names)
        self.assertIn("visualize_model_architecture", names)
        self.assertIn("load_graph_from_triples", names)
        self.assertIn("create_analysis_report", names)
        self.assertNotIn("get_train_config", names)
        self.assertNotIn("get_infer_config", names)
        load_text = next(t for t in tools if t.get("name") == "load_graph_from_text")
        self.assertIn("append", load_text["inputSchema"]["properties"])
        self.assertIn("extraction_goal", load_text["inputSchema"]["properties"])

    def test_reporting_tool_is_available_from_graph_data_when_requested(self) -> None:
        tools_resp = self.client.get("/tools?include_reporting=true")
        self.assertEqual(tools_resp.status_code, 200)
        tools = tools_resp.json()
        by_name = {str(t.get("name")): t for t in tools if isinstance(t, dict)}
        self.assertIn("create_analysis_report", by_name)

        tool = by_name["create_analysis_report"]
        self.assertEqual(tool["category"], "reporting")
        props = tool["inputSchema"]["properties"]
        self.assertIn("report_intent", props)
        self.assertIn("analysis_goal", props)
        self.assertIn("title", props)
        self.assertIn("source_results", props)
        self.assertIn("top_k", props["options"]["properties"])
        self.assertIn("include_narrative", props["options"]["properties"])
        self.assertIn("max_sections", props["options"]["properties"])

    def test_create_analysis_report_from_source_results(self) -> None:
        resp = self.client.post(
            "/sessions/no-session/tools/create_analysis_report",
            json={
                "arguments": {
                    "report_intent": "overview",
                    "analysis_goal": "总结当前图的结构特征",
                    "title": "结构报告",
                    "source_results": {
                        "get_graph_info": {
                            "loaded": True,
                            "graph_info": {
                                "num_nodes": 4,
                                "num_edges": 3,
                                "directed": False,
                                "has_edge_weight": False,
                                "has_node_feat": False,
                            },
                        }
                    },
                }
            },
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        report = body["report"]
        self.assertEqual(report["version"], "analysis_report.v1")
        self.assertEqual(report["title"], "结构报告")
        self.assertIn("structure-overview", {section["id"] for section in report["sections"]})

    def test_create_analysis_report_preserves_graph_version(self) -> None:
        sid = "s-report-version"
        self.client.post(f"/sessions/{sid}")
        self.client.post(f"/sessions/{sid}/graph/load_text", json={"edge_text": "A B\nB C\nC A"})
        before = self.client.get(f"/sessions/{sid}/graph/info").json()["graph_version"]

        resp = self.client.post(
            f"/sessions/{sid}/tools/create_analysis_report",
            json={
                "arguments": {
                    "report_intent": "analysis",
                    "analysis_goal": "查看连接分布",
                    "source_results": {
                        "degree_analysis": {
                            "success": True,
                            "directed": False,
                            "degree_stats": {"mean": 2, "max": 2, "min": 2, "median": 2},
                            "avg_degree": 2,
                            "top_degree_nodes": [
                                {"node": "A", "degree": 2},
                                {"node": "B", "degree": 2},
                                {"node": "C", "degree": 2},
                            ],
                        }
                    },
                }
            },
        )
        after = self.client.get(f"/sessions/{sid}/graph/info").json()["graph_version"]

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.assertEqual(before, after)

    def test_create_analysis_report_supplements_graph_info_when_loaded(self) -> None:
        sid = "s-report-supplement"
        self.client.post(f"/sessions/{sid}")
        self.client.post(f"/sessions/{sid}/graph/load_text", json={"edge_text": "A B\nB C"})

        resp = self.client.post(
            f"/sessions/{sid}/tools/create_analysis_report",
            json={
                "arguments": {
                    "report_intent": "analysis",
                    "analysis_goal": "查看连接分布",
                    "source_results": {
                        "degree_analysis": {
                            "success": True,
                            "directed": False,
                            "degree_stats": {"mean": 1.3, "max": 2, "min": 1, "median": 1},
                            "avg_degree": 1.3,
                            "top_degree_nodes": [
                                {"node": "B", "degree": 2},
                                {"node": "A", "degree": 1},
                            ],
                        }
                    },
                }
            },
        )

        self.assertEqual(resp.status_code, 200)
        report = resp.json()["report"]
        section_ids = {section["id"] for section in report["sections"]}
        self.assertIn("structure-overview", section_ids)
        self.assertIn("degree-distribution", section_ids)

    def test_create_analysis_report_returns_error_for_invalid_source_results(self) -> None:
        resp = self.client.post(
            "/sessions/no-session/tools/create_analysis_report",
            json={
                "arguments": {
                    "report_intent": "analysis",
                    "analysis_goal": "生成报告",
                    "source_results": [],
                }
            },
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("source_results", body["error"])

        sid = "s-report-invalid-loaded"
        self.client.post(f"/sessions/{sid}")
        self.client.post(f"/sessions/{sid}/graph/load_text", json={"edge_text": "A B\nB C"})
        loaded_resp = self.client.post(
            f"/sessions/{sid}/tools/create_analysis_report",
            json={
                "arguments": {
                    "report_intent": "analysis",
                    "analysis_goal": "生成报告",
                    "source_results": [],
                }
            },
        )
        self.assertEqual(loaded_resp.status_code, 200)
        loaded_body = loaded_resp.json()
        self.assertFalse(loaded_body["success"])
        self.assertIn("source_results", loaded_body["error"])

    def test_heavy_graph_ml_duplicate_calls_share_inflight_task(self) -> None:
        asyncio.run(self._run_heavy_graph_ml_duplicate_calls_share_inflight_task())

    async def _run_heavy_graph_ml_duplicate_calls_share_inflight_task(self) -> None:
        service = GraphDataService()
        calls = 0
        started = threading.Event()
        release = threading.Event()

        def fake_dispatch(
            session_id: str,
            tool_name: str,
            arguments: dict,
            progress_callback=None,
            user_id: str = "",
        ) -> dict:
            nonlocal calls
            calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=2.0))
            return {"success": True, "calls": calls}

        service._dispatch_graph_ml_tool = fake_dispatch
        arguments = {"task": "node_classification", "model": "GCN"}

        first = asyncio.create_task(
            service._dispatch_heavy_graph_ml_tool_once("sid", "train", arguments)
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2.0))
        second = asyncio.create_task(
            service._dispatch_heavy_graph_ml_tool_once("sid", "train", dict(arguments))
        )
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)

        release.set()
        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(calls, 1)
        self.assertEqual(first_result, {"success": True, "calls": 1})
        self.assertEqual(second_result, {"success": True, "calls": 1})

    def test_ingest_upload_then_load(self) -> None:
        sid = "s-upload-ingest"
        self.client.post(f"/sessions/{sid}")
        old_upload_dir = os.environ.get("GRAPH_DATA_UPLOAD_DIR")
        with tempfile.TemporaryDirectory() as td:
            os.environ["GRAPH_DATA_UPLOAD_DIR"] = td
            try:
                ingest = self.client.post(
                    f"/sessions/{sid}/uploads/ingest",
                    data={"file_token": "tok123", "original_filename": "edges.txt"},
                    files={"file": ("edges.txt", b"A B 1\nB C 2\n", "text/plain")},
                )
                self.assertEqual(ingest.status_code, 200)
                ingest_body = ingest.json()
                self.assertTrue(ingest_body.get("success"))
                self.assertEqual(ingest_body.get("filename"), "edges.txt")

                with mock.patch.object(self.service._file_format_classifier, "available", return_value=False):
                    loaded = self.client.post(
                        f"/sessions/{sid}/graph/load_upload",
                        json={"filename": "edges.txt", "append": False},
                    )
                self.assertEqual(loaded.status_code, 200)
                loaded_body = loaded.json()
                self.assertTrue(loaded_body.get("success"))
                self.assertIn("graph_version", loaded_body)
            finally:
                if old_upload_dir is None:
                    os.environ.pop("GRAPH_DATA_UPLOAD_DIR", None)
                else:
                    os.environ["GRAPH_DATA_UPLOAD_DIR"] = old_upload_dir

    def test_undo_graph_and_dataset_roundtrip(self) -> None:
        sid = "s-undo-dataset"
        self.client.post(f"/sessions/{sid}")

        self.client.post(f"/sessions/{sid}/graph/load_text", json={"edge_text": "A B\nB C"})
        self.client.post(f"/sessions/{sid}/graph/load_text", json={"edge_text": "A B\nB C\nC D"})

        undo_resp = self.client.post(f"/sessions/{sid}/tools/undo_graph", json={"arguments": {}})
        self.assertEqual(undo_resp.status_code, 200)
        self.assertTrue(undo_resp.json()["success"])

        save_resp = self.client.post(
            f"/sessions/{sid}/tools/save_as_dataset",
            json={"arguments": {"name": "test-dataset", "description": "roundtrip"}},
        )
        self.assertEqual(save_resp.status_code, 200)
        dataset = save_resp.json()["dataset"]
        self.assertEqual(dataset["name"], "test-dataset")

        list_resp = self.client.post(f"/sessions/{sid}/tools/list_datasets", json={"arguments": {}})
        self.assertEqual(list_resp.status_code, 200)
        self.assertTrue(any(row["dataset_id"] == dataset["dataset_id"] for row in list_resp.json()["datasets"]))

        load_resp = self.client.post(
            f"/sessions/{sid}/tools/load_dataset",
            json={"arguments": {"dataset_id": dataset["dataset_id"]}},
        )
        self.assertEqual(load_resp.status_code, 200)
        self.assertTrue(load_resp.json()["success"])

    def test_load_graph_from_text_and_retrieve_context(self) -> None:
        sid = "s-text-rag"
        self.client.post(f"/sessions/{sid}")

        text_resp = self.client.post(
            f"/sessions/{sid}/tools/load_graph_from_text",
            json={"arguments": {"text": "Alice knows Bob. Bob works at OpenAI."}},
        )
        self.assertEqual(text_resp.status_code, 200)
        payload = text_resp.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload.get("async"))
        payload = self._wait_for_job_result(sid, payload["job_id"])
        self.assertGreaterEqual(payload["num_nodes"], 2)
        self.assertEqual(payload["chunks_processed"], 1)
        self.assertEqual(payload["raw_triples"], 2)
        self.assertEqual(payload["deduped_triples"], 2)
        self.assertIn("sample_triples", payload)

        doc_resp = self.client.post(
            f"/sessions/{sid}/tools/upload_document",
            json={"arguments": {"content": "OpenAI builds models. Bob works at OpenAI."}},
        )
        self.assertEqual(doc_resp.status_code, 200)
        self.assertTrue(doc_resp.json()["success"])

        retrieve_resp = self.client.post(
            f"/sessions/{sid}/tools/retrieve_context",
            json={"arguments": {"query": "Who works at OpenAI?", "top_k": 2}},
        )
        self.assertEqual(retrieve_resp.status_code, 200)
        retrieve_payload = retrieve_resp.json()
        self.assertTrue(retrieve_payload["success"])
        self.assertGreaterEqual(len(retrieve_payload["chunks"]), 1)


    # ── 辅助方法 ─────────────────────────────────────────────────

    def _load_clustered_graph(self, sid: str) -> None:
        """加载一个含两个明显社区的图。"""
        self.client.post(f"/sessions/{sid}")
        self.client.post(
            f"/sessions/{sid}/graph/load_text",
            json={"edge_text": "A B\nB C\nC A\nD E\nE F\nF D\nC D"},
        )

    def _invoke_tool(self, sid: str, tool: str, arguments: dict) -> dict:
        resp = self.client.post(f"/sessions/{sid}/tools/{tool}", json={"arguments": arguments})
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _wait_for_job_result(self, sid: str, job_id: str, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        last_job = None
        while time.monotonic() < deadline:
            status = self._invoke_tool(sid, "get_job_status", {"job_id": job_id})
            last_job = status.get("job")
            if isinstance(last_job, dict) and last_job.get("status") in {"done", "failed", "cancelled"}:
                self.assertEqual(last_job.get("status"), "done", last_job)
                result = last_job.get("result")
                self.assertIsInstance(result, dict)
                return result
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish: {last_job}")

    # ── 新增算法测试 ──────────────────────────────────────────────

    def test_centrality_new_types(self) -> None:
        sid = "s-cent-new"
        self._load_clustered_graph(sid)
        for ctype in ("harmonic", "katz", "load", "voterank", "percolation"):
            result = self._invoke_tool(sid, "node_centrality", {"centrality_type": ctype})
            self.assertTrue(result.get("success"), f"{ctype} failed: {result}")
            self.assertIn("top_nodes", result)
            self.assertGreaterEqual(len(result["top_nodes"]), 1)

    def test_community_new_algorithms(self) -> None:
        sid = "s-comm-new"
        self._load_clustered_graph(sid)
        for algo in ("girvan_newman", "asyn_fluidc", "label_propagation_asyn"):
            result = self._invoke_tool(sid, "community_detection", {"algorithm": algo})
            self.assertTrue(result.get("success"), f"{algo} failed: {result}")
            self.assertGreaterEqual(result["num_communities"], 1)

    def test_subgraph_extraction(self) -> None:
        sid = "s-subgraph"
        self._load_clustered_graph(sid)
        ego = self._invoke_tool(sid, "subgraph_extraction", {"method": "ego_graph", "node": "A", "k": 1})
        self.assertTrue(ego["success"])
        self.assertGreaterEqual(ego["num_nodes"], 2)

        kcore = self._invoke_tool(sid, "subgraph_extraction", {"method": "k_core", "k": 1})
        self.assertTrue(kcore["success"])

        subset = self._invoke_tool(sid, "subgraph_extraction", {"method": "node_subset", "nodes": ["A", "B"]})
        self.assertTrue(subset["success"])
        self.assertEqual(subset["num_nodes"], 2)

    def test_user_subgraph_version_saved_under_session_dir(self) -> None:
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                sid = "s-subgraph-user-path"
                headers = {"X-User-ID": "alice"}
                self.client.post(f"/sessions/{sid}", headers=headers)
                self.client.post(
                    f"/sessions/{sid}/graph/load_text",
                    headers=headers,
                    json={"edge_text": "A B\nB C\nC D\nD A"},
                )
                result = self.client.post(
                    f"/sessions/{sid}/tools/subgraph_extraction",
                    headers=headers,
                    json={"arguments": {"method": "ego_graph", "node": "A", "k": 1}},
                ).json()

                self.assertTrue(result["success"])
                self.assertTrue(result["current_graph_replaced"])
                self.assertEqual(result["graph_version"], 2)

                session_dir = os.path.join(td, "users", "alice", "sessions", sid)
                self.assertTrue(os.path.exists(os.path.join(session_dir, "graph_current.npz")))
                self.assertTrue(os.path.exists(os.path.join(session_dir, "graph_versions.json")))
                self.assertTrue(os.path.exists(os.path.join(session_dir, "snapshots", "v002.npz")))
                self.assertFalse(
                    os.path.exists(
                        os.path.join(td, "users", "alice", "uploads", sid, "graph_current.npz")
                    )
                )
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root

    def test_link_prediction(self) -> None:
        sid = "s-linkpred"
        self._load_clustered_graph(sid)
        result = self._invoke_tool(sid, "link_prediction_score", {"method": "jaccard"})
        self.assertTrue(result["success"])
        self.assertIn("scores", result)

    def test_path_analysis(self) -> None:
        sid = "s-pathana"
        self._load_clustered_graph(sid)
        has = self._invoke_tool(sid, "path_analysis", {"method": "has_path", "source": "A", "target": "F"})
        self.assertTrue(has["success"])
        self.assertTrue(has["has_path"])

        paths = self._invoke_tool(
            sid, "path_analysis", {"method": "all_simple_paths", "source": "A", "target": "F", "cutoff": 5},
        )
        self.assertTrue(paths["success"])
        self.assertGreaterEqual(paths["count"], 1)

    def test_clique_analysis(self) -> None:
        sid = "s-clique"
        self._load_clustered_graph(sid)
        result = self._invoke_tool(sid, "clique_analysis", {"method": "find_cliques"})
        self.assertTrue(result["success"])
        self.assertGreaterEqual(len(result["cliques"]), 1)

    def test_reachability(self) -> None:
        sid = "s-reach"
        self._load_clustered_graph(sid)
        desc = self._invoke_tool(sid, "reachability", {"method": "descendants", "node": "A"})
        self.assertTrue(desc["success"])
        self.assertIn("nodes_preview", desc)
        self.assertIn("total_count", desc)

        anc = self._invoke_tool(sid, "reachability", {"method": "ancestors", "node": "F"})
        self.assertTrue(anc["success"])
        self.assertIn("nodes_preview", anc)
        self.assertIn("total_count", anc)

    def test_global_metrics(self) -> None:
        sid = "s-global"
        self._load_clustered_graph(sid)
        for metric in ("density", "diameter", "transitivity", "average_clustering"):
            result = self._invoke_tool(sid, "global_metrics", {"metric": metric})
            self.assertTrue(result.get("success"), f"{metric} failed: {result}")
            self.assertIn("value", result)

    def test_shortest_path_yen_k(self) -> None:
        sid = "s-yen"
        self._load_clustered_graph(sid)
        result = self._invoke_tool(
            sid, "shortest_path",
            {"source": "A", "target": "F", "algorithm": "yen_k_shortest", "k": 2},
        )
        self.assertTrue(result["success"])
        self.assertIn("paths", result)


if __name__ == "__main__":
    unittest.main()

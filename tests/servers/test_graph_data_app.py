from __future__ import annotations

import asyncio
import os
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import torch

from fastapi.testclient import TestClient

from graph_data_service.app import GraphDataService, create_app
from graph_data_service.graph_data import GraphData
from tests.fixtures.graph_classification import make_graph_classification_payload


class GraphDataAppTest(unittest.TestCase):
    def setUp(self) -> None:
        async def fake_text_graph_llm(prompt: str) -> str:
            if "文件内容样本" in prompt:
                if "A B 1" in prompt:
                    return (
                        '{"format":"edge_list","separator":"space",'
                        '"reason":"weighted edge rows","extraction_goal":"无"}'
                    )
                return (
                    '{"format":"freetext","separator":null,'
                    '"reason":"natural language","extraction_goal":"extract entities and relations"}'
                )
            return (
                "<triples>\n"
                "Alice | knows | Bob\n"
                "Bob | works at | OpenAI\n"
                "</triples>"
            )

        self.service = GraphDataService(text_graph_llm_factory=fake_text_graph_llm)
        self.client = TestClient(create_app(self.service))

    def _wait_for_job_result(self, sid: str, payload: dict) -> dict:
        if not payload.get("async"):
            return payload
        job_id = str(payload.get("job_id") or "")
        self.assertTrue(job_id)
        for _ in range(200):
            status_resp = self.client.post(
                f"/sessions/{sid}/tools/get_job_status",
                json={"arguments": {"job_id": job_id}},
            )
            self.assertEqual(status_resp.status_code, 200)
            job = status_resp.json().get("job") or {}
            status = str(job.get("status") or "")
            if status == "done":
                result = job.get("result")
                self.assertIsInstance(result, dict)
                self.assertTrue(result.get("success"), result)
                return result
            if status in {"failed", "cancelled"}:
                self.fail(f"job {job_id} ended with {status}: {job.get('message')}")
            time.sleep(0.01)
        self.fail(f"job {job_id} did not finish")

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
        tools_resp = self.client.get("/tools")
        self.assertEqual(tools_resp.status_code, 200)
        tools = tools_resp.json()
        names = {str(t.get("name")) for t in tools if isinstance(t, dict)}
        self.assertIn("train", names)
        self.assertIn("infer", names)
        self.assertIn("visualize_model_architecture", names)
        self.assertIn("load_graph_classification_dataset", names)
        self.assertIn("load_graph_from_triples", names)
        self.assertNotIn("get_train_config", names)
        self.assertNotIn("get_infer_config", names)
        load_text = next(t for t in tools if t.get("name") == "load_graph_from_text")
        self.assertIn("append", load_text["inputSchema"]["properties"])
        self.assertIn("extraction_goal", load_text["inputSchema"]["properties"])
        train = next(t for t in tools if t.get("name") == "train")
        self.assertIn(
            "graph_classification",
            train["inputSchema"]["properties"]["task"]["enum"],
        )

    def test_graph_classification_contract_errors_do_not_require_single_graph(self) -> None:
        sid = "s-graph-classification-contract"
        self.client.post(f"/sessions/{sid}")

        load_resp = self.client.post(
            f"/sessions/{sid}/tools/load_graph_classification_dataset",
            json={"arguments": {"filename": "hiv.pt", "dataset_profile": "auto"}},
        )
        self.assertEqual(load_resp.status_code, 200)
        self.assertFalse(load_resp.json()["success"])
        self.assertIn("未找到已注册上传文件", load_resp.json()["error"])

        train_resp = self.client.post(
            f"/sessions/{sid}/tools/train",
            json={"arguments": {"task": "graph_classification", "model": "GCN"}},
        )
        self.assertEqual(train_resp.status_code, 200)
        self.assertFalse(train_resp.json()["success"])
        self.assertIn("load_graph_classification_dataset", train_resp.json()["error"])

    def test_graph_classification_dataset_load_runs_as_async_job_with_progress(self) -> None:
        asyncio.run(self._run_graph_classification_dataset_load_async())

    async def _run_graph_classification_dataset_load_async(self) -> None:
        sid = "s-graph-classification-async-load"
        session = self.service.get_or_create(sid)
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "hiv.pt"
            torch.save(make_graph_classification_payload("hiv"), dataset_path)
            session.register_upload("token-hiv-async", "hiv.pt", str(dataset_path))

            initial = await self.service.dispatch_tool(
                sid,
                "load_graph_classification_dataset",
                {"filename": "hiv.pt", "dataset_profile": "hiv", "__async": True},
            )
            self.assertTrue(initial["success"])
            self.assertTrue(initial["async"])
            job_id = initial["job_id"]
            progress_updates = []
            for _ in range(200):
                status_payload = await self.service.dispatch_tool(
                    sid,
                    "get_job_status",
                    {"job_id": job_id},
                )
                progress_updates.extend(status_payload.get("progress_updates") or [])
                job = status_payload.get("job") or {}
                if job.get("status") == "done":
                    result = job["result"]
                    self.assertTrue(result["success"])
                    self.assertTrue(result["readiness"]["supported"])
                    self.assertEqual(result["dataset"]["name"], "hiv")
                    break
                if job.get("status") in {"failed", "cancelled"}:
                    self.fail(str(job))
                await asyncio.sleep(0.01)
            else:
                self.fail("graph classification dataset load job did not finish")

        self.assertIsNone(session.get_graph())
        self.assertTrue(progress_updates)
        self.assertTrue(
            any(
                update.get("kind") == "graph_classification_dataset_load_done"
                for update in progress_updates
            )
        )

    def test_delete_session_releases_graph_ml_dataset_memory(self) -> None:
        self.service.get_or_create("s-release", user_id="alice")
        with patch.object(
            self.service._graph_ml,
            "release_session",
            wraps=self.service._graph_ml.release_session,
        ) as release:
            self.service.delete_session("s-release", user_id="alice")

        release.assert_called_once_with("alice:s-release")
        self.assertNotIn("alice:s-release", self.service._sessions)

    def test_heavy_graph_ml_duplicate_calls_share_inflight_task(self) -> None:
        asyncio.run(self._run_heavy_graph_ml_duplicate_calls_share_inflight_task())

    def test_duplicate_dataset_load_calls_share_heavy_inflight_task(self) -> None:
        asyncio.run(self._run_duplicate_dataset_load_calls_share_inflight_task())

    async def _run_duplicate_dataset_load_calls_share_inflight_task(self) -> None:
        service = GraphDataService()
        calls = 0
        started = threading.Event()
        release = threading.Event()

        def fake_dispatch(
            session_id: str,
            tool_name: str,
            arguments: dict,
            user_id: str = "",
        ) -> dict:
            nonlocal calls
            calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=2.0))
            return {"success": True, "dataset_id": "gds_one", "calls": calls}

        service._dispatch_graph_ml_tool = fake_dispatch
        arguments = {"filename": "hiv.pt", "dataset_profile": "hiv"}
        first = asyncio.create_task(
            service.dispatch_tool(
                "sid-dataset-inflight",
                "load_graph_classification_dataset",
                arguments,
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2.0))
        second = asyncio.create_task(
            service.dispatch_tool(
                "sid-dataset-inflight",
                "load_graph_classification_dataset",
                dict(arguments),
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(calls, 1)
        self.assertEqual(first_result, second_result)

    async def _run_heavy_graph_ml_duplicate_calls_share_inflight_task(self) -> None:
        service = GraphDataService()
        calls = 0
        started = threading.Event()
        release = threading.Event()

        def fake_dispatch(
            session_id: str,
            tool_name: str,
            arguments: dict,
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
        payload = self._wait_for_job_result(sid, payload)
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

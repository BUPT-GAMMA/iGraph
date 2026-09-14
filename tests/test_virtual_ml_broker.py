from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np

from benchmark_platform.virtual_ml import (
    VIRTUAL_ML_BROKER_ENDPOINT_ENV,
    VIRTUAL_ML_BROKER_TOKEN_ENV,
    VIRTUAL_ML_BROKER_TOKEN_HEADER,
    VIRTUAL_ML_CONTEXT_ENV,
)
from benchmark_platform.virtual_ml_broker import ALLOWED_ACTIONS, VirtualMLBroker
from graph_analysis_skills import graph_ml_tools


_LOCAL_OPENER = build_opener(ProxyHandler({}))


class VirtualMLBrokerTests(unittest.TestCase):
    def setUp(self):
        self.features = np.arange(12, dtype=float).reshape(4, 3)
        self.edges = np.array([[0, 1, 2], [1, 2, 3]], dtype=int)

    def _write_node_inputs(self, root: Path) -> tuple[Path, Path, Path]:
        features = root / "features.npy"
        edges = root / "edges.npy"
        labels = root / "labels.npy"
        np.save(features, self.features)
        np.save(edges, self.edges)
        np.save(labels, np.array([0, 1, 0, 1]))
        return features, edges, labels

    def _post(self, environment: dict[str, str], payload: bytes, token: str | None = None):
        request = Request(
            environment[VIRTUAL_ML_BROKER_ENDPOINT_ENV],
            data=payload,
            headers={
                "Content-Type": "application/json",
                VIRTUAL_ML_BROKER_TOKEN_HEADER: (
                    environment[VIRTUAL_ML_BROKER_TOKEN_ENV]
                    if token is None
                    else token
                ),
            },
            method="POST",
        )
        return _LOCAL_OPENER.open(request, timeout=5)

    def test_environment_contains_only_opaque_access_and_removes_legacy_context(self):
        task = {"id": "task-1", "task_type": "NC", "ground_truth": 7}
        with VirtualMLBroker(task) as broker:
            access = broker.environment
            self.assertEqual(
                set(access),
                {VIRTUAL_ML_BROKER_ENDPOINT_ENV, VIRTUAL_ML_BROKER_TOKEN_ENV},
            )
            self.assertTrue(access[VIRTUAL_ML_BROKER_ENDPOINT_ENV].startswith("http://127.0.0.1:"))
            self.assertGreaterEqual(len(access[VIRTUAL_ML_BROKER_TOKEN_ENV]), 32)
            environment = broker.build_environment(
                {"KEEP": "yes", VIRTUAL_ML_CONTEXT_ENV: "must-not-survive"}
            )
            self.assertEqual(environment["KEEP"], "yes")
            self.assertNotIn(VIRTUAL_ML_CONTEXT_ENV, environment)

    def test_allowlist_matches_exactly_the_six_cli_handlers(self):
        self.assertEqual(ALLOWED_ACTIONS, frozenset(graph_ml_tools.ACTION_HANDLERS))
        self.assertEqual(len(ALLOWED_ACTIONS), 6)

    def test_cli_routes_train_and_infer_through_broker_and_records_order(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            features, edges, labels = self._write_node_inputs(root)
            with VirtualMLBroker(
                {"id": "task-1", "task_type": "NC", "ground_truth": 1}
            ) as broker:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.dict(os.environ, broker.environment, clear=True):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        train_code = graph_ml_tools.main(
                            [
                                "structural_node_embedding_classification_train",
                                "--node_features",
                                str(features),
                                "--edge_index",
                                str(edges),
                                "--labels",
                                str(labels),
                            ]
                        )
                self.assertEqual(train_code, 0)
                self.assertEqual(stderr.getvalue(), "")
                model_state = json.loads(stdout.getvalue())
                model_path = root / "model.json"
                model_path.write_text(json.dumps(model_state), encoding="utf-8")

                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.dict(os.environ, broker.environment, clear=True):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        infer_code = graph_ml_tools.main(
                            [
                                "structural_node_embedding_classification_infer",
                                "--model_state",
                                str(model_path),
                                "--node_features",
                                str(features),
                                "--edge_index",
                                str(edges),
                            ]
                        )
                self.assertEqual(infer_code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(json.loads(stdout.getvalue())["predictions"], [1, 1, 1, 1])
                trace = broker.trace

        self.assertEqual(
            [item["canonical_name"] for item in trace],
            [
                "structural_node_embedding_classification_train",
                "structural_node_embedding_classification_infer",
            ],
        )
        self.assertEqual([item["sequence"] for item in trace], [1, 2])
        self.assertEqual([item["status"] for item in trace], ["success", "success"])
        self.assertTrue(all(item["success"] for item in trace))
        self.assertTrue(all("arguments" not in item for item in trace))

    def test_unknown_action_and_handler_failure_are_authoritatively_recorded(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            features, edges, _labels = self._write_node_inputs(root)
            bad_model = root / "bad-model.json"
            bad_model.write_text(
                json.dumps({"task": "wrong", "status": "trained"}), encoding="utf-8"
            )
            with VirtualMLBroker(
                {"id": "task-2", "task_type": "NC", "ground_truth": 0}
            ) as broker:
                endpoint = broker.environment[VIRTUAL_ML_BROKER_ENDPOINT_ENV]
                token = broker.environment[VIRTUAL_ML_BROKER_TOKEN_ENV]
                with self.assertRaises(graph_ml_tools.VirtualMLBrokerClientError) as unknown:
                    graph_ml_tools._invoke_virtual_ml_broker(
                        ["not_an_action"], endpoint, token
                    )
                self.assertEqual(unknown.exception.exit_code, 2)
                with self.assertRaises(graph_ml_tools.VirtualMLBrokerClientError) as failed:
                    graph_ml_tools._invoke_virtual_ml_broker(
                        [
                            "structural_node_embedding_classification_infer",
                            "--model_state",
                            str(bad_model),
                            "--node_features",
                            str(features),
                            "--edge_index",
                            str(edges),
                        ],
                        endpoint,
                        token,
                    )
                self.assertEqual(failed.exception.exit_code, 1)
                trace = broker.trace

        self.assertEqual(trace[0]["raw_name"], "not_an_action")
        self.assertIsNone(trace[0]["canonical_name"])
        self.assertEqual(trace[0]["status"], "failed")
        self.assertEqual(
            trace[1]["canonical_name"],
            "structural_node_embedding_classification_infer",
        )
        self.assertEqual(trace[1]["status"], "failed")

    def test_wrong_token_is_rejected_without_polluting_agent_trace(self):
        payload = json.dumps(
            {"argv": ["structural_node_embedding_classification_train"]}
        ).encode("utf-8")
        with VirtualMLBroker(
            {"id": "task-auth", "task_type": "NC", "ground_truth": 1}
        ) as broker:
            with self.assertRaises(HTTPError) as caught:
                self._post(broker.environment, payload, token="wrong-token")
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            self.assertEqual(broker.trace, ())

    def test_request_size_limit_rejects_before_json_parsing_and_records_failure(self):
        with VirtualMLBroker(
            {"id": "task-size", "task_type": "NC", "ground_truth": 1},
            max_request_bytes=32,
        ) as broker:
            with self.assertRaises(HTTPError) as caught:
                self._post(broker.environment, b"{" + (b"x" * 64) + b"}")
            self.assertEqual(caught.exception.code, 413)
            caught.exception.close()
            trace = broker.trace
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["status"], "failed")
        self.assertIsNone(trace[0]["canonical_name"])

    def test_relative_npy_and_json_values_are_resolved_against_client_cwd(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir).resolve()
            arguments = [
                "structural_node_embedding_classification_infer",
                "--model_state",
                "states/model.json",
                "--node_features",
                "arrays/features.npy",
                "--edge_index",
                str(root / "already-absolute.npy"),
            ]
            resolved = graph_ml_tools._absolutize_broker_file_arguments(
                arguments, cwd=root
            )
        self.assertEqual(resolved[0:2], arguments[0:2])
        self.assertEqual(Path(resolved[2]), root / "states" / "model.json")
        self.assertEqual(Path(resolved[4]), root / "arrays" / "features.npy")
        self.assertEqual(resolved[6], arguments[6])

    def test_concurrent_brokers_keep_injected_labels_request_local(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            features, edges, _labels = self._write_node_inputs(root)
            model_path = root / "model.json"
            model_path.write_text(
                json.dumps(
                    {
                        "task": "node_classification",
                        "status": "trained",
                        "in_channels": 3,
                        "out_channels": 2,
                        "weights_checksum": 3162,
                    }
                ),
                encoding="utf-8",
            )
            arguments = [
                "structural_node_embedding_classification_infer",
                "--model_state",
                str(model_path),
                "--node_features",
                str(features),
                "--edge_index",
                str(edges),
            ]
            with VirtualMLBroker(
                {"id": "zero", "task_type": "NC", "ground_truth": 0}
            ) as zero_broker, VirtualMLBroker(
                {"id": "one", "task_type": "NC", "ground_truth": 1}
            ) as one_broker:
                barrier = threading.Barrier(2)
                original_label_lookup = graph_ml_tools.virtual_label_for

                def synchronized_label_lookup(family):
                    barrier.wait(timeout=5)
                    return original_label_lookup(family)

                with mock.patch.object(
                    graph_ml_tools,
                    "virtual_label_for",
                    side_effect=synchronized_label_lookup,
                ):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        zero_future = pool.submit(
                            graph_ml_tools._invoke_virtual_ml_broker,
                            arguments,
                            zero_broker.environment[VIRTUAL_ML_BROKER_ENDPOINT_ENV],
                            zero_broker.environment[VIRTUAL_ML_BROKER_TOKEN_ENV],
                        )
                        one_future = pool.submit(
                            graph_ml_tools._invoke_virtual_ml_broker,
                            arguments,
                            one_broker.environment[VIRTUAL_ML_BROKER_ENDPOINT_ENV],
                            one_broker.environment[VIRTUAL_ML_BROKER_TOKEN_ENV],
                        )
                        zero_result = zero_future.result(timeout=10)
                        one_result = one_future.result(timeout=10)
                self.assertEqual(zero_result["predictions"], [0, 0, 0, 0])
                self.assertEqual(one_result["predictions"], [1, 1, 1, 1])
                self.assertEqual(zero_broker.trace[0]["status"], "success")
                self.assertEqual(one_broker.trace[0]["status"], "success")


if __name__ == "__main__":
    unittest.main()

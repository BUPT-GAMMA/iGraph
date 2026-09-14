from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import benchmark_platform.runner as runner
from benchmark_platform.agent_protocol import AgentExecution
from benchmark_platform.judge_protocol import JudgeExecution
from benchmark_platform.masked_dataset import DatasetBinding, MaterializedDataset
from benchmark_platform.runner import (
    ReplayStore,
    RunnerConfig,
    _merge_authoritative_ml_trace,
    run_questions,
)


class CommandRedactionTests(unittest.TestCase):
    def test_redacts_explicit_credentials_without_misclassifying_paths(self) -> None:
        command = (
            "python",
            "/opt/secret-tools/adapter.py",
            "--provider-profile",
            "/configured/provider-profile.sh",
            "--api_key=unit-test-placeholder",
            "--model",
            "unit-model",
        )

        self.assertEqual(
            runner._redacted_command(command),
            [
                "python",
                "/opt/secret-tools/adapter.py",
                "--provider-profile",
                "<redacted>",
                "--api_key=<redacted>",
                "--model",
                "unit-model",
            ],
        )


def _task(
    identifier: str,
    dataset: str,
    ground_truth: bool,
) -> dict:
    return {
        "composite_id": f"ml/{dataset}/{identifier}",
        "id": identifier,
        "question": f"Question {identifier}",
        "category": "ml",
        "dataset": dataset,
        "task_type": "LP",
        "required_tools": ["structural_node_embedding_link_prediction"],
        "ground_truth": ground_truth,
        "context": {"task_index": 0, "node1": 1, "node2": 2},
    }


def _agent_response(answer: bool) -> dict:
    return {
        "status": "success",
        "final_answer": {"has_edge": answer},
        "reasoning": ["inspect", "train", "infer"],
        "tool_calls": [
            {
                "name": "structural_node_embedding_link_prediction_train",
                "arguments": {"node_features": "x.npy"},
                "result": {"status": "trained"},
                "success": True,
            },
            {
                "name": "structural_node_embedding_link_prediction_infer",
                "arguments": {"model_state": "model.json"},
                "result": {"status": "success", "predictions": [answer]},
                "success": True,
            },
        ],
        "transcript": [{"role": "assistant", "content": "done"}],
    }


class ReplayStoreTests(unittest.TestCase):
    def test_records_resolve_by_composite_id_and_attempt(self):
        task = _task("task-1", "cora", True)
        records = {
            "responses": [
                {
                    "question_id": task["composite_id"],
                    "attempt": 1,
                    "response": {"final_answer": {"has_edge": False}},
                },
                {
                    "question_id": task["composite_id"],
                    "attempt": 2,
                    "response": {"final_answer": {"has_edge": True}},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            replay_path = Path(temporary_dir) / "replay.json"
            replay_path.write_text(json.dumps(records), encoding="utf-8")
            store = ReplayStore(replay_path)
            first = store.resolve(task, 1, 0)
            second = store.resolve(task, 2, 1)
        self.assertEqual(first["final_answer"], {"has_edge": False})
        self.assertEqual(second["final_answer"], {"has_edge": True})

    def test_record_preserves_captured_trajectory_without_changing_resolve_contract(self):
        task = _task("task-captured", "cora", True)
        agent = {"status": "success", "final_answer": {"has_edge": True}}
        captured_calls = [
            {
                "canonical_name": "structural_node_embedding_link_prediction_train",
                "raw_name": "exec_command",
                "status": "success",
            },
            {
                "canonical_name": "structural_node_embedding_link_prediction_infer",
                "raw_name": "exec_command",
                "status": "success",
            },
        ]
        records = [
            {
                "question_id": task["composite_id"],
                "attempt": 1,
                "agent": agent,
                "trajectory": {"calls": captured_calls},
            }
        ]
        with tempfile.TemporaryDirectory() as temporary_dir:
            replay_path = Path(temporary_dir) / "replay.json"
            replay_path.write_text(json.dumps(records), encoding="utf-8")
            store = ReplayStore(replay_path)
            resolved = store.resolve(task, 1, 0)
            record = store._resolve_record(task, 1, 0)

        self.assertEqual(resolved, agent)
        self.assertEqual(list(record.trajectory_calls or ()), captured_calls)

    def test_replay_judge_requires_successful_integer_binary_score(self):
        for score in (0, 1):
            with self.subTest(valid_score=score):
                result = runner._successful_replay_judge(
                    {"status": "success", "score": score, "error": None},
                    input_location="record.judge",
                )
                self.assertIsNotNone(result)
                self.assertEqual(result.score, score)

        invalid = [
            {"status": "success", "score": True},
            {"status": "success", "score": False},
            {"status": "success", "score": 1.0},
            {"status": "success", "score": "1"},
            {"status": "success", "score": 2},
            {"status": "failed", "score": 1},
            {"status": "success", "score": 1, "error": {"type": "error"}},
        ]
        for judge in invalid:
            with self.subTest(invalid_judge=judge):
                self.assertIsNone(
                    runner._successful_replay_judge(
                        judge,
                        input_location="record.judge",
                    )
                )

        self.assertTrue(
            runner._complete_judge_decision(
                {"status": "success", "score": 1}
            )
        )
        self.assertTrue(
            runner._complete_judge_decision(
                {
                    "status": "not_called",
                    "score": 0,
                    "source": "agent_execution_failure",
                }
            )
        )
        self.assertFalse(
            runner._complete_judge_decision({"status": "failed", "score": 1})
        )
        self.assertFalse(
            runner._complete_judge_decision(
                {"status": "success", "score": True}
            )
        )

    def test_result_artifact_judge_is_not_mixed_with_summary_judge(self):
        task = _task("task-judge-origin", "cora", True)
        artifact_agent = {
            "status": "success",
            "final_answer": {"has_edge": True},
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            artifact = root / "attempt.json"
            artifact.write_text(
                json.dumps(
                    {
                        "agent": artifact_agent,
                        "trajectory": {"calls": []},
                        "evaluation": {
                            "judge": {"status": "success", "score": 0}
                        },
                    }
                ),
                encoding="utf-8",
            )
            replay_path = root / "result.json"
            replay_path.write_text(
                json.dumps(
                    {
                        "questions": [
                            {
                                "question": {
                                    "composite_id": task["composite_id"]
                                },
                                "attempts": [
                                    {
                                        "attempt": 1,
                                        "agent": {"status": "success"},
                                        "judge": {
                                            "status": "success",
                                            "score": 1,
                                        },
                                        "attempt_result_file": artifact.name,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            store = ReplayStore(replay_path)
            record = store._resolve_record(task, 1, 0)

            summary_path = root / "summary-only-result.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "questions": [
                            {
                                "question": {
                                    "composite_id": task["composite_id"]
                                },
                                "attempts": [
                                    {
                                        "attempt": 1,
                                        "agent": artifact_agent,
                                        "judge": {
                                            "status": "success",
                                            "score": 1,
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            summary_record = ReplayStore(summary_path)._resolve_record(
                task, 1, 0
            )

        self.assertEqual(record.agent_payload, artifact_agent)
        self.assertIsNotNone(record.successful_judge)
        self.assertEqual(record.successful_judge.score, 0)
        self.assertEqual(
            record.successful_judge.input_location,
            "attempt_artifact.evaluation.judge",
        )
        self.assertIsNotNone(summary_record.successful_judge)
        self.assertEqual(summary_record.successful_judge.score, 1)
        self.assertEqual(
            summary_record.successful_judge.input_location,
            "result.attempt.judge",
        )

    def test_ordered_jsonl_falls_back_to_flat_index(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            replay_path = Path(temporary_dir) / "replay.jsonl"
            replay_path.write_text(
                '{"final_answer":1}\n{"final_answer":2}\n', encoding="utf-8"
            )
            store = ReplayStore(replay_path)
            resolved = store.resolve(_task("missing", "cora", True), 1, 1)
        self.assertEqual(resolved["final_answer"], 2)


class ReplayCleanupRecoveryTests(unittest.TestCase):
    @staticmethod
    def execution(
        error: dict,
        *,
        final_answer: object = False,
        transcript: list | None = None,
    ) -> AgentExecution:
        return AgentExecution(
            status="failed",
            final_answer=final_answer,
            transcript=(
                [{"role": "assistant", "content": "captured answer"}]
                if transcript is None
                else transcript
            ),
            error=error,
        )

    def test_recovers_current_and_legacy_structured_cleanup_errors(self):
        errors = [
            {
                "type": "ContainerCleanupError",
                "message": "permission denied: virtual_ml.py",
            },
            {
                "type": "dict",
                "message": (
                    "{'type': 'ContainerCleanupError', "
                    "'message': 'permission denied: virtual_ml.py'}"
                ),
            },
        ]
        for error in errors:
            with self.subTest(error=error):
                execution = self.execution(error)
                recovery = runner._recover_replayed_cleanup_failure(execution)

                self.assertIsNotNone(recovery)
                self.assertEqual(execution.status, "success")
                self.assertIsNone(execution.error)
                self.assertEqual(
                    execution.warnings[0]["type"],
                    "RecoveredContainerCleanupError",
                )
                self.assertEqual(
                    recovery["original_error"]["type"],
                    "ContainerCleanupError",
                )

    def test_recovery_rejects_substrings_empty_answers_and_empty_transcripts(self):
        exact_error = {
            "type": "ContainerCleanupError",
            "message": "permission denied: virtual_ml.py",
        }
        cases = [
            self.execution(
                {
                    "type": "dict",
                    "message": "log mentioned ContainerCleanupError but is not a dict",
                }
            ),
            self.execution({"type": "OtherError", "message": "cleanup failed"}),
            self.execution(exact_error, final_answer=""),
            self.execution(exact_error, transcript=[]),
        ]
        for execution in cases:
            with self.subTest(execution=execution):
                recovery = runner._recover_replayed_cleanup_failure(execution)

                self.assertIsNone(recovery)
                self.assertEqual(execution.status, "failed")
                self.assertIsNotNone(execution.error)
                self.assertEqual(execution.warnings, [])

        execution = self.execution(exact_error)
        recovery = runner._recover_replayed_cleanup_failure(
            execution,
            {"execution_error": "agent command failed"},
        )
        self.assertIsNone(recovery)
        self.assertEqual(execution.status, "failed")


class ParallelRunnerTests(unittest.TestCase):
    def test_authoritative_ml_replacement_preserves_non_ml_interleave(self):
        reported = [
            {
                "name": "structural_node_embedding_link_prediction_train",
                "success": True,
            },
            {"name": "get_density", "success": True},
            {
                "name": "structural_node_embedding_link_prediction_infer",
                "success": True,
            },
        ]
        broker = [
            {
                "canonical_name": "structural_node_embedding_link_prediction_train",
                "success": True,
            },
            {
                "canonical_name": "structural_node_embedding_link_prediction_infer",
                "success": True,
            },
        ]

        merged = _merge_authoritative_ml_trace(reported, broker)

        self.assertEqual(
            [call["canonical_name"] for call in merged],
            [
                "structural_node_embedding_link_prediction_train",
                "get_density",
                "structural_node_embedding_link_prediction_infer",
            ],
        )

    def test_no_virtual_ml_does_not_inject_bundled_virtual_tool(self):
        task = _task("task-real-adapter", "cora", False)
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"")
            execution = AgentExecution(
                status="success",
                final_answer={"has_edge": False},
                tool_calls=_agent_response(False)["tool_calls"],
            )
            with (
                mock.patch.object(
                    runner,
                    "_dataset_binding",
                    return_value=DatasetBinding(
                        base_path=(dataset_root / "cora.pt").resolve(),
                        category="ml",
                        dataset="cora",
                    ),
                ),
                mock.patch(
                    "benchmark_platform.runner.execute_agent_command",
                    return_value=execution,
                ) as execute,
            ):
                result, exit_code = run_questions(
                    [task],
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        agent_command=("real-agent-adapter",),
                        virtual_graph_ml=False,
                    ),
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "completed")
        environment = execute.call_args.kwargs["environment"]
        self.assertNotIn("IGRAPH_GRAPH_ML_TOOL", environment)
        self.assertNotIn("IGRAPH_VIRTUAL_ML_BROKER_ENDPOINT", environment)
        self.assertNotIn("IGRAPH_VIRTUAL_ML_BROKER_TOKEN", environment)

    def test_successful_agent_warning_is_aggregated_and_sets_error_exit(self):
        task = _task("task-warning", "cora", False)
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"")
            execution = AgentExecution(
                status="success",
                final_answer={"has_edge": False},
                warnings=[
                    {
                        "type": "ContainerCleanupWarning",
                        "message": "workspace cleanup was incomplete",
                    }
                ],
            )
            with (
                mock.patch.object(
                    runner,
                    "_dataset_binding",
                    return_value=DatasetBinding(
                        base_path=(dataset_root / "cora.pt").resolve(),
                        category="ml",
                        dataset="cora",
                    ),
                ),
                mock.patch(
                    "benchmark_platform.runner.execute_agent_command",
                    return_value=execution,
                ),
            ):
                result, exit_code = run_questions(
                    [task],
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        agent_command=("warning-agent",),
                        virtual_graph_ml=False,
                    ),
                )

        self.assertEqual(exit_code, 5)
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertTrue(result["questions"][0]["execution_success"])
        self.assertEqual(result["summary"]["successful_attempts"], 1)
        self.assertEqual(result["summary"]["agent_warning_attempt_count"], 1)
        self.assertEqual(
            result["questions"][0]["attempts"][0]["agent"]["warnings"][0][
                "type"
            ],
            "ContainerCleanupWarning",
        )

    def test_masked_attempts_receive_distinct_ephemeral_datasets(self):
        task = _task("task-isolated", "cora", False)
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            base_path = root / "dataset" / "cora.pt"
            masked_path = root / "questions" / "ml" / "cora" / "pure_ml_bench_masked_data.pt"
            base_path.parent.mkdir(parents=True)
            masked_path.parent.mkdir(parents=True)
            base_path.write_bytes(b"base")
            masked_path.write_bytes(b"masked")
            binding = DatasetBinding(
                base_path=base_path,
                masked_path=masked_path,
                category="ml",
                dataset="cora",
            )
            materialized_paths: list[Path] = []
            observed_paths: list[Path] = []
            path_lock = threading.Lock()

            def fake_materialize(_binding, _task_value, destination):
                path = Path(destination).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"attempt={path.parent.name}", encoding="utf-8")
                with path_lock:
                    materialized_paths.append(path)
                return MaterializedDataset(
                    path=path,
                    base_path=base_path.resolve(),
                    masked_path=masked_path.resolve(),
                    masked_index=0,
                    task_kind="LP",
                    applied_field="edge_index",
                    masked_cache_hit=bool(materialized_paths[:-1]),
                )

            def fake_execute(_command, envelope, **_kwargs):
                path = Path(envelope["dataset_path"]).resolve()
                self.assertTrue(path.is_file())
                self.assertNotIn("task_index", envelope["context"])
                self.assertNotIn("masked_index", envelope["dataset_input"])
                with path_lock:
                    observed_paths.append(path)
                return AgentExecution(status="success", final_answer={"has_edge": False})

            with (
                mock.patch.object(
                    runner,
                    "_dataset_binding",
                    return_value=binding,
                ),
                mock.patch(
                    "benchmark_platform.runner.materialize_task_dataset",
                    side_effect=fake_materialize,
                ) as materialize,
                mock.patch(
                    "benchmark_platform.runner.execute_agent_command",
                    side_effect=fake_execute,
                ),
            ):
                result, exit_code = run_questions(
                    [task],
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=base_path.parent,
                        task_parallelism=1,
                        intra_task_parallelism=2,
                        attempts_per_question=2,
                        agent_command=("fake-agent",),
                        virtual_graph_ml=False,
                    ),
                )

            self.assertEqual(
                exit_code,
                0,
                {
                    "status": result.get("status"),
                    "attempt": result.get("questions", [{}])[0].get("attempts", [{}])[0],
                },
            )
            self.assertEqual(materialize.call_count, 2)
            self.assertEqual(len(set(materialized_paths)), 2)
            self.assertEqual(set(observed_paths), set(materialized_paths))
            self.assertTrue(all(not path.exists() for path in materialized_paths))
            for attempt in result["questions"][0]["attempts"]:
                detail = json.loads(
                    Path(attempt["attempt_result_file"]).read_text(encoding="utf-8")
                )
                self.assertTrue(detail["dataset_input"]["ephemeral"])
                self.assertTrue(detail["dataset_input"]["removed_after_attempt"])
                self.assertEqual(
                    detail["dataset_input"]["masked_path"], str(masked_path.resolve())
                )
                self.assertFalse(detail["dataset_input"]["provenance_deferred"])

    def test_invalid_accepted_path_fails_preflight_before_agent_call(self):
        task = _task("task-bad-alternative", "cora", False)
        task["category"] = "theory"
        task["composite_id"] = "theory/cora/task-bad-alternative"
        task["context"] = {
            "accepted_tool_paths": [["nx.not_a_real_action"]]
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"")
            with mock.patch(
                "benchmark_platform.runner.execute_agent_command"
            ) as execute:
                with self.assertRaisesRegex(
                    ValueError, "unknown benchmark graph tool"
                ):
                    run_questions(
                        [task],
                        RunnerConfig(
                            output_dir=root / "output",
                            dataset_root=dataset_root,
                            agent_command=("unused-agent",),
                            virtual_graph_ml=False,
                        ),
                    )
        execute.assert_not_called()

    def test_command_mode_scores_authoritative_virtual_ml_broker_trace(self):
        task = _task("task-command", "cora", False)
        # This mirrors the real pure-ML corpus: task_type carries the workflow
        # and required_tools is intentionally empty.
        task["required_tools"] = []
        fake_agent = (
            Path(__file__).resolve().parent / "fixtures" / "fake_question_agent.py"
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"")

            with mock.patch.object(
                runner,
                "_dataset_binding",
                return_value=DatasetBinding(
                    base_path=(dataset_root / "cora.pt").resolve(),
                    category="ml",
                    dataset="cora",
                ),
            ):
                result, exit_code = run_questions(
                    [task],
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        task_parallelism=1,
                        intra_task_parallelism=1,
                        attempts_per_question=1,
                        agent_command=(sys.executable, str(fake_agent)),
                        virtual_graph_ml=True,
                        evaluate=True,
                        judge_command=(
                            sys.executable,
                            str(
                                Path(__file__).resolve().parent
                                / "fixtures"
                                / "fake_binary_judge.py"
                            ),
                        ),
                    ),
                )

            self.assertEqual(
                exit_code,
                0,
                {
                    "status": result.get("status"),
                    "attempt": result.get("questions", [{}])[0].get("attempts", [{}])[0],
                },
            )
            self.assertEqual(result["status"], "completed")
            question = result["questions"][0]
            self.assertTrue(question["execution_success"])
            self.assertTrue(question["ground_truth_pass"])
            self.assertTrue(question["tool_path_pass"])
            summary = question["attempts"][0]
            self.assertEqual(summary["agent"]["status"], "success")
            self.assertTrue(summary["scores"]["ground_truth"]["exact"])
            self.assertTrue(summary["scores"]["tool_path"]["strict"]["exact"])
            self.assertTrue(summary["scores"]["tool_path"]["successful"]["exact"])

            detail_path = Path(summary["attempt_result_file"])
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
            self.assertEqual(detail["agent"]["final_answer"], {"has_edge": False})
            self.assertEqual(
                detail["agent"]["reasoning"],
                ["virtual_link_prediction_train", "virtual_link_prediction_infer"],
            )
            self.assertEqual(detail["agent"]["tool_calls"], [])

            provenance = detail["tool_trace_provenance"]
            self.assertEqual(provenance["reported_by_agent"], 0)
            self.assertTrue(provenance["scoring_uses_authoritative_virtual_ml"])
            broker_trace = provenance["authoritative_virtual_ml"]
            self.assertEqual(
                [call["canonical_name"] for call in broker_trace],
                [
                    "structural_node_embedding_link_prediction_train",
                    "structural_node_embedding_link_prediction_infer",
                ],
            )
            self.assertEqual([call["sequence"] for call in broker_trace], [1, 2])
            self.assertTrue(all(call["success"] for call in broker_trace))
            expected_path = [
                "structural_node_embedding_link_prediction_train",
                "structural_node_embedding_link_prediction_infer",
            ]
            self.assertEqual(
                detail["scores"]["tool_path"]["strict_actual"], expected_path
            )
            self.assertEqual(
                detail["scores"]["tool_path"]["successful_actual"], expected_path
            )
            self.assertTrue(detail["scores"]["tool_path"]["strict"]["exact"])
            self.assertTrue(
                detail["scores"]["tool_path"]["successful"]["exact"]
            )

            run_dir = Path(result["result_file"]).parent
            self.assertEqual(list(run_dir.rglob("*virtual_ml_context*")), [])

    def test_result_replay_prefers_captured_authoritative_trajectory(self):
        task = _task("task-captured-result", "cora", False)
        expected_path = [
            "structural_node_embedding_link_prediction_train",
            "structural_node_embedding_link_prediction_infer",
        ]
        captured_calls = [
            {
                "index": index,
                "source_index": index,
                "raw_name": "exec_command",
                "canonical_name": name,
                "status": "success",
                "source": "normalized",
            }
            for index, name in enumerate(expected_path)
        ]
        reported_agent = {
            "status": "success",
            "final_answer": {"has_edge": False},
            # This deliberately conflicts with the finished captured trace.
            # Replay must not merge it into the authoritative path.
            "tool_calls": [
                {
                    "name": "get_density",
                    "result": {"status": "success"},
                    "success": True,
                }
            ],
            "transcript": [{"role": "assistant", "content": "done"}],
        }

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"fixture")

            prior_dir = root / "prior-run"
            prior_dir.mkdir()
            artifact_path = prior_dir / "attempt-001.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "question_id": task["composite_id"],
                        "attempt": 1,
                        "agent": reported_agent,
                        "trajectory": {"calls": captured_calls},
                    }
                ),
                encoding="utf-8",
            )
            replay_path = prior_dir / "result.json"
            replay_path.write_text(
                json.dumps(
                    {
                        "questions": [
                            {
                                "question": {
                                    "composite_id": task["composite_id"]
                                },
                                "attempts": [
                                    {
                                        "attempt": 1,
                                        "agent": {
                                            "status": "success",
                                            "error": None,
                                        },
                                        "attempt_result_file": artifact_path.name,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                runner,
                "_dataset_binding",
                return_value=DatasetBinding(
                    base_path=(dataset_root / "cora.pt").resolve(),
                    category="ml",
                    dataset="cora",
                ),
            ):
                result, exit_code = run_questions(
                    [task],
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        replay_file=replay_path,
                        virtual_graph_ml=True,
                        evaluate=True,
                        judge_command=(
                            sys.executable,
                            str(
                                Path(__file__).resolve().parent
                                / "fixtures"
                                / "fake_binary_judge.py"
                            ),
                        ),
                    ),
                )

            self.assertEqual(exit_code, 0)
            summary = result["questions"][0]["attempts"][0]
            self.assertTrue(summary["scores"]["tool_path"]["strict"]["exact"])
            detail = json.loads(
                Path(summary["attempt_result_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(detail["trajectory"]["strict_path"], expected_path)
            self.assertEqual(
                detail["scores"]["tool_path"]["strict_actual"], expected_path
            )
            self.assertEqual(
                [call["name"] for call in detail["agent"]["tool_calls"]],
                ["get_density"],
            )
            provenance = detail["tool_trace_provenance"]
            self.assertTrue(provenance["captured_replay_trajectory_used"])
            self.assertTrue(
                provenance["scoring_uses_captured_replay_trajectory"]
            )
            self.assertEqual(provenance["trajectory_source"], "captured_replay")
            self.assertEqual(provenance["captured_replay_call_count"], 2)

    def test_explicit_replay_recovery_scores_cleanup_only_failure(self):
        task = _task("task-cleanup-recovery", "cora", False)
        expected_path = [
            "structural_node_embedding_link_prediction_train",
            "structural_node_embedding_link_prediction_infer",
        ]
        replay_record = {
            "question_id": task["composite_id"],
            "attempt": 1,
            "agent": {
                "status": "failed",
                "final_answer": {"has_edge": False},
                "tool_calls": [],
                "transcript": [
                    {"role": "assistant", "content": "captured answer"}
                ],
                # This is the shape persisted by the legacy normalizer.
                "error": {
                    "type": "dict",
                    "message": (
                        "{'type': 'ContainerCleanupError', "
                        "'message': \"Permission denied: 'virtual_ml.py'\"}"
                    ),
                },
            },
            "trajectory": {
                "calls": [
                    {
                        "canonical_name": name,
                        "raw_name": "exec_command",
                        "status": "success",
                    }
                    for name in expected_path
                ]
            },
        }

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"fixture")
            replay_path = root / "replay.json"
            replay_path.write_text(json.dumps([replay_record]), encoding="utf-8")

            with mock.patch.object(
                runner,
                "_dataset_binding",
                return_value=DatasetBinding(
                    base_path=(dataset_root / "cora.pt").resolve(),
                    category="ml",
                    dataset="cora",
                ),
            ):
                result, exit_code = run_questions(
                    [task],
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        replay_file=replay_path,
                        recover_cleanup_failures=True,
                        virtual_graph_ml=True,
                        evaluate=True,
                        judge_command=(
                            sys.executable,
                            str(
                                Path(__file__).resolve().parent
                                / "fixtures"
                                / "fake_binary_judge.py"
                            ),
                        ),
                    ),
                )

            self.assertEqual(exit_code, 5)
            self.assertEqual(
                result["status"],
                "completed_with_errors",
                result.get("questions", [{}])[0].get("attempts", [{}])[0],
            )
            self.assertTrue(result["questions"][0]["execution_success"])
            self.assertEqual(result["summary"]["successful_attempts"], 1)
            self.assertEqual(result["summary"]["agent_warning_attempt_count"], 1)
            self.assertEqual(result["summary"]["tool_selection_accuracy"], 1.0)
            self.assertEqual(result["summary"]["success_rate"], 1.0)
            summary = result["questions"][0]["attempts"][0]
            self.assertEqual(summary["agent"]["status"], "success")
            self.assertIsNone(summary["agent"]["error"])
            self.assertEqual(
                summary["agent"]["warnings"][0]["type"],
                "RecoveredContainerCleanupError",
            )
            self.assertTrue(summary["replay_recovery"]["applied"])
            detail = json.loads(
                Path(summary["attempt_result_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(detail["trajectory"]["strict_path"], expected_path)
            self.assertEqual(
                detail["replay_recovery"]["original_error"]["type"],
                "ContainerCleanupError",
            )

    def test_replay_reuses_only_successful_judges_and_retries_the_rest(self):
        tasks = [
            _task("judge-reused", "cora", True),
            _task("judge-failed", "cora", False),
            _task("judge-missing", "cora", False),
        ]
        replay_records = [
            {
                "question_id": tasks[0]["composite_id"],
                "agent": _agent_response(True),
                "judge": {
                    "status": "success",
                    "score": 1,
                    "source": "llm_as_a_judge",
                    "duration_seconds": 3.5,
                },
            },
            {
                "question_id": tasks[1]["composite_id"],
                "agent": _agent_response(False),
                "judge": {"status": "failed", "score": None},
            },
            {
                "question_id": tasks[2]["composite_id"],
                "agent": _agent_response(False),
            },
        ]

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"fixture")
            replay_path = root / "replay.json"
            replay_path.write_text(
                json.dumps(replay_records),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    runner,
                    "_dataset_binding",
                    return_value=DatasetBinding(
                        base_path=(dataset_root / "cora.pt").resolve(),
                        category="ml",
                        dataset="cora",
                    ),
                ),
                mock.patch.object(
                    runner,
                    "execute_judge_command",
                    return_value=JudgeExecution(status="success", score=0),
                ) as judge_mock,
            ):
                result, exit_code = run_questions(
                    tasks,
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        replay_file=replay_path,
                        reuse_successful_judge_results=True,
                        virtual_graph_ml=False,
                        evaluate=True,
                        judge_command=("fake-judge",),
                    ),
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(judge_mock.call_count, 2)
        self.assertEqual(result["summary"]["judge_reused_count"], 1)
        self.assertEqual(result["summary"]["judge_invocation_count"], 2)
        self.assertEqual(result["summary"]["judge_target_count"], 3)
        self.assertEqual(result["summary"]["judge_completed_count"], 3)
        self.assertAlmostEqual(result["summary"]["success_rate"], 1 / 3)
        judges = [question["attempts"][0]["judge"] for question in result["questions"]]
        self.assertTrue(judges[0]["reused"])
        self.assertEqual(judges[0]["source"], "llm_as_a_judge")
        self.assertEqual(
            judges[0]["provenance"]["result_origin"],
            "replay",
        )
        self.assertEqual(
            judges[0]["provenance"]["input_location"],
            "record.judge",
        )
        self.assertFalse(judges[1]["reused"])
        self.assertFalse(judges[2]["reused"])

    def test_replay_judge_reuse_is_disabled_by_default(self):
        task = _task("judge-not-reused", "cora", True)
        replay_record = {
            "question_id": task["composite_id"],
            "agent": _agent_response(True),
            "judge": {"status": "success", "score": 1},
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"fixture")
            replay_path = root / "replay.json"
            replay_path.write_text(json.dumps([replay_record]), encoding="utf-8")
            with (
                mock.patch.object(
                    runner,
                    "_dataset_binding",
                    return_value=DatasetBinding(
                        base_path=(dataset_root / "cora.pt").resolve(),
                        category="ml",
                        dataset="cora",
                    ),
                ),
                mock.patch.object(
                    runner,
                    "execute_judge_command",
                    return_value=JudgeExecution(status="success", score=0),
                ) as judge_mock,
            ):
                result, exit_code = run_questions(
                    [task],
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        replay_file=replay_path,
                        virtual_graph_ml=False,
                        evaluate=True,
                        judge_command=("fake-judge",),
                    ),
                )

        self.assertEqual(exit_code, 0)
        judge_mock.assert_called_once()
        self.assertEqual(result["summary"]["judge_reused_count"], 0)
        self.assertEqual(result["summary"]["judge_invocation_count"], 1)
        self.assertEqual(result["summary"]["success_rate"], 0.0)

    def test_replay_run_persists_detailed_results_for_every_attempt(self):
        tasks = [
            _task("task-1", "cora", True),
            _task("task-2", "hiv", False),
        ]
        replay_records = []
        for task in tasks:
            for attempt in (1, 2):
                replay_records.append(
                    {
                        "question_id": task["composite_id"],
                        "attempt": attempt,
                        "response": _agent_response(task["ground_truth"]),
                    }
                )

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            output_dir = root / "output"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"fixture")
            (dataset_root / "hiv.pt").write_bytes(b"fixture")
            replay_path = root / "replay.json"
            replay_path.write_text(json.dumps(replay_records), encoding="utf-8")

            with mock.patch.object(
                runner,
                "_dataset_binding",
                side_effect=lambda _config, current: DatasetBinding(
                    base_path=(dataset_root / f"{current['dataset']}.pt").resolve(),
                    category="ml",
                    dataset=current["dataset"],
                ),
            ):
                result, exit_code = run_questions(
                    tasks,
                    RunnerConfig(
                        output_dir=output_dir,
                        dataset_root=dataset_root,
                        task_parallelism=2,
                        intra_task_parallelism=2,
                        attempts_per_question=2,
                        replay_file=replay_path,
                        virtual_graph_ml=True,
                        evaluate=True,
                        judge_command=(
                            sys.executable,
                            str(
                                Path(__file__).resolve().parent
                                / "fixtures"
                                / "fake_binary_judge.py"
                            ),
                        ),
                    ),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["summary"]["questions"], 2)
            self.assertEqual(result["summary"]["attempts"], 4)
            self.assertEqual(result["summary"]["successful_attempts"], 4)
            self.assertEqual(
                [item["question"]["id"] for item in result["questions"]],
                ["task-1", "task-2"],
            )
            result_path = Path(result["result_file"])
            self.assertTrue(result_path.is_file())
            persisted_run = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted_run["schema"], result["schema"])
            for persisted_question in persisted_run["questions"]:
                for persisted_attempt in persisted_question["attempts"]:
                    self.assertEqual(
                        set(persisted_attempt["agent"]),
                        {"status", "error", "warnings"},
                    )
                    self.assertNotIn(
                        "normalized_tool_calls", persisted_attempt["scores"]["tool_path"]
                    )

            for question in result["questions"]:
                self.assertNotIn("ground_truth", question["question"])
                self.assertNotIn("ground_truth_sha256", question["question"])
                self.assertNotIn("required_tools_sha256", question["question"])
                self.assertEqual(len(question["attempts"]), 2)
                self.assertTrue(question["ground_truth_pass"])
                self.assertTrue(question["tool_path_pass"])
                for attempt in question["attempts"]:
                    self.assertEqual(
                        attempt["agent"],
                        {"status": "success", "error": None, "warnings": []},
                    )
                    self.assertNotIn("reasoning", attempt["agent"])
                    self.assertNotIn("tool_calls", attempt["agent"])
                    detail_path = Path(attempt["attempt_result_file"])
                    self.assertTrue(detail_path.is_file())
                    detail = json.loads(detail_path.read_text(encoding="utf-8"))
                    self.assertEqual(detail["question_id"], question["question"]["composite_id"])
                    self.assertEqual(detail["agent"]["reasoning"], ["inspect", "train", "infer"])
                    self.assertEqual(len(detail["agent"]["tool_calls"]), 2)
                    self.assertTrue(detail["scores"]["ground_truth"]["exact"])
                    self.assertTrue(detail["scores"]["tool_path"]["successful"]["exact"])

            legacy_context_files = list(
                Path(result["result_file"]).parent.glob(
                    "workspaces/*/attempt-*/virtual_ml_context.json"
                )
            )
            self.assertEqual(legacy_context_files, [])

    def test_outer_question_and_inner_attempt_parallelism_overlap(self):
        tasks = [
            _task("task-a", "cora", True),
            _task("task-b", "hiv", False),
        ]
        all_workers_started = threading.Barrier(4, timeout=10)
        calls: list[tuple[str, int, int]] = []
        calls_lock = threading.Lock()

        def fake_run_attempt(task, task_index, attempt, config, run_dir, replay_store):
            with calls_lock:
                calls.append(
                    (task["composite_id"], attempt, threading.get_ident())
                )
            all_workers_started.wait()
            return {
                "question_id": task["composite_id"],
                "attempt": attempt,
                "agent": {"status": "success"},
                "trajectory": {
                    "strict_path": [],
                    "successful_path": [],
                    "call_count": 0,
                },
            }

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "cora.pt").write_bytes(b"")
            (dataset_root / "hiv.pt").write_bytes(b"")
            with (
                mock.patch.object(
                    runner,
                    "_dataset_binding",
                    side_effect=lambda _config, current: DatasetBinding(
                        base_path=(
                            dataset_root / f"{current['dataset']}.pt"
                        ).resolve(),
                        category="ml",
                        dataset=current["dataset"],
                    ),
                ),
                mock.patch(
                    "benchmark_platform.runner._run_attempt",
                    side_effect=fake_run_attempt,
                ),
            ):
                result, exit_code = run_questions(
                    tasks,
                    RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        task_parallelism=2,
                        intra_task_parallelism=2,
                        attempts_per_question=2,
                        agent_command=("unused-agent",),
                        virtual_graph_ml=False,
                    ),
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["summary"]["attempts"], 4)
        self.assertEqual(
            {(question_id, attempt) for question_id, attempt, _ in calls},
            {
                ("ml/cora/task-a", 1),
                ("ml/cora/task-a", 2),
                ("ml/hiv/task-b", 1),
                ("ml/hiv/task-b", 2),
            },
        )
        self.assertGreaterEqual(len({thread_id for _, _, thread_id in calls}), 4)


if __name__ == "__main__":
    unittest.main()

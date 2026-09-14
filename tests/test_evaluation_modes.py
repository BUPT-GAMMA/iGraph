from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import benchmark_platform.runner as runner
from benchmark_platform.judge_protocol import JudgeExecution
from benchmark_platform.scoring import score_attempt


def _task(identifier: str, dataset: str, ground_truth: dict) -> dict:
    return {
        "composite_id": f"theory/{dataset}/{identifier}",
        "id": identifier,
        "question": f"Solve {identifier}",
        "category": "theory",
        "dataset": dataset,
        "task_type": "theory",
        "required_tools": ["get_density", "get_node_degree"],
        "ground_truth": ground_truth,
        "context": {},
    }


def _response(answer: dict) -> dict:
    return {
        "status": "success",
        "final_answer": answer,
        "reasoning": ["inspect graph", "compute answer"],
        "transcript": [{"role": "assistant", "content": "done"}],
        "tool_calls": [
            {"name": "get_density", "success": False},
            {"name": "get_node_degree", "success": True},
        ],
    }


def _prepare_replay(root: Path, tasks: list[dict], *, attempts: int = 1) -> tuple[Path, Path]:
    dataset_root = root / "dataset"
    dataset_root.mkdir()
    for dataset in {str(task["dataset"]) for task in tasks}:
        (dataset_root / f"{dataset}.pt").write_bytes(b"fixture")
    replay_records = [
        {
            "question_id": task["composite_id"],
            "attempt": attempt,
            "response": _response(task["ground_truth"]),
        }
        for task in tasks
        for attempt in range(1, attempts + 1)
    ]
    replay_path = root / "replay.json"
    replay_path.write_text(json.dumps(replay_records), encoding="utf-8")
    return dataset_root, replay_path


def _grouped_result(
    *,
    identifier: str,
    category: str,
    dataset: str,
    task_type: str,
    tsa_numerator: int,
    tsa_denominator: int,
    judge_score: int,
) -> dict:
    return {
        "question": {
            "composite_id": f"{category}/{dataset}/{identifier}",
            "id": identifier,
            "category": category,
            "dataset": dataset,
            "task_type": task_type,
        },
        "execution_success": True,
        "evaluation_enabled": True,
        "ground_truth_pass": True,
        "tool_path_pass": tsa_numerator == tsa_denominator,
        "judge_pass": judge_score == 1,
        "attempts": [
            {
                "agent": {"status": "success"},
                "scores": {
                    "ground_truth": {
                        "available": True,
                        "accuracy": 1.0,
                        "macro_accuracy": 1.0,
                    },
                    "tool_path": {
                        "available": True,
                        "successful": {
                            "accuracy": (
                                tsa_numerator / tsa_denominator
                                if tsa_denominator
                                else 0.0
                            )
                        },
                        "strict": {
                            "tsa_numerator": tsa_numerator,
                            "tsa_denominator": tsa_denominator,
                        },
                    },
                },
                "judge": {"status": "success", "score": judge_score},
            }
        ],
    }


class EvaluationModeTests(unittest.TestCase):
    def test_graph_family_taxonomy_matches_declared_benchmark_datasets(self):
        expected = {
            "NAG": (
                "DGraph-Fin",
                "dgraphfin",
                "dgraph_fin",
                "pokec",
                "NBA",
                "nba",
                "credit",
                "bail",
            ),
            "TAG": (
                "Reddit",
                "reddit",
                "Instagram",
                "history",
                "product",
                "arxiv",
                "cora",
            ),
            "TPG": ("HIV", "hiv", "PCBA", "pcba"),
        }
        for family, datasets in expected.items():
            for dataset in datasets:
                with self.subTest(family=family, dataset=dataset):
                    self.assertEqual(runner._graph_family(dataset), family)
        self.assertEqual(runner._graph_family("pubmed"), "UNKNOWN")
        self.assertEqual(runner._graph_family("fitness"), "UNKNOWN")

    def test_conflicting_taxonomy_hints_are_not_silently_classified(self):
        self.assertEqual(
            runner._structural_task_subtype(
                {"task_type": "Node", "context": {"level": "Edge"}}
            ),
            "UNKNOWN",
        )
        self.assertEqual(
            runner._structural_task_subtype(
                {"task_type": "Node", "context": {"level": ["Edge"]}}
            ),
            "Node",
        )
        self.assertEqual(
            runner._task_subtype(
                {
                    "task_type": "NC",
                    "required_tools": [
                        "structural_node_embedding_link_prediction_train"
                    ],
                },
                "ml",
            ),
            "UNKNOWN",
        )

    def test_reference_derived_subtype_is_deferred_until_agents_exit(self):
        task = {
            "composite_id": "ml/cora/deferred",
            "id": "deferred",
            "question": "Predict the missing link",
            "category": "ml",
            "dataset": "cora",
            "task_type": "prediction",
            "required_tools": [
                "structural_node_embedding_link_prediction_train"
            ],
            "ground_truth": True,
            "context": {},
        }
        question_result = {
            "question": runner._audit_task(task),
            "attempts": [],
        }
        self.assertEqual(question_result["question"]["task_subtype"], "UNKNOWN")

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            runner._attach_final_provenance(
                [task],
                [question_result],
                [runner.DatasetBinding(base_path=root / "cora.pt")],
                root,
            )

        self.assertEqual(question_result["question"]["task_subtype"], "LP")
        self.assertTrue(
            question_result["question"]["benchmark_dimensions_finalized"]
        )

    def test_cached_public_dimensions_cannot_override_source_fields(self):
        result = {
            "question": {
                "category": "theory",
                "dataset": "cora",
                "task_type": "node",
                "problem_group": "ml",
                "graph_family": "NAG",
                "task_subtype": "LP",
            }
        }
        self.assertEqual(
            runner._result_dimensions(result),
            ("theory", "TAG", "Node"),
        )

    def test_balanced_sampling_key_uses_the_formal_matrix_taxonomy(self):
        self.assertEqual(
            runner.benchmark_type_key(
                {
                    "category": "theory",
                    "dataset": "cora",
                    "task_type": "node",
                    "context": {},
                }
            ),
            "theory/TAG/Node",
        )
        with self.assertRaisesRegex(ValueError, "formal GABench taxonomy"):
            runner.benchmark_type_key(
                {
                    "category": "retrieval",
                    "dataset": "pubmed",
                    "task_type": "node",
                    "context": {},
                }
            )
        with self.assertRaisesRegex(ValueError, "formal GABench taxonomy"):
            runner.benchmark_type_key(
                {
                    "category": "ml",
                    "dataset": "hiv",
                    "task_type": "LP",
                    "required_tools": [],
                    "ground_truth": True,
                    "context": {},
                }
            )

    def test_benchmark_matrix_groups_tsa_and_sr_without_averaging_percentages(self):
        results = [
            _grouped_result(
                identifier="t1",
                category="theory",
                dataset="cora",
                task_type="Node",
                tsa_numerator=1,
                tsa_denominator=2,
                judge_score=1,
            ),
            _grouped_result(
                identifier="t2",
                category="theory",
                dataset="CORA",
                task_type="node_level",
                tsa_numerator=2,
                tsa_denominator=3,
                judge_score=0,
            ),
            _grouped_result(
                identifier="m1",
                category="ml",
                dataset="DGraph-Fin",
                task_type="link_prediction",
                tsa_numerator=1,
                tsa_denominator=1,
                judge_score=1,
            ),
            _grouped_result(
                identifier="m2",
                category="ml",
                dataset="hiv",
                task_type="GC",
                tsa_numerator=2,
                tsa_denominator=2,
                judge_score=1,
            ),
            _grouped_result(
                identifier="m3",
                category="ml",
                dataset="hiv",
                task_type="LP",
                tsa_numerator=1,
                tsa_denominator=2,
                judge_score=0,
            ),
            _grouped_result(
                identifier="o1",
                category="insight",
                dataset="reddit",
                task_type="open_ended",
                tsa_numerator=1,
                tsa_denominator=2,
                judge_score=0,
            ),
            _grouped_result(
                identifier="o2",
                category="prediction",
                dataset="pcba",
                task_type="prediction",
                tsa_numerator=1,
                tsa_denominator=1,
                judge_score=1,
            ),
            _grouped_result(
                identifier="u1",
                category="retrieval",
                dataset="pubmed",
                task_type="Graph",
                tsa_numerator=1,
                tsa_denominator=2,
                judge_score=1,
            ),
        ]

        summary = runner._aggregate(results)
        matrix = summary["benchmark_matrix"]
        tag_node = matrix["cells"]["theory"]["TAG"]["Node"]
        self.assertEqual(tag_node["questions"], 2)
        self.assertEqual(tag_node["tool_selection_lcs_sum"], 3)
        self.assertEqual(tag_node["tool_selection_expected_length_sum"], 5)
        self.assertAlmostEqual(tag_node["tool_selection_accuracy"], 3 / 5)
        self.assertEqual(tag_node["success_rate"], 0.5)

        self.assertEqual(
            matrix["cells"]["ml"]["NAG"]["LP"]["success_rate"], 1.0
        )
        self.assertEqual(
            matrix["cells"]["ml"]["TPG"]["GC"]["questions"], 1
        )
        self.assertEqual(
            matrix["cells"]["open_qa"]["TAG"]["Insight"]["questions"], 1
        )
        self.assertEqual(
            matrix["cells"]["open_qa"]["TPG"]["Prediction"]["questions"], 1
        )

        empty = matrix["cells"]["ml"]["NAG"]["NC"]
        self.assertEqual(empty["questions"], 0)
        self.assertIsNone(empty["tool_selection_accuracy"])
        self.assertIsNone(empty["success_rate"])
        self.assertFalse(matrix["classification_complete"])
        self.assertEqual(matrix["unmapped_datasets"], ["pubmed"])
        self.assertEqual(matrix["out_of_taxonomy_question_count"], 2)
        self.assertEqual(
            matrix["cells"]["retrieval"]["UNKNOWN"]["Graph"]["questions"], 1
        )
        self.assertEqual(matrix["cells"]["ml"]["TPG"]["LP"]["questions"], 1)
        self.assertIn(
            {
                "problem_group": "ml",
                "graph_family": "TPG",
                "task_subtype": "LP",
                "questions": 1,
            },
            matrix["unexpected_cells"],
        )

        leaf_metrics = [
            metrics
            for families in matrix["cells"].values()
            for subtypes in families.values()
            for metrics in subtypes.values()
        ]
        for field in (
            "questions",
            "attempts",
            "tool_selection_lcs_sum",
            "tool_selection_expected_length_sum",
            "judge_success_count",
            "judge_target_count",
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    sum(metrics[field] for metrics in leaf_metrics),
                    summary[field],
                )

    def test_capture_only_replay_never_scores_or_judges_and_saves_complete_records(self):
        tasks = [
            _task("q1", "alpha", {"value": 1}),
            _task("q2", "beta", {"value": 2}),
        ]
        attempts = 2

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root, replay_path = _prepare_replay(
                root, tasks, attempts=attempts
            )
            completed: set[tuple[str, int]] = set()
            completed_lock = threading.Lock()
            original_run_attempt = runner._run_attempt
            original_persist = runner._persist_capture_results

            def tracked_run_attempt(task, task_index, attempt, config, run_dir, replay_store):
                result = original_run_attempt(
                    task, task_index, attempt, config, run_dir, replay_store
                )
                with completed_lock:
                    completed.add((task["composite_id"], attempt))
                return result

            def checked_persist(tasks_arg, question_results, run_dir):
                expected = {
                    (task["composite_id"], attempt)
                    for task in tasks
                    for attempt in range(1, attempts + 1)
                }
                with completed_lock:
                    self.assertEqual(completed, expected)
                return original_persist(tasks_arg, question_results, run_dir)

            with (
                mock.patch.object(
                    runner, "_run_attempt", side_effect=tracked_run_attempt
                ),
                mock.patch.object(
                    runner, "_persist_capture_results", side_effect=checked_persist
                ) as persist_mock,
                mock.patch.object(
                    runner,
                    "score_attempt",
                    side_effect=AssertionError("capture-only mode must not score"),
                ) as score_mock,
                mock.patch.object(
                    runner,
                    "execute_judge_command",
                    side_effect=AssertionError("capture-only mode must not judge"),
                ) as judge_mock,
            ):
                result, exit_code = runner.run_questions(
                    tasks,
                    runner.RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        task_parallelism=2,
                        intra_task_parallelism=2,
                        attempts_per_question=attempts,
                        replay_file=replay_path,
                        virtual_graph_ml=False,
                        evaluate=False,
                    ),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "completed")
            self.assertFalse(result["summary"]["evaluation_enabled"])
            self.assertTrue(result["evaluation"]["raw_ground_truth_saved"])
            capture_cell = result["summary"]["benchmark_matrix"]["cells"][
                "theory"
            ]["UNKNOWN"]["UNKNOWN"]
            self.assertFalse(capture_cell["evaluation_enabled"])
            self.assertIsNone(capture_cell["tool_selection_accuracy"])
            self.assertIsNone(capture_cell["success_rate"])
            score_mock.assert_not_called()
            judge_mock.assert_not_called()
            persist_mock.assert_called_once()

            for task, question_result in zip(tasks, result["questions"], strict=True):
                capture = json.loads(
                    Path(question_result["capture_file"]).read_text(encoding="utf-8")
                )
                self.assertEqual(capture["ground_truth"], task["ground_truth"])
                self.assertEqual(len(capture["attempts"]), attempts)
                for summary, captured in zip(
                    question_result["attempts"], capture["attempts"], strict=True
                ):
                    self.assertIn("trajectory", summary)
                    self.assertNotIn("scores", summary)
                    detail = json.loads(
                        Path(summary["attempt_result_file"]).read_text(encoding="utf-8")
                    )
                    self.assertIn("trajectory", detail)
                    self.assertNotIn("scores", detail)
                    self.assertEqual(
                        captured["agent"]["reasoning"],
                        ["inspect graph", "compute answer"],
                    )
                    self.assertEqual(
                        captured["agent"]["transcript"],
                        [{"role": "assistant", "content": "done"}],
                    )
                    self.assertEqual(
                        captured["trajectory"]["strict_path"],
                        ["get_density", "get_node_degree"],
                    )
                    self.assertNotIn("scores", captured)

    def test_evaluate_replay_persists_raw_capture_after_all_judges_finish(self):
        tasks = [
            _task("q1", "alpha", {"value": 1}),
            _task("q2", "beta", {"value": 2}),
        ]
        attempts = 2

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root, replay_path = _prepare_replay(
                root, tasks, attempts=attempts
            )
            judged: set[tuple[str, int]] = set()
            judged_lock = threading.Lock()
            original_persist = runner._persist_capture_results

            def decision(_command, envelope, **_kwargs):
                question_id = envelope["question_id"]
                with judged_lock:
                    attempt = 1 + sum(
                        existing_id == question_id
                        for existing_id, _ in judged
                    )
                    judged.add((question_id, attempt))
                return JudgeExecution(status="success", score=1)

            def checked_persist(tasks_arg, question_results, run_dir):
                expected = {
                    (task["composite_id"], attempt)
                    for task in tasks
                    for attempt in range(1, attempts + 1)
                }
                with judged_lock:
                    self.assertEqual(judged, expected)
                return original_persist(tasks_arg, question_results, run_dir)

            with (
                mock.patch.object(
                    runner, "execute_judge_command", side_effect=decision
                ) as judge_mock,
                mock.patch.object(
                    runner, "_persist_capture_results", side_effect=checked_persist
                ) as persist_mock,
            ):
                result, exit_code = runner.run_questions(
                    tasks,
                    runner.RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        task_parallelism=2,
                        intra_task_parallelism=2,
                        attempts_per_question=attempts,
                        replay_file=replay_path,
                        virtual_graph_ml=False,
                        evaluate=True,
                        judge_command=("fake-judge",),
                    ),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["evaluation"]["raw_ground_truth_saved"])
            self.assertEqual(judge_mock.call_count, len(tasks) * attempts)
            persist_mock.assert_called_once()

            for task, question_result in zip(tasks, result["questions"], strict=True):
                self.assertTrue(question_result["ground_truth_saved"])
                capture_path = Path(question_result["capture_file"])
                self.assertTrue(capture_path.is_file())
                capture = json.loads(capture_path.read_text(encoding="utf-8"))
                self.assertEqual(capture["ground_truth"], task["ground_truth"])
                self.assertEqual(len(capture["attempts"]), attempts)
                for captured in capture["attempts"]:
                    self.assertEqual(
                        captured["agent"]["reasoning"],
                        ["inspect graph", "compute answer"],
                    )
                    self.assertEqual(
                        captured["trajectory"]["strict_path"],
                        ["get_density", "get_node_degree"],
                    )
                    self.assertNotIn("scores", captured)
                    self.assertNotIn("evaluation", captured)

    def test_tsa_is_ratio_of_lcs_sums_and_strict_path_counts_failed_calls(self):
        first_task = _task("q1", "alpha", {"value": 1})
        first_score = score_attempt(
            first_task,
            first_task["ground_truth"],
            [
                {"name": "get_density", "success": False},
                {"name": "get_node_degree", "success": True},
            ],
        )
        second_task = {
            **_task("q2", "beta", {"value": 2}),
            "required_tools": [
                "get_density",
                "get_node_degree",
                "get_clustering_coefficient",
            ],
        }
        second_score = score_attempt(
            second_task,
            second_task["ground_truth"],
            [{"name": "get_node_degree", "success": True}],
        )

        self.assertEqual(
            first_score["tool_path"]["strict_actual"],
            ["get_density", "get_node_degree"],
        )
        self.assertEqual(
            first_score["tool_path"]["successful_actual"], ["get_node_degree"]
        )
        self.assertEqual(first_score["tool_path"]["strict"]["tsa_numerator"], 2)

        question_results = [
            {
                "question": {"category": "theory", "dataset": "alpha"},
                "execution_success": True,
                "ground_truth_pass": True,
                "tool_path_pass": True,
                "attempts": [
                    {"agent": {"status": "success"}, "scores": first_score}
                ],
            },
            {
                "question": {"category": "theory", "dataset": "beta"},
                "execution_success": True,
                "ground_truth_pass": True,
                "tool_path_pass": False,
                "attempts": [
                    {"agent": {"status": "success"}, "scores": second_score}
                ],
            },
        ]
        summary = runner._aggregate_core(question_results)

        self.assertEqual(summary["tool_selection_lcs_sum"], 3)
        self.assertEqual(summary["tool_selection_expected_length_sum"], 5)
        self.assertAlmostEqual(summary["tool_selection_accuracy"], 3 / 5)

    def test_binary_judge_scores_produce_success_rate(self):
        tasks = [
            _task("q1", "alpha", {"value": 1}),
            _task("q2", "beta", {"value": 2}),
        ]
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root, replay_path = _prepare_replay(root, tasks)
            def decision(_command, envelope, **_kwargs):
                score = 1 if envelope["question_id"].endswith("/q1") else 0
                return JudgeExecution(status="success", score=score)

            with mock.patch.object(
                runner, "execute_judge_command", side_effect=decision
            ) as judge_mock:
                result, exit_code = runner.run_questions(
                    tasks,
                    runner.RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        task_parallelism=2,
                        replay_file=replay_path,
                        virtual_graph_ml=False,
                        evaluate=True,
                        judge_command=("fake-judge",),
                    ),
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(judge_mock.call_count, 2)
        self.assertEqual(result["summary"]["judge_success_count"], 1)
        self.assertEqual(result["summary"]["judge_target_count"], 2)
        self.assertEqual(result["summary"]["judge_coverage"], 1.0)
        self.assertEqual(result["summary"]["success_rate"], 0.5)
        envelopes = [call.args[1] for call in judge_mock.call_args_list]
        self.assertEqual(
            {envelope["original_question"] for envelope in envelopes},
            {task["question"] for task in tasks},
        )
        self.assertEqual(
            {json.dumps(envelope["final_answer"], sort_keys=True) for envelope in envelopes},
            {json.dumps(task["ground_truth"], sort_keys=True) for task in tasks},
        )
        self.assertTrue(
            all("agent_final_response" not in envelope for envelope in envelopes)
        )
        self.assertEqual(
            [question["attempts"][0]["judge"]["score"] for question in result["questions"]],
            [1, 0],
        )

    def test_incomplete_judge_coverage_nulls_success_rate_and_exits_five(self):
        tasks = [
            _task("q1", "alpha", {"value": 1}),
            _task("q2", "beta", {"value": 2}),
        ]
        tasks[0].update(
            composite_id="theory/cora/q1",
            dataset="cora",
            task_type="node",
        )
        tasks[1].update(
            composite_id="theory/bail/q2",
            dataset="bail",
            task_type="edge",
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root, replay_path = _prepare_replay(root, tasks)
            def decision(_command, envelope, **_kwargs):
                if envelope["question_id"].endswith("/q1"):
                    return JudgeExecution(status="success", score=1)
                return JudgeExecution(
                    status="invalid_output",
                    error={"type": "ValueError", "message": "missing binary score"},
                )

            with mock.patch.object(
                runner, "execute_judge_command", side_effect=decision
            ):
                result, exit_code = runner.run_questions(
                    tasks,
                    runner.RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        task_parallelism=2,
                        replay_file=replay_path,
                        virtual_graph_ml=False,
                        evaluate=True,
                        judge_command=("fake-judge",),
                    ),
                )

        self.assertEqual(exit_code, 5)
        self.assertEqual(result["status"], "completed_with_evaluation_errors")
        self.assertIsNone(result["summary"]["success_rate"])
        self.assertEqual(result["summary"]["provisional_success_rate"], 1.0)
        self.assertEqual(result["summary"]["judge_target_count"], 2)
        self.assertEqual(result["summary"]["judge_completed_count"], 1)
        self.assertEqual(result["summary"]["judge_failed_count"], 1)
        self.assertEqual(result["summary"]["judge_coverage"], 0.5)
        matrix = result["summary"]["benchmark_matrix"]
        successful_cell = matrix["cells"]["theory"]["TAG"]["Node"]
        failed_cell = matrix["cells"]["theory"]["NAG"]["Edge"]
        self.assertEqual(successful_cell["success_rate"], 1.0)
        self.assertEqual(successful_cell["judge_coverage"], 1.0)
        self.assertIsNone(failed_cell["success_rate"])
        self.assertIsNone(failed_cell["provisional_success_rate"])
        self.assertEqual(failed_cell["judge_coverage"], 0.0)

    def test_data_preparation_failure_is_not_scored_or_judged(self):
        task = _task("q-data", "alpha", {"value": 1})
        failed_attempt = {
            "question_id": task["composite_id"],
            "attempt": 1,
            "agent": {
                "status": "failed",
                "error": {"type": "MaskedDatasetError", "message": "bad PT"},
            },
            "failure_origin": "data_preparation",
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
            (dataset_root / "alpha.pt").write_bytes(b"fixture")
            with (
                mock.patch.object(
                    runner, "_run_attempt", return_value=failed_attempt
                ),
                mock.patch.object(
                    runner,
                    "score_attempt",
                    side_effect=AssertionError("data errors are not model errors"),
                ) as score_mock,
                mock.patch.object(
                    runner,
                    "execute_judge_command",
                    side_effect=AssertionError("data errors must not spend judge tokens"),
                ) as judge_mock,
            ):
                result, exit_code = runner.run_questions(
                    [task],
                    runner.RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        evaluate=True,
                        agent_command=("fake-agent",),
                        judge_command=("fake-judge",),
                        virtual_graph_ml=False,
                    ),
                )

        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "completed_with_data_errors")
        self.assertEqual(result["summary"]["data_error_attempt_count"], 1)
        self.assertFalse(result["summary"]["evaluation_complete"])
        self.assertIsNone(result["summary"]["tool_selection_accuracy"])
        self.assertIsNone(result["summary"]["success_rate"])
        data_error_cell = result["summary"]["benchmark_matrix"]["cells"][
            "theory"
        ]["UNKNOWN"]["UNKNOWN"]
        self.assertIsNone(data_error_cell["tool_selection_accuracy"])
        self.assertIsNone(data_error_cell["success_rate"])
        score_mock.assert_not_called()
        judge_mock.assert_not_called()

    def test_data_cleanup_failure_is_not_scored_or_judged(self):
        task = _task("q-cleanup", "alpha", {"value": 1})
        failed_attempt = {
            "question_id": task["composite_id"],
            "attempt": 1,
            "agent": {"status": "success", "error": None},
            "failure_origin": "data_cleanup",
            "trajectory": {
                "strict_path": ["get_density"],
                "successful_path": ["get_density"],
                "call_count": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            (dataset_root / "alpha.pt").write_bytes(b"fixture")
            with (
                mock.patch.object(
                    runner, "_run_attempt", return_value=failed_attempt
                ),
                mock.patch.object(
                    runner,
                    "score_attempt",
                    side_effect=AssertionError("cleanup errors are not model errors"),
                ) as score_mock,
                mock.patch.object(
                    runner,
                    "execute_judge_command",
                    side_effect=AssertionError("cleanup errors must not spend judge tokens"),
                ) as judge_mock,
            ):
                result, exit_code = runner.run_questions(
                    [task],
                    runner.RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        evaluate=True,
                        agent_command=("fake-agent",),
                        judge_command=("fake-judge",),
                        virtual_graph_ml=False,
                    ),
                )

        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "completed_with_data_errors")
        self.assertFalse(result["questions"][0]["attempts"][0]["evaluation_eligible"])
        score_mock.assert_not_called()
        judge_mock.assert_not_called()

    def test_programmatic_evaluation_requires_a_judge(self):
        task = _task("q-no-judge", "alpha", {"value": 1})
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset_root, replay_path = _prepare_replay(root, [task])
            with self.assertRaisesRegex(ValueError, "requires judge-command"):
                runner.run_questions(
                    [task],
                    runner.RunnerConfig(
                        output_dir=root / "output",
                        dataset_root=dataset_root,
                        replay_file=replay_path,
                        evaluate=True,
                        virtual_graph_ml=False,
                    ),
                )


if __name__ == "__main__":
    unittest.main()

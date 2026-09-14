from __future__ import annotations

from collections import Counter
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import benchmark_questions


def _task(task_id: str) -> dict:
    return {
        "id": task_id,
        "question": f"Question {task_id}?",
        "task_type": "GC",
        "required_tools": [],
        "ground_truth": 0,
        "context": {"graph_idx": 1},
    }


class QuestionCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.questions_root = self.root / "questions"
        self.dataset_root = self.root / "dataset"
        self.output_dir = self.root / "output"
        self.dataset_root.mkdir()
        (self.dataset_root / "hiv.pt").write_bytes(b"fixture")
        question_path = self.questions_root / "ml" / "hiv" / "questions.json"
        question_path.parent.mkdir(parents=True)
        (question_path.parent / "pure_ml_bench_masked_data.pt").write_bytes(
            b"fixture"
        )
        question_path.write_text(
            json.dumps(
                {
                    "schema": "igraph.benchmark.questions",
                    "schema_version": "1.0",
                    "category": "ml",
                    "dataset": "hiv",
                    "metadata": {},
                    "tasks": [_task("task_0001"), _task("task_0002")],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, dict, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = benchmark_questions.main(list(arguments))
        raw = output.getvalue()
        return exit_code, json.loads(raw), raw

    def base_arguments(self) -> list[str]:
        return [
            "questions",
            "--questions-root",
            str(self.questions_root),
            "--dataset-root",
            str(self.dataset_root),
            "--output-dir",
            str(self.output_dir),
        ]

    def write_cora_taxonomy(self, *, copies_per_type: int = 2) -> None:
        (self.dataset_root / "cora.pt").write_bytes(b"fixture")
        category_types = {
            "theory": ("node", "edge", "graph"),
            "retrieval": ("node", "edge", "graph"),
            "ml": ("NC", "LP"),
            "insight": ("insight",),
            "prediction": ("prediction",),
        }
        for category, task_types in category_types.items():
            directory = self.questions_root / category / "cora"
            directory.mkdir(parents=True, exist_ok=True)
            if category == "ml":
                (directory / "pure_ml_bench_masked_data.pt").write_bytes(b"fixture")
            if category == "prediction":
                (directory / "intermediate_ground_truth_masked_data.pt").write_bytes(
                    b"fixture"
                )
            tasks = []
            for task_type in task_types:
                for copy_index in range(copies_per_type):
                    tasks.append(
                        {
                            "id": f"{task_type.lower()}_{copy_index}",
                            "question": f"Cora {category} {task_type}?",
                            "task_type": task_type,
                            "required_tools": [],
                            "ground_truth": 0,
                            "context": {"task_index": len(tasks)},
                        }
                    )
            (directory / "questions.json").write_text(
                json.dumps(
                    {
                        "schema": "igraph.benchmark.questions",
                        "schema_version": "1.0",
                        "category": category,
                        "dataset": "cora",
                        "metadata": {},
                        "tasks": tasks,
                    }
                ),
                encoding="utf-8",
            )

    def test_validate_only_defaults_to_one_question_and_writes_details(self) -> None:
        exit_code, receipt, raw = self.invoke(
            *self.base_arguments(), "--validate-only"
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(raw.count("\n"), 1)
        self.assertEqual(receipt["status"], "validated")
        self.assertEqual(receipt["question_count"], 1)
        self.assertEqual(receipt["available_question_count"], 2)
        result_path = Path(receipt["result_file"])
        self.assertTrue(result_path.is_file())
        details = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(details["selection"]["question_count"], 1)

    def test_zero_question_count_selects_all(self) -> None:
        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(),
            "--question-count",
            "0",
            "--validate-only",
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["question_count"], 2)

    def test_questions_per_type_selects_ten_balanced_cora_cells(self) -> None:
        self.write_cora_taxonomy()
        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(),
            "--datasets",
            "cora",
            "--categories",
            "theory",
            "retrieval",
            "ml",
            "insight",
            "prediction",
            "--questions-per-type",
            "1",
            "--shuffle",
            "--seed",
            "7",
            "--validate-only",
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["question_count"], 10)
        self.assertEqual(receipt["sampling"]["type_count"], 10)
        self.assertEqual(
            set(receipt["sampling"]["selected_type_counts"].values()), {1}
        )
        result = json.loads(
            Path(receipt["result_file"]).read_text(encoding="utf-8")
        )
        self.assertEqual(result["selection"]["sampling"], receipt["sampling"])
        questions = [item["question"] for item in result["questions"]]
        self.assertEqual(
            Counter(question["category"] for question in questions),
            {
                "theory": 3,
                "retrieval": 3,
                "ml": 2,
                "insight": 1,
                "prediction": 1,
            },
        )
        self.assertEqual(
            {
                (
                    question["problem_group"],
                    question["graph_family"],
                    question["task_subtype"],
                )
                for question in questions
            },
            {
                ("theory", "TAG", "Node"),
                ("theory", "TAG", "Edge"),
                ("theory", "TAG", "Graph"),
                ("retrieval", "TAG", "Node"),
                ("retrieval", "TAG", "Edge"),
                ("retrieval", "TAG", "Graph"),
                ("ml", "TAG", "NC"),
                ("ml", "TAG", "LP"),
                ("open_qa", "TAG", "Insight"),
                ("open_qa", "TAG", "Prediction"),
            },
        )

    def test_questions_per_type_rejects_conflicting_caps(self) -> None:
        self.write_cora_taxonomy()
        for cap_arguments in (
            ("--question-count", "5"),
            ("--questions-per-file", "1"),
        ):
            with self.subTest(cap=cap_arguments[0]):
                exit_code, receipt, _ = self.invoke(
                    *self.base_arguments(),
                    "--datasets",
                    "cora",
                    "--questions-per-type",
                    "1",
                    *cap_arguments,
                    "--validate-only",
                )
                self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
                self.assertEqual(receipt["status"], "usage_error")

        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(),
            "--datasets",
            "cora",
            "--questions-per-type",
            "0",
            "--validate-only",
        )
        self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
        self.assertEqual(receipt["status"], "usage_error")

    def test_questions_per_type_fails_before_runner_when_a_cell_is_short(self) -> None:
        self.write_cora_taxonomy(copies_per_type=2)
        with patch.object(benchmark_questions, "run_questions") as mocked:
            exit_code, receipt, _ = self.invoke(
                *self.base_arguments(),
                "--datasets",
                "cora",
                "--questions-per-type",
                "3",
                "--validate-only",
            )

        self.assertEqual(exit_code, benchmark_questions.EXIT_INPUT)
        self.assertEqual(receipt["status"], "input_error")
        self.assertIn("available=2, required=3", receipt["error"]["message"])
        mocked.assert_not_called()

    def test_questions_per_type_rejects_a_missing_selected_source_file(self) -> None:
        self.write_cora_taxonomy()
        (self.questions_root / "prediction" / "cora" / "questions.json").unlink()

        with patch.object(benchmark_questions, "run_questions") as mocked:
            exit_code, receipt, _ = self.invoke(
                *self.base_arguments(),
                "--datasets",
                "cora",
                "--questions-per-type",
                "1",
                "--validate-only",
            )

        self.assertEqual(exit_code, benchmark_questions.EXIT_INPUT)
        self.assertEqual(receipt["status"], "input_error")
        self.assertIn("prediction/cora", receipt["error"]["message"])
        mocked.assert_not_called()

    def test_dispatcher_may_consume_questions_subcommand(self) -> None:
        arguments = self.base_arguments()[1:] + ["--validate-only"]

        exit_code, receipt, _ = self.invoke(*arguments)

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["command"], "questions")

    def test_parallel_and_execution_options_reach_runner_config(self) -> None:
        replay = self.root / "replay.json"
        replay.write_text("{}", encoding="utf-8")
        fake_result = {
            "status": "completed",
            "run_id": "questions-test",
            "result_file": str(self.root / "result.json"),
            "summary": {"questions": 1},
        }
        arguments = self.base_arguments() + [
            "--categories",
            "ml",
            "--datasets",
            "hiv",
            "--task-parallelism",
            "3",
            "--intra-task-parallelism",
            "2",
            "--attempts-per-question",
            "4",
            "--shuffle",
            "--seed",
            "7",
            "--timeout",
            "12.5",
            "--tolerance",
            "0.01",
            "--no-virtual-graph-ml",
            "--replay-file",
            str(replay),
            "--recover-cleanup-failures",
        ]

        with patch.object(
            benchmark_questions, "run_questions", return_value=(fake_result, 0)
        ) as mocked:
            exit_code, receipt, _ = self.invoke(*arguments)

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["run_id"], "questions-test")
        tasks, config = mocked.call_args.args
        self.assertEqual(len(tasks), 1)
        self.assertEqual(config.task_parallelism, 3)
        self.assertEqual(config.intra_task_parallelism, 2)
        self.assertEqual(config.attempts_per_question, 4)
        self.assertEqual(config.timeout_seconds, 12.5)
        self.assertEqual(config.numeric_tolerance, 0.01)
        self.assertFalse(config.virtual_graph_ml)
        self.assertEqual(config.replay_file, replay)
        self.assertTrue(config.recover_cleanup_failures)
        self.assertFalse(config.evaluate)
        self.assertEqual(config.questions_root, self.questions_root)

    def test_agent_command_is_a_json_argv_array(self) -> None:
        fake_result = {
            "status": "completed",
            "run_id": "questions-command",
            "result_file": str(self.root / "result.json"),
            "summary": {},
        }
        arguments = self.base_arguments() + [
            "--agent-command-json",
            '["python","agent.py","--mode=test"]',
        ]

        with patch.object(
            benchmark_questions, "run_questions", return_value=(fake_result, 0)
        ) as mocked:
            exit_code, _, _ = self.invoke(*arguments)

        self.assertEqual(exit_code, 0)
        _, config = mocked.call_args.args
        self.assertEqual(
            config.agent_command, ("python", "agent.py", "--mode=test")
        )
        self.assertFalse(config.recover_cleanup_failures)
        self.assertFalse(config.reuse_successful_judge_results)

    def test_execution_modes_are_mutually_exclusive(self) -> None:
        exit_code, receipt, raw = self.invoke(
            *self.base_arguments(),
            "--validate-only",
            "--replay-file",
            str(self.root / "replay.json"),
        )

        self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
        self.assertEqual(receipt["status"], "usage_error")
        self.assertEqual(raw.count("\n"), 1)

    def test_cleanup_recovery_requires_replay_mode(self) -> None:
        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(),
            "--agent-command-json",
            '["python","agent.py"]',
            "--recover-cleanup-failures",
        )

        self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
        self.assertEqual(receipt["status"], "usage_error")
        self.assertIn("requires --replay-file", receipt["error"]["message"])

    def test_evaluate_requires_judge_and_reaches_runner_config(self) -> None:
        replay = self.root / "replay.json"
        replay.write_text("{}", encoding="utf-8")
        fake_result = {
            "status": "completed",
            "run_id": "questions-evaluated",
            "result_file": str(self.root / "result.json"),
            "summary": {},
        }
        arguments = self.base_arguments() + [
            "--replay-file",
            str(replay),
            "--evaluate",
            "--judge-command-json",
            '["python","judge.py"]',
            "--judge-timeout-seconds",
            "15",
            "--reuse-successful-judge-results",
        ]

        with patch.object(
            benchmark_questions, "run_questions", return_value=(fake_result, 0)
        ) as mocked:
            exit_code, _, _ = self.invoke(*arguments)

        self.assertEqual(exit_code, 0)
        _, config = mocked.call_args.args
        self.assertTrue(config.evaluate)
        self.assertEqual(config.judge_command, ("python", "judge.py"))
        self.assertEqual(config.judge_timeout_seconds, 15.0)
        self.assertTrue(config.reuse_successful_judge_results)

    def test_judge_reuse_requires_replay_evaluation(self) -> None:
        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(),
            "--agent-command-json",
            '["python","agent.py"]',
            "--evaluate",
            "--judge-command-json",
            '["python","judge.py"]',
            "--reuse-successful-judge-results",
        )

        self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
        self.assertEqual(receipt["status"], "usage_error")
        self.assertIn(
            "requires --replay-file and --evaluate",
            receipt["error"]["message"],
        )

    def test_evaluate_without_judge_is_usage_error(self) -> None:
        replay = self.root / "replay.json"
        replay.write_text("{}", encoding="utf-8")

        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(),
            "--replay-file",
            str(replay),
            "--evaluate",
        )

        self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
        self.assertEqual(receipt["status"], "usage_error")

    def test_validate_only_cannot_claim_to_evaluate(self) -> None:
        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(), "--validate-only", "--evaluate"
        )

        self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
        self.assertEqual(receipt["status"], "usage_error")

    def test_invalid_agent_json_is_usage_error(self) -> None:
        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(), "--agent-command-json", "not-json"
        )

        self.assertEqual(exit_code, benchmark_questions.EXIT_USAGE)
        self.assertEqual(receipt["error"]["type"], "CLIUsageError")

    def test_schema_failure_has_stable_input_exit_code(self) -> None:
        question_path = self.questions_root / "ml" / "hiv" / "questions.json"
        value = json.loads(question_path.read_text(encoding="utf-8"))
        value["schema_version"] = "2.0"
        question_path.write_text(json.dumps(value), encoding="utf-8")

        exit_code, receipt, _ = self.invoke(
            *self.base_arguments(), "--validate-only"
        )

        self.assertEqual(exit_code, benchmark_questions.EXIT_INPUT)
        self.assertEqual(receipt["status"], "input_error")
        self.assertIsNone(receipt["result_file"])


if __name__ == "__main__":
    unittest.main()

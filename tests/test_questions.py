from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest

from benchmark_platform.questions import (
    QuestionDiscoveryError,
    QuestionSchemaError,
    discover_question_files,
    load_question_file,
    load_question_files,
    load_questions,
)


def _task(task_id: str, *, ground_truth=1, required_tools=None) -> dict:
    suffix = task_id.rsplit("_", 1)[-1]
    return {
        "id": task_id,
        "question": f"Question {task_id}?",
        "task_type": "node",
        "required_tools": list(required_tools or []),
        "ground_truth": ground_truth,
        "context": {"task_index": int(suffix) if suffix.isdigit() else 0},
    }


def _standard(category: str, dataset: str, tasks: list[dict]) -> dict:
    return {
        "schema": "igraph.benchmark.questions",
        "schema_version": "1.0",
        "category": category,
        "dataset": dataset,
        "metadata": {"source_format": "tasks"},
        "tasks": tasks,
    }


class QuestionLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, category: str, dataset: str, payload) -> Path:
        path = self.root / category / dataset / "questions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_standard_schema_preserves_scalar_and_object_ground_truth(self) -> None:
        path = self.write(
            "prediction",
            "hiv",
            _standard(
                "prediction",
                "hiv",
                [
                    _task(
                        "task_0001",
                        ground_truth={"shortest_path": {"success": True, "path": [1, 2]}},
                        required_tools=["shortest_path"],
                    ),
                    _task("task_0002", ground_truth=0),
                ],
            ),
        )

        loaded = load_question_file(path)

        self.assertEqual(loaded.question_count, 2)
        self.assertEqual(loaded.questions[0].composite_id, "prediction/hiv/task_0001")
        self.assertEqual(loaded.questions[0].required_tools, ("shortest_path",))
        self.assertEqual(
            loaded.questions[0].ground_truth,
            {"shortest_path": {"success": True, "path": [1, 2]}},
        )
        self.assertEqual(loaded.questions[1].ground_truth, 0)
        self.assertEqual(loaded.questions[0].context, {"task_index": 1})

    def test_discovery_filters_categories_and_case_insensitive_datasets(self) -> None:
        expected = self.write("ml", "NBA", _standard("ml", "NBA", [_task("task_0001")]))
        self.write("theory", "cora", _standard("theory", "cora", [_task("task_0001")]))

        found = discover_question_files(
            self.root, categories="ml", datasets=["nba"]
        )

        self.assertEqual(found, [expected])
        with self.assertRaises(QuestionDiscoveryError):
            discover_question_files(self.root, categories="unknown")

    def test_partial_dataset_filter_typo_is_not_silently_ignored(self) -> None:
        self.write("ml", "cora", _standard("ml", "cora", [_task("task_0001")]))

        with self.assertRaisesRegex(QuestionDiscoveryError, "corra"):
            discover_question_files(self.root, datasets=["cora", "corra"])

    def test_legacy_questions_benchmark_list_and_single_dict(self) -> None:
        paths = [
            self.write("theory", "a", {"questions": [_task("task_0001")]}),
            self.write(
                "retrieval",
                "b",
                {"benchmark": {"tasks": [_task("task_0002")]}},
            ),
            self.write("ml", "c", [_task("task_0003")]),
            self.write(
                "insight",
                "d",
                {
                    "id": "task_0004",
                    "prompt": "Legacy prompt",
                    "answer": {"value": 4},
                },
            ),
        ]

        loaded = [load_question_file(path) for path in paths]

        self.assertEqual([item.question_count for item in loaded], [1, 1, 1, 1])
        self.assertEqual(loaded[-1].questions[0].question, "Legacy prompt")
        self.assertEqual(loaded[-1].questions[0].ground_truth, {"value": 4})
        self.assertIsNone(loaded[-1].questions[0].task_type)

    def test_standard_schema_version_and_scope_are_strict(self) -> None:
        wrong_version = _standard("ml", "hiv", [_task("task_0001")])
        wrong_version["schema_version"] = "2.0"
        version_path = self.write("ml", "hiv", wrong_version)
        with self.assertRaisesRegex(QuestionSchemaError, "schema_version"):
            load_question_file(version_path)

        mismatch_path = self.write(
            "theory", "cora", _standard("theory", "pubmed", [_task("task_0001")])
        )
        with self.assertRaisesRegex(QuestionSchemaError, "path dataset"):
            load_question_file(mismatch_path)

    def test_per_file_and_global_limits_report_both_counts(self) -> None:
        for category, dataset in (("theory", "a"), ("retrieval", "b")):
            self.write(
                category,
                dataset,
                _standard(category, dataset, [_task(f"task_{i:04d}") for i in range(1, 5)]),
            )

        result = load_questions(
            self.root, questions_per_file=3, question_count=4
        )

        self.assertEqual(result.question_count, 4)
        self.assertEqual(result.file_counts, {"theory/a": 4, "retrieval/b": 4})
        self.assertEqual(
            result.selected_file_counts, {"theory/a": 3, "retrieval/b": 1}
        )
        self.assertEqual(
            [question.composite_id for question in result.questions],
            [
                "theory/a/task_0001",
                "theory/a/task_0002",
                "theory/a/task_0003",
                "retrieval/b/task_0001",
            ],
        )

    def test_seeded_shuffle_is_reproducible(self) -> None:
        self.write(
            "ml",
            "hiv",
            _standard("ml", "hiv", [_task(f"task_{i:04d}") for i in range(1, 11)]),
        )

        first = load_questions(self.root, question_count=5, shuffle_seed=17)
        second = load_questions(self.root, question_count=5, shuffle_seed=17)

        self.assertEqual(
            [question.composite_id for question in first.questions],
            [question.composite_id for question in second.questions],
        )
        self.assertEqual(first.question_count, 5)

    def test_explicit_file_loader_has_runner_facing_api(self) -> None:
        path = self.write(
            "ml",
            "hiv",
            _standard("ml", "hiv", [_task(f"task_{i:04d}") for i in range(1, 7)]),
        )

        first = load_question_files(
            [path], question_count=3, questions_per_file=4, shuffle=True, seed=9
        )
        second = load_question_files(
            [path], question_count=3, questions_per_file=4, shuffle=True, seed=9
        )

        self.assertEqual(first.question_count, 3)
        self.assertEqual(first.file_counts, {"ml/hiv": 6})
        self.assertEqual(
            [record.to_dict() for record in first.questions],
            [record.to_dict() for record in second.questions],
        )

    def test_balanced_type_sampling_is_exact_and_seeded(self) -> None:
        tasks = []
        task_index = 1
        for task_type, count in (("node", 4), ("edge", 3), ("graph", 2)):
            for _ in range(count):
                tasks.append(
                    {
                        **_task(f"task_{task_index:04d}"),
                        "task_type": task_type,
                    }
                )
                task_index += 1
        path = self.write("theory", "cora", _standard("theory", "cora", tasks))
        arguments = {
            "questions_per_type": 2,
            "type_key": lambda question: str(question.task_type),
            "expected_type_keys": lambda _loaded: ("node", "edge", "graph"),
            "shuffle": True,
            "seed": 17,
        }

        first = load_question_files([path], **arguments)
        second = load_question_files([path], **arguments)

        self.assertEqual(first.question_count, 6)
        self.assertEqual(first.questions_per_type, 2)
        self.assertEqual(
            Counter(question.task_type for question in first.questions),
            {"node": 2, "edge": 2, "graph": 2},
        )
        self.assertEqual(
            first.available_type_counts,
            {"edge": 3, "graph": 2, "node": 4},
        )
        self.assertEqual(
            first.selected_type_counts,
            {"edge": 2, "graph": 2, "node": 2},
        )
        self.assertEqual(first.selected_file_counts, {"theory/cora": 6})
        self.assertEqual(
            [question.composite_id for question in first.questions],
            [question.composite_id for question in second.questions],
        )
        self.assertTrue(
            all(
                question.source_index
                == int(question.id.rsplit("_", 1)[1]) - 1
                for question in first.questions
            )
        )

    def test_balanced_type_sampling_fails_on_shortage_and_conflicting_caps(self) -> None:
        path = self.write(
            "theory",
            "cora",
            _standard(
                "theory",
                "cora",
                [
                    {**_task("task_0001"), "task_type": "node"},
                    {**_task("task_0002"), "task_type": "edge"},
                ],
            ),
        )
        callbacks = {
            "type_key": lambda question: str(question.task_type),
            "expected_type_keys": lambda _loaded: ("node", "edge"),
        }

        with self.assertRaisesRegex(
            QuestionDiscoveryError,
            r"edge: available=1, required=2.*node: available=1, required=2",
        ):
            load_question_files([path], questions_per_type=2, **callbacks)
        with self.assertRaisesRegex(QuestionDiscoveryError, "cannot be combined"):
            load_question_files(
                [path],
                question_count=1,
                questions_per_type=1,
                **callbacks,
            )
        with self.assertRaisesRegex(QuestionDiscoveryError, "positive integer"):
            load_question_files([path], questions_per_type=0, **callbacks)

    def test_duplicate_ids_are_rejected_before_sampling(self) -> None:
        path = self.write(
            "theory",
            "hiv",
            _standard("theory", "hiv", [_task("same"), _task("same")]),
        )

        with self.assertRaisesRegex(QuestionSchemaError, "duplicate composite id"):
            load_question_file(path)
        with self.assertRaises(QuestionSchemaError):
            load_questions(self.root, question_count=1)

    def test_bad_task_fields_are_rejected(self) -> None:
        invalid_tasks = [
            {**_task("task_0001"), "question": ""},
            {**_task("task_0001"), "required_tools": "shortest_path"},
            {**_task("task_0001"), "context": []},
            {key: value for key, value in _task("task_0001").items() if key != "ground_truth"},
            {**_task("task_0001"), "ground_truth": [1, 2]},
        ]
        for index, task in enumerate(invalid_tasks):
            with self.subTest(index=index):
                path = self.write(
                    "prediction",
                    f"bad{index}",
                    _standard("prediction", f"bad{index}", [task]),
                )
                with self.assertRaises(QuestionSchemaError):
                    load_question_file(path)

    def test_bad_limits_and_empty_selection_are_rejected(self) -> None:
        self.write("ml", "hiv", _standard("ml", "hiv", [_task("task_0001")]))

        with self.assertRaises(QuestionDiscoveryError):
            load_questions(self.root, questions_per_file=-1)
        with self.assertRaises(QuestionDiscoveryError):
            load_questions(self.root, shuffle_seed=True)
        with self.assertRaises(QuestionDiscoveryError):
            load_questions(self.root, datasets="missing")


if __name__ == "__main__":
    unittest.main()

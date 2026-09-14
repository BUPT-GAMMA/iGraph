from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from benchmark_platform.questions import QuestionRecord, load_question_file
from benchmark_platform.runner import RunnerConfig, _dataset_path


def _standard_question(category: str, dataset: str) -> dict:
    return {
        "schema": "igraph.benchmark.questions",
        "schema_version": "1.0",
        "category": category,
        "dataset": dataset,
        "metadata": {},
        "tasks": [
            {
                "id": "task_0001",
                "question": f"Evaluate {category}/{dataset}",
                "task_type": "node",
                "required_tools": [],
                "ground_truth": 1,
                "context": {},
            }
        ],
    }


class DatasetRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_dir.name)
        self.questions_root = self.root / "questions"
        self.dataset_root = self.root / "dataset"
        self.dataset_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_dir.cleanup()

    def _write_question(
        self,
        category: str,
        dataset: str,
        *,
        root: Path | None = None,
    ) -> QuestionRecord:
        question_file = (
            (self.questions_root if root is None else root)
            / category
            / dataset
            / "questions.json"
        )
        question_file.parent.mkdir(parents=True, exist_ok=True)
        question_file.write_text(
            json.dumps(_standard_question(category, dataset)),
            encoding="utf-8",
        )
        return load_question_file(question_file).questions[0]

    def _config(self) -> RunnerConfig:
        return RunnerConfig(
            output_dir=self.root / "output",
            dataset_root=self.dataset_root,
            questions_root=self.questions_root,
        )

    def test_ml_uses_category_local_pure_ml_masked_dataset(self) -> None:
        question = self._write_question("ml", "bail")
        expected = (
            self.questions_root / "ml" / "bail" / "pure_ml_bench_masked_data.pt"
        )
        expected.touch()

        self.assertEqual(_dataset_path(self._config(), question), expected.resolve())

    def test_prediction_uses_category_local_intermediate_masked_dataset(self) -> None:
        question = self._write_question("prediction", "bail")
        expected = (
            self.questions_root
            / "prediction"
            / "bail"
            / "intermediate_ground_truth_masked_data.pt"
        )
        expected.touch()

        self.assertEqual(_dataset_path(self._config(), question), expected.resolve())

    def test_other_categories_keep_shared_dataset_root_routing(self) -> None:
        for category, dataset in (
            ("theory", "cora"),
            ("retrieval", "pubmed"),
            ("insight", "hiv"),
        ):
            with self.subTest(category=category, dataset=dataset):
                question = self._write_question(category, dataset)
                expected = self.dataset_root / f"{dataset}.pt"
                expected.touch()

                self.assertEqual(
                    _dataset_path(self._config(), question),
                    expected.resolve(),
                )

    def test_dataset_directory_case_is_preserved_for_nba(self) -> None:
        question = self._write_question("ml", "NBA")
        expected = (
            self.questions_root / "ml" / "NBA" / "pure_ml_bench_masked_data.pt"
        )
        expected.touch()

        self.assertEqual(_dataset_path(self._config(), question), expected.resolve())
        self.assertEqual(_dataset_path(self._config(), question).parent.name, "NBA")

    def test_category_local_source_file_must_stay_within_questions_root(self) -> None:
        outside_root = self.root / "outside-questions"
        question = self._write_question("ml", "bail", root=outside_root)
        (outside_root / "ml" / "bail" / "pure_ml_bench_masked_data.pt").touch()

        with self.assertRaises(ValueError):
            _dataset_path(self._config(), question)

    def test_category_local_source_scope_must_match_question_scope(self) -> None:
        question = self._write_question("ml", "bail")
        expected = (
            self.questions_root / "ml" / "bail" / "pure_ml_bench_masked_data.pt"
        )
        expected.touch()

        mismatched_dataset = replace(
            question,
            composite_id="ml/cora/task_0001",
            dataset="cora",
        )
        mismatched_category = replace(
            question,
            composite_id="prediction/bail/task_0001",
            category="prediction",
        )

        for record in (mismatched_dataset, mismatched_category):
            with self.subTest(composite_id=record.composite_id):
                with self.assertRaises(ValueError):
                    _dataset_path(self._config(), record)

    def test_bare_programmatic_ml_and_prediction_tasks_fail_closed(self) -> None:
        config = replace(self._config(), questions_root=None)
        for category, filename in (
            ("ml", "pure_ml_bench_masked_data.pt"),
            ("prediction", "intermediate_ground_truth_masked_data.pt"),
        ):
            with self.subTest(category=category):
                (self.dataset_root / "bail.pt").touch()
                task = {
                    "composite_id": f"{category}/bail/task_0001",
                    "id": "task_0001",
                    "category": category,
                    "dataset": "bail",
                    "question": "Evaluate bail",
                    "ground_truth": 1,
                    "required_tools": [],
                    "context": {"task_index": 0},
                }
                with self.assertRaisesRegex(ValueError, "category-local masked"):
                    _dataset_path(config, task)


if __name__ == "__main__":
    unittest.main()

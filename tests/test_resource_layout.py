from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.migration import migrate_bundled_skills
from agent_runtime.agent.skills.registry import SkillRegistry


# Keep the shipped inventory explicit so missing, unexpected or duplicate skills
# are reported by name, including the graph-construction workflow.
EXPECTED_BUNDLED_SKILLS = (
    "data_ingestion_table",
    "data_ingestion_text",
    "dataset_management",
    "experiment_reporting",
    "finance-ownership-workflow",
    "gnn_node_classification_training",
    "graph_analysis_centrality",
    "graph_analysis_community",
    "graph_construction",
    "graph_ml_inference",
    "graph_ml_training",
    "graph_path_connectivity",
    "graph_rag",
    "graph_state_check",
    "graph_statistical_reporting",
    "graph_visualization",
    "molecular-analysis-workflow",
    "remote_graph_qa",
)


class ResourceLayoutTest(unittest.TestCase):
    def test_registry_reads_bundled_skills_without_runtime_initialization(self) -> None:
        specs = SkillRegistry(skill_dirs=[]).discover()
        self.assertEqual(specs, [])

        specs = SkillRegistry().discover()
        bundled = [spec for spec in specs if spec.source == "bundled"]
        self.assertCountEqual(
            [spec.name for spec in bundled], EXPECTED_BUNDLED_SKILLS
        )

    def test_bundled_skills_seed_an_empty_runtime_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"IGRAPH_DATA_ROOT": temp_dir}, clear=False):
                migrate_bundled_skills()

            skills_dir = Path(temp_dir) / "global" / "skills"
            skill_names = sorted(
                path.parent.name for path in skills_dir.glob("*/SKILL.md")
            )

        self.assertCountEqual(skill_names, EXPECTED_BUNDLED_SKILLS)


if __name__ == "__main__":
    unittest.main()

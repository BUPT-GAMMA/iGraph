from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.migration import migrate_bundled_skills
from agent_runtime.agent.skills.registry import SkillRegistry


class ResourceLayoutTest(unittest.TestCase):
    def test_registry_reads_bundled_skills_without_runtime_initialization(self) -> None:
        specs = SkillRegistry(skill_dirs=[]).discover()
        self.assertEqual(specs, [])

        specs = SkillRegistry().discover()
        bundled = [spec for spec in specs if spec.source == "bundled"]
        self.assertEqual(len(bundled), 14)
        self.assertIn("graph_ml_training", {spec.name for spec in bundled})

    def test_bundled_skills_seed_an_empty_runtime_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"IGRAPH_DATA_ROOT": temp_dir}, clear=False):
                migrate_bundled_skills()

            skills_dir = Path(temp_dir) / "global" / "skills"
            skill_names = sorted(
                path.parent.name for path in skills_dir.glob("*/SKILL.md")
            )

        self.assertEqual(len(skill_names), 14)
        self.assertIn("graph_path_connectivity", skill_names)
        self.assertIn("graph_ml_training", skill_names)
        self.assertNotIn("user_test_skill", skill_names)


if __name__ == "__main__":
    unittest.main()

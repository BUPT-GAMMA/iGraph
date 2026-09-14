from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_benchmark_bundle as bundle


class BenchmarkBundleTest(unittest.TestCase):
    def test_bundle_contains_only_the_headless_allowlist_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "igraph-benchmark"
            bundle.build_bundle(destination)

            self.assertTrue((destination / "run_benchmark.sh").is_file())
            if os.name != "nt":
                self.assertTrue(
                    (destination / "run_benchmark.sh").stat().st_mode & stat.S_IXUSR
                )
            self.assertTrue((destination / "main.py").is_file())
            self.assertTrue(
                (destination / "scripts" / "benchmark_questions.py").is_file()
            )
            self.assertTrue(
                (destination / "benchmark_platform" / "runner.py").is_file()
            )
            launch_wrapper = (
                destination / "scripts" / "run_zhizengzeng_benchmark.sh"
            ).read_text(encoding="utf-8")
            self.assertIn('CONTAINER_CPUS=""', launch_wrapper)
            self.assertIn(
                'RUN_ARGUMENTS+=(--agent-env DOCKER_HOST)',
                launch_wrapper,
            )
            self.assertIn("--rootless-netns-pid", launch_wrapper)
            self.assertIn("IGRAPH_ROOTLESS_NETNS_ENTERED=1", launch_wrapper)
            self.assertIn('--preserve-credentials', launch_wrapper)
            self.assertIn('ORIGINAL_ARGUMENTS=("$@")', launch_wrapper)
            self.assertIn('"${ORIGINAL_ARGUMENTS[@]}"', launch_wrapper)
            self.assertTrue(
                (destination / "graph_analysis_skills" / "graph_ml_tools.py").is_file()
            )
            for forbidden in bundle.FORBIDDEN_TOP_LEVEL:
                self.assertFalse((destination / forbidden).exists())
            self.assertEqual(list(destination.rglob("__pycache__")), [])
            self.assertEqual(list(destination.rglob("*.pyc")), [])

            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from scripts.benchmark_graph_classification "
                        "import _load_runtime_components; "
                        "_load_runtime_components(); print('bundle-import-ok')"
                    ),
                ],
                cwd=destination,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                # Importing torch and the graph stack can take over a minute
                # on a busy shared benchmark host even when the bundle is
                # healthy.  Keep a finite guard without treating cold-start
                # latency as a packaging failure.
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "bundle-import-ok")

    def test_bundle_refuses_to_overwrite_an_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "existing"
            destination.mkdir()
            marker = destination / "keep.txt"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                bundle.build_bundle(destination)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_recursive_allowlist_rejects_nested_login_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir) / "source"
            component = project_root / "benchmark_platform"
            component.mkdir(parents=True)
            (component / "runner.py").write_text("", encoding="utf-8")
            (component / "login.py").write_text("", encoding="utf-8")
            manifest = project_root / "benchmark-package.txt"
            manifest.write_text("benchmark_platform\n", encoding="utf-8")

            with mock.patch.object(bundle, "PROJECT_ROOT", project_root), mock.patch.object(
                bundle, "MANIFEST_PATH", manifest
            ):
                with self.assertRaisesRegex(ValueError, "login"):
                    bundle.build_bundle(Path(temp_dir) / "output")


if __name__ == "__main__":
    unittest.main()

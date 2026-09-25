from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "scripts" / "run_zhizengzeng_benchmark.sh"


def _option_value(arguments: list[str], option: str) -> str:
    return arguments[arguments.index(option) + 1]


@unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "requires bash")
class ZhizengzengLauncherTests(unittest.TestCase):
    def _render_commands(self, *model_arguments: str) -> tuple[list[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            taskset = bin_dir / "taskset"
            taskset.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "print(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            taskset.chmod(taskset.stat().st_mode | stat.S_IXUSR)

            environment = os.environ.copy()
            environment["PATH"] = str(bin_dir) + os.pathsep + environment["PATH"]
            environment["PYTHON_BIN"] = sys.executable
            environment["IGRAPH_QUESTIONS_ROOT"] = str(root / "questions")
            environment["IGRAPH_DATASET_ROOT"] = str(root / "datasets")
            environment["IGRAPH_CODEX_HARNESS_ROOT"] = str(root / "harness")
            environment["IGRAPH_PROVIDER_PROFILE"] = str(root / "provider-profile.sh")
            completed = subprocess.run(
                [
                    "bash",
                    str(LAUNCHER),
                    "--output-dir",
                    str(root / "output"),
                    "--cpuset-cpus",
                    "0",
                    "--questions-per-type",
                    "1",
                    "--skills-dir",
                    str(root / "skills"),
                    "--categories",
                    "insight",
                    "--evaluate",
                    *model_arguments,
                    "--",
                    "NAG",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        runner_arguments = json.loads(completed.stdout)
        agent_command = json.loads(
            _option_value(runner_arguments, "--agent-command-json")
        )
        judge_command = json.loads(
            _option_value(runner_arguments, "--judge-command-json")
        )
        return agent_command, judge_command

    def test_igraph_backend_selects_project_agent_adapter(self) -> None:
        agent_command, judge_command = self._render_commands(
            "--agent-backend",
            "igraph",
            "--igraph-runtime-root",
            "/srv/igraph-source",
            "--agent-model",
            "gpt-5.6-sol",
            "--judge-model",
            "deepseek-v4.1-flash",
        )

        self.assertTrue(agent_command[1].endswith("igraph_agent_adapter.py"))
        self.assertNotIn("codex_harness_adapter.py", " ".join(agent_command))
        self.assertEqual(_option_value(agent_command, "--runtime-root"), "/srv/igraph-source")
        self.assertTrue(_option_value(agent_command, "--skills-dir").endswith("skills"))
        self.assertEqual(_option_value(agent_command, "--model"), "gpt-5.6-sol")
        self.assertEqual(_option_value(judge_command, "--model"), "deepseek-v4.1-flash")

    def test_role_models_have_independent_defaults(self) -> None:
        agent_command, judge_command = self._render_commands()

        self.assertEqual(_option_value(agent_command, "--model"), "glm-5-turbo")
        self.assertEqual(_option_value(judge_command, "--model"), "gpt-5-mini")

    def test_legacy_model_is_fallback_for_role_specific_options(self) -> None:
        agent_command, judge_command = self._render_commands(
            "--judge-model",
            "judge-specific",
            "--model",
            "legacy-model",
            "--agent-model",
            "agent-specific",
        )

        self.assertEqual(_option_value(agent_command, "--model"), "agent-specific")
        self.assertEqual(_option_value(judge_command, "--model"), "judge-specific")

    def test_legacy_model_still_configures_both_roles(self) -> None:
        agent_command, judge_command = self._render_commands(
            "--model",
            "legacy-model",
        )

        self.assertEqual(_option_value(agent_command, "--model"), "legacy-model")
        self.assertEqual(_option_value(judge_command, "--model"), "legacy-model")


if __name__ == "__main__":
    unittest.main()

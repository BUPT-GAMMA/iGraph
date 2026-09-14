"""Opt-in rootless-container smoke test for Codex workspace cleanup.

This test never invokes an agent or an LLM.  Run it only on a benchmark host:

    IGRAPH_RUN_DOCKER_SMOKE=1 python -m unittest \
      tests.test_server_rootless_cleanup
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from scripts.codex_harness_adapter import (
    _capture_workspace_owner,
    _restore_workspace_ownership,
)


@unittest.skipUnless(
    os.environ.get("IGRAPH_RUN_DOCKER_SMOKE") == "1",
    "set IGRAPH_RUN_DOCKER_SMOKE=1 on a benchmark host",
)
class RootlessWorkspaceCleanupSmokeTests(unittest.TestCase):
    def test_container_visible_owner_restores_host_deletability(self) -> None:
        docker = shutil.which("docker")
        self.assertIsNotNone(docker, "docker executable is unavailable")
        image = os.environ.get("IGRAPH_DOCKER_SMOKE_IMAGE", "sandbox_codex:latest")
        cpuset = os.environ.get("IGRAPH_DOCKER_SMOKE_CPUSET", "32")
        container = f"igraph-cleanup-smoke-{uuid.uuid4().hex[:12]}"

        with tempfile.TemporaryDirectory(prefix="igraph-cleanup-smoke-") as directory:
            attempt_root = Path(directory) / "attempt"
            workspace_root = attempt_root / "codex-workspace"
            workspace_exec = workspace_root / "exec"
            workspace_exec.mkdir(parents=True)
            dataset_target = workspace_exec / "fixture.pt"
            dataset_target.write_bytes(b"immutable-dataset-fixture")
            dataset_uid = dataset_target.stat().st_uid
            dataset_hash = hashlib.sha256(dataset_target.read_bytes()).hexdigest()

            command = [
                str(docker),
                "run",
                "-d",
                "--name",
                container,
                "--network",
                "none",
                "--cpuset-cpus",
                cpuset,
                "--memory",
                "256m",
                "--pids-limit",
                "64",
                "--env",
                "NVIDIA_VISIBLE_DEVICES=void",
                "-v",
                f"{workspace_exec}:{workspace_exec}:rw",
                "-v",
                f"{dataset_target}:{dataset_target}:ro",
                image,
                "/bin/bash",
                "-c",
                "tail -f /dev/null",
            ]
            started = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            try:
                owner, detail = _capture_workspace_owner(
                    str(docker),
                    container,
                    workspace_exec,
                    attempt_root=attempt_root,
                    timeout_seconds=20,
                )
                self.assertIsNotNone(owner, detail)

                corrupted = subprocess.run(
                    [
                        str(docker),
                        "exec",
                        container,
                        "/bin/bash",
                        "-c",
                        (
                            'set -eu; mkdir -p "$1/nested"; '
                            ': > "$1/nested/virtual_ml.py"; '
                            'chown -R 1:1 -- "$1/nested"; '
                            'chmod 0555 -- "$1/nested"'
                        ),
                        "igraph-corrupt-owner",
                        str(workspace_exec),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(corrupted.returncode, 0, corrupted.stderr)

                restored, detail = _restore_workspace_ownership(
                    str(docker),
                    container,
                    workspace_exec,
                    dataset_target,
                    attempt_root=attempt_root,
                    container_owner=str(owner),
                    timeout_seconds=20,
                )
                self.assertTrue(restored, detail)
            finally:
                subprocess.run(
                    [str(docker), "rm", "-f", container],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )

            self.assertEqual(dataset_target.stat().st_uid, dataset_uid)
            self.assertEqual(
                hashlib.sha256(dataset_target.read_bytes()).hexdigest(),
                dataset_hash,
            )
            self.assertEqual(
                (workspace_exec / "nested" / "virtual_ml.py").stat().st_uid,
                os.getuid(),
            )
            shutil.rmtree(workspace_root)
            self.assertFalse(workspace_root.exists())


if __name__ == "__main__":
    unittest.main()

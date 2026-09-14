from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
import signal
import tempfile
import unittest
from unittest import mock

from scripts import codex_harness_adapter as adapter


class CodexHarnessAdapterTests(unittest.TestCase):
    def test_historical_tool_output_never_forces_terminal_failure(self) -> None:
        outputs = (
            '{"metadata":{"exit_code":0},"output":"Error loading graph"}',
            '{"metadata":{"exit_code":2},"output":"real early failure"}',
            '{"status":"failed","timed_out":true}',
            "Error: unstructured intermediate failure",
        )

        for output in outputs:
            with self.subTest(output=output):
                self.assertFalse(
                    adapter._tool_output_requires_terminal_failure(output)
                )

    def test_recovered_tool_failure_keeps_completed_transcript_successful(self) -> None:
        transcript = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "failed-call",
                    "name": "load_graph",
                    "arguments": "{}",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "failed-call",
                    "output": '{"metadata":{"exit_code":2},"output":"failed"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "recovery-call",
                    "name": "train_graph_model",
                    "arguments": "{}",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "recovery-call",
                    "output": '{"metadata":{"exit_code":0},"output":"trained"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "final answer"}],
                },
            },
        ]

        result = adapter._result_from_transcript(
            transcript,
            execution_error=None,
            cleanup_errors=(),
            usage={},
            model="gpt-5-mini",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            [call["success"] for call in result["tool_calls"]],
            [False, True],
        )

    def test_codex_provider_uses_responses_wire_api_not_legacy_chat(self) -> None:
        for model in ("gpt-5-mini", "another-provider-model"):
            with self.subTest(model=model):
                wire_api = adapter._responses_wire_api_for_model(model)
                self.assertEqual(wire_api, "responses")
                self.assertNotEqual(wire_api, "chat")

    def test_docker_exec_rewrite_inserts_taskset_after_options_and_container(self) -> None:
        original = [
            "exec",
            "-it",
            "-u",
            "1000:1000",
            "-e",
            "A=B",
            "--env=C=D",
            "-w",
            "/workspace",
            "--privileged",
            "container-id",
            "/bin/bash",
            "-lc",
            "python worker.py",
        ]

        rewritten = adapter._rewrite_docker_cli_for_taskset(original, "4-5")

        container_index = rewritten.index("container-id")
        self.assertEqual(rewritten[:container_index], original[:10])
        self.assertEqual(
            rewritten[container_index:],
            [
                "container-id",
                "taskset",
                "-c",
                "4-5",
                "/bin/bash",
                "-lc",
                "python worker.py",
            ],
        )

    def test_docker_exec_rewrite_supports_attached_options_and_separator(self) -> None:
        rewritten = adapter._rewrite_docker_cli_for_taskset(
            [
                "exec",
                "-uroot",
                "-eA=B",
                "-w/workspace",
                "--",
                "-container-name",
                "python",
                "worker.py",
            ],
            "0-3,8",
        )

        self.assertEqual(
            rewritten,
            [
                "exec",
                "-uroot",
                "-eA=B",
                "-w/workspace",
                "--",
                "-container-name",
                "taskset",
                "-c",
                "0-3,8",
                "python",
                "worker.py",
            ],
        )

    def test_docker_shim_passes_other_subcommands_and_rejects_ambiguous_exec(self) -> None:
        original = ["run", "--rm", "image", "true"]
        self.assertEqual(
            adapter._rewrite_docker_cli_for_taskset(original, "2-3"),
            original,
        )
        for arguments in (
            ["exec", "--unknown", "container", "true"],
            ["exec", "-u"],
            ["exec", "container"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    adapter._rewrite_docker_cli_for_taskset(arguments, "2-3")

    def test_private_docker_shim_preserves_argv_without_eval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_docker = root / "docker-real"
            real_docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            real_docker.chmod(0o700)

            shim = adapter._write_docker_exec_taskset_shim(
                root / "host-bin",
                docker=str(real_docker),
                cpuset_cpus="4-5",
            )
            launcher = shim.read_text(encoding="utf-8")

        self.assertIn(adapter._DOCKER_SHIM_MODE, launcher)
        self.assertIn('"$@"', launcher)
        self.assertNotIn("eval", launcher)

    def test_main_routes_private_docker_shim_mode_before_agent_setup(self) -> None:
        with mock.patch.object(adapter, "_docker_shim_main", return_value=17) as shim:
            result = adapter.main([adapter._DOCKER_SHIM_MODE, "argument"])

        self.assertEqual(result, 17)
        shim.assert_called_once_with(["argument"])

    def test_cpuset_is_required_and_cfs_quota_is_opt_in(self) -> None:
        required = [
            "--harness-root",
            "/srv/harness",
            "--skills-dir",
            "/srv/skills",
            "--provider-profile",
            "/srv/provider.sh",
        ]
        with self.assertRaises(SystemExit) as raised:
            adapter._arguments(required)

        self.assertEqual(raised.exception.code, 2)
        parsed = adapter._arguments(
            [*required, "--container-cpuset-cpus", "4-5"]
        )
        self.assertIsNone(parsed.container_cpus)
        self.assertEqual(parsed.container_cpuset_cpus, "4-5")

    def test_container_resource_values_are_strict(self) -> None:
        self.assertEqual(adapter._container_cpus("1.5"), "1.5")
        self.assertEqual(adapter._container_cpuset_cpus("0-3,8,10-11"), "0-3,8,10-11")
        self.assertEqual(adapter._container_memory("8G"), "8g")
        self.assertEqual(adapter._container_pids_limit("512"), 512)

        invalid_values = (
            (adapter._container_cpus, "0"),
            (adapter._container_cpus, "1e2"),
            (adapter._container_cpuset_cpus, "0-3,3-4"),
            (adapter._container_cpuset_cpus, "3-1"),
            (adapter._container_memory, "5m"),
            (adapter._container_memory, "8gb"),
            (adapter._container_pids_limit, "0"),
            (adapter._container_pids_limit, "+4"),
        )
        for validator, value in invalid_values:
            with self.subTest(validator=validator.__name__, value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    validator(value)

    def test_docker_command_enforces_limits_labels_cid_and_no_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_file = root / "container.env"
            environment_file.write_text("TMPDIR=/tmp\n", encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            exec_path = root / "workspace" / "exec"
            exec_path.mkdir(parents=True)
            dataset_path = exec_path / "cora.pt"
            dataset_path.write_bytes(b"fixture")
            lease = adapter._ContainerLease(
                task_id="igraph-task",
                lease_id="lease123",
                attempt_root=root,
                environment_file=environment_file,
                cidfile=root / "container.cid",
                audit_file=root / "lease.json",
                docker="/usr/bin/docker",
                docker_timeout_seconds=30.0,
                notify_fd=-1,
                watchdog=mock.Mock(),
            )
            args = SimpleNamespace(
                container_cpus="1.5",
                container_cpuset_cpus="4-5",
                container_memory="6g",
                container_pids_limit=256,
                image="sandbox_codex:latest",
            )

            command = adapter._docker_container_command(
                args,
                docker="/usr/bin/docker",
                task_id="igraph-task",
                lease=lease,
                environment_file=environment_file,
                exec_path=exec_path,
                dataset_target=dataset_path,
                config_path=config_path,
            )

        self.assertEqual(command[:3], ["/usr/bin/docker", "run", "-d"])
        self.assertIn("--rm", command)
        for option, value in (
            ("--cidfile", str(lease.cidfile)),
            ("--cpus", "1.5"),
            ("--cpuset-cpus", "4-5"),
            ("--memory", "6g"),
            ("--memory-swap", "6g"),
            ("--pids-limit", "256"),
        ):
            index = command.index(option)
            self.assertEqual(command[index + 1], value)
        self.assertIn(f"{adapter._MANAGED_LABEL}=true", command)
        self.assertIn(f"{adapter._LEASE_LABEL}=lease123", command)
        self.assertIn("NVIDIA_VISIBLE_DEVICES=void", command)
        self.assertIn("CUDA_VISIBLE_DEVICES=-1", command)
        self.assertNotIn("--gpus", command)

    def test_docker_command_omits_unsupported_cfs_quota_but_keeps_cpuset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_file = root / "container.env"
            environment_file.write_text("TMPDIR=/tmp\n", encoding="utf-8")
            exec_path = root / "workspace" / "exec"
            exec_path.mkdir(parents=True)
            dataset_path = exec_path / "cora.pt"
            dataset_path.write_bytes(b"fixture")
            lease = adapter._ContainerLease(
                task_id="igraph-task",
                lease_id="lease123",
                attempt_root=root,
                environment_file=environment_file,
                cidfile=root / "container.cid",
                audit_file=root / "lease.json",
                docker="/usr/bin/docker",
                docker_timeout_seconds=30.0,
                notify_fd=-1,
                watchdog=mock.Mock(),
            )
            args = SimpleNamespace(
                container_cpus=None,
                container_cpuset_cpus="4-5",
                container_memory="6g",
                container_pids_limit=256,
                image="sandbox_codex:latest",
            )

            command = adapter._docker_container_command(
                args,
                docker="/usr/bin/docker",
                task_id="igraph-task",
                lease=lease,
                environment_file=environment_file,
                exec_path=exec_path,
                dataset_target=dataset_path,
                config_path=None,
            )

        self.assertNotIn("--cpus", command)
        cpuset_index = command.index("--cpuset-cpus")
        self.assertEqual(command[cpuset_index + 1], "4-5")

    def test_ownership_restore_excludes_read_only_dataset_bind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt_root = Path(directory)
            workspace_exec = attempt_root / "codex-workspace" / "exec"
            workspace_exec.mkdir(parents=True)
            dataset_target = workspace_exec / "cora.pt"
            dataset_target.write_bytes(b"fixture")
            completed = SimpleNamespace(returncode=0, stderr="", stdout="")
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=completed,
            ) as run:
                succeeded, detail = adapter._restore_workspace_ownership(
                    "/usr/bin/docker",
                    "igraph-task",
                    workspace_exec,
                    dataset_target,
                    attempt_root=attempt_root,
                    container_owner="0:0",
                    timeout_seconds=17.0,
                )

        self.assertTrue(succeeded, detail)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [
            "/usr/bin/docker",
            "exec",
            "igraph-task",
            "/bin/bash",
        ])
        self.assertIn('-path "$2" -prune', command[5])
        self.assertIn("-xdev", command[5])
        self.assertIn("/bin/chmod u+rwx", command[5])
        self.assertEqual(command[-3:], [
            str(workspace_exec.resolve()),
            str(dataset_target.resolve()),
            "0:0",
        ])
        self.assertEqual(run.call_args.kwargs["timeout"], 17.0)

    def test_workspace_owner_is_captured_in_the_container_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt_root = Path(directory)
            workspace_exec = attempt_root / "codex-workspace" / "exec"
            workspace_exec.mkdir(parents=True)
            completed = SimpleNamespace(returncode=0, stderr="", stdout="0:0\n")
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=completed,
            ) as run:
                owner, detail = adapter._capture_workspace_owner(
                    "/usr/bin/docker",
                    "igraph-task",
                    workspace_exec,
                    attempt_root=attempt_root,
                    timeout_seconds=17.0,
                )

        self.assertEqual(owner, "0:0", detail)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [
            "/usr/bin/docker",
            "exec",
            "igraph-task",
        ])
        self.assertEqual(command[-1], str(workspace_exec.resolve()))
        self.assertNotIn("1000:1001", command)

    def test_workspace_owner_capture_rejects_invalid_stat_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt_root = Path(directory)
            workspace_exec = attempt_root / "codex-workspace" / "exec"
            workspace_exec.mkdir(parents=True)
            completed = SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="unexpected owner\n",
            )
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=completed,
            ):
                owner, detail = adapter._capture_workspace_owner(
                    "/usr/bin/docker",
                    "igraph-task",
                    workspace_exec,
                    attempt_root=attempt_root,
                    timeout_seconds=17.0,
                )

        self.assertIsNone(owner)
        self.assertIn("invalid owner", detail)

    def test_ownership_restore_rejects_dataset_outside_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt_root = Path(directory)
            workspace_exec = attempt_root / "codex-workspace" / "exec"
            workspace_exec.mkdir(parents=True)
            dataset_target = attempt_root / "dataset.pt"
            dataset_target.write_bytes(b"fixture")
            with mock.patch.object(adapter.subprocess, "run") as run:
                succeeded, detail = adapter._restore_workspace_ownership(
                    "/usr/bin/docker",
                    "igraph-task",
                    workspace_exec,
                    dataset_target,
                    attempt_root=attempt_root,
                    container_owner="0:0",
                    timeout_seconds=17.0,
                )

        self.assertFalse(succeeded)
        self.assertIn("dataset outside", detail)
        run.assert_not_called()

    def test_transcript_is_normalized_into_answer_and_tool_calls(self) -> None:
        transcript = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "get_density",
                    "arguments": "{}",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": '{"ok":true}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "The answer is 4."}],
                },
            },
        ]

        result = adapter._result_from_transcript(
            transcript,
            execution_error=None,
            cleanup_errors=(),
            usage={"input_tokens": 10},
            model="gpt-5-mini",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["final_answer"], "The answer is 4.")
        self.assertEqual(result["tool_calls"][0]["name"], "get_density")
        self.assertTrue(result["tool_calls"][0]["success"])
        self.assertEqual(result["reasoning"]["model"], "gpt-5-mini")

    def test_missing_final_answer_and_cleanup_failure_are_not_success(self) -> None:
        no_answer = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "get_density",
                    "arguments": "{}",
                },
            }
        ]
        invalid = adapter._result_from_transcript(
            no_answer,
            execution_error=None,
            cleanup_errors=(),
            usage={},
            model="gpt-5-mini",
        )
        cleanup_failed = adapter._result_from_transcript(
            [
                {
                    "type": "response_item",
                    "payload": {"role": "assistant", "content": "done"},
                }
            ],
            execution_error=None,
            cleanup_errors=("ownership restore failed",),
            usage={},
            model="gpt-5-mini",
        )

        self.assertEqual(invalid["status"], "failed")
        self.assertEqual(invalid["error"]["type"], "InvalidCodexTranscript")
        self.assertEqual(cleanup_failed["status"], "failed")
        self.assertEqual(cleanup_failed["error"]["type"], "ContainerCleanupError")

        cleanup_warning = adapter._result_from_transcript(
            [
                {
                    "type": "response_item",
                    "payload": {"role": "assistant", "content": "done"},
                }
            ],
            execution_error=None,
            cleanup_errors=(),
            cleanup_warnings=("temporary workspace could not be removed",),
            usage={},
            model="gpt-5-mini",
        )
        self.assertEqual(cleanup_warning["status"], "success")
        self.assertEqual(
            cleanup_warning["warnings"][0]["type"],
            "ContainerWorkspaceCleanupWarning",
        )

    def test_skill_tool_path_uses_the_selected_directory_name(self) -> None:
        self.assertEqual(
            adapter._container_graph_ml_tool(Path("/srv/custom_graph_skill")),
            "/root/.codex/skills/custom_graph_skill/graph_ml_tools.py",
        )
        with self.assertRaisesRegex(ValueError, "unsafe"):
            adapter._container_graph_ml_tool(Path("/srv/not safe"))

    def test_provider_key_and_broker_token_are_recursively_redacted(self) -> None:
        value = {
            "error": "provider=provider-secret",
            "transcript": [
                {"content": "prefix broker-secret suffix"},
                {"provider-secret": ["broker-secret", 1]},
            ],
        }

        redacted = adapter._redact_secrets(
            value,
            ("provider-secret", "broker-secret"),
        )

        self.assertNotIn("provider-secret", str(redacted))
        self.assertNotIn("broker-secret", str(redacted))
        self.assertIn(adapter._REDACTED, str(redacted))

    def test_soft_signal_raises_a_catchable_cancellation(self) -> None:
        with mock.patch.object(adapter.signal, "signal"):
            with self.assertRaisesRegex(adapter.AdapterCancelled, "SIGTERM"):
                adapter._raise_cancellation(signal.SIGTERM, None)


if __name__ == "__main__":
    unittest.main()

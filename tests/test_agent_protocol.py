from __future__ import annotations

import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import benchmark_platform.agent_protocol as agent_protocol
from benchmark_platform.agent_protocol import (
    execute_agent_command,
    normalize_agent_payload,
    parse_agent_stdout,
)


def _codex_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _codex_output(call_id: str, output: str, exit_code: int = 0) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(
                {"metadata": {"exit_code": exit_code}, "output": output}
            ),
        },
    }


class AgentProtocolTests(unittest.TestCase):
    def test_posix_timeout_allows_graceful_process_group_cleanup(self):
        process = mock.Mock(pid=43200)
        process.wait.return_value = -signal.SIGTERM

        with (
            mock.patch.object(agent_protocol.os, "name", "posix"),
            mock.patch.object(agent_protocol.os, "killpg") as killpg,
        ):
            agent_protocol._terminate_process_group(process)

        killpg.assert_called_once_with(43200, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=30)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_posix_timeout_escalates_to_sigkill_after_cleanup_grace(self):
        process = mock.Mock(pid=43201)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd=["fake-agent"], timeout=30),
            -signal.SIGKILL,
        ]

        with (
            mock.patch.object(agent_protocol.os, "name", "posix"),
            mock.patch.object(agent_protocol.os, "killpg") as killpg,
        ):
            agent_protocol._terminate_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(43201, signal.SIGTERM),
                mock.call(43201, signal.SIGKILL),
            ],
        )
        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(timeout=30), mock.call(timeout=5)],
        )
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_windows_timeout_terminates_then_kills_after_cleanup_grace(self):
        process = mock.Mock(pid=43202)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd=["fake-agent"], timeout=30),
            -1,
        ]

        with mock.patch.object(agent_protocol.os, "name", "nt"):
            agent_protocol._terminate_process_group(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(timeout=30), mock.call(timeout=5)],
        )

    def test_top_level_json_null_is_invalid_output(self):
        execution = normalize_agent_payload(parse_agent_stdout("null"))

        self.assertEqual(execution.status, "invalid_output")
        self.assertIsNone(execution.final_answer)

    def test_structured_errors_and_warnings_keep_their_types(self):
        successful = normalize_agent_payload(
            {
                "status": "success",
                "final_answer": "done",
                "warnings": [
                    {
                        "type": "ContainerWorkspaceCleanupWarning",
                        "message": "temporary workspace remains",
                    }
                ],
            }
        )
        failed = normalize_agent_payload(
            {
                "status": "failed",
                "error": {
                    "type": "ContainerCleanupError",
                    "message": "container remains",
                },
            }
        )

        self.assertEqual(successful.status, "success")
        self.assertEqual(
            successful.warnings[0]["type"],
            "ContainerWorkspaceCleanupWarning",
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error["type"], "ContainerCleanupError")

    def test_codex_jsonl_pairs_function_calls_and_outputs_in_order(self):
        transcript = [
            _codex_call(
                "call-1",
                "shell",
                {
                    "command": [
                        "python",
                        "/skills/graph_theory_tools.py",
                        "get_density",
                        "cora.pt",
                        "{}",
                    ]
                },
            ),
            _codex_output("call-1", '{"density": 0.25}'),
            _codex_call(
                "call-2",
                "shell",
                {
                    "command": [
                        "python",
                        "/skills/graph_theory_tools.py",
                        "get_node_degree",
                        "cora.pt",
                        '{"node": 7}',
                    ]
                },
            ),
            _codex_output("call-2", "Error: invalid node", exit_code=2),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": '{"answer": 0.25}'}
                    ],
                },
            },
        ]

        execution = normalize_agent_payload(transcript)

        self.assertEqual(execution.status, "success")
        self.assertEqual(execution.final_answer, {"answer": 0.25})
        self.assertEqual(len(execution.tool_calls), 2)
        self.assertEqual(
            [call["call_id"] for call in execution.tool_calls],
            ["call-1", "call-2"],
        )
        self.assertEqual(execution.tool_calls[0]["raw_name"], "shell")
        self.assertIsInstance(execution.tool_calls[0]["arguments"], dict)
        self.assertTrue(execution.tool_calls[0]["success"])
        self.assertFalse(execution.tool_calls[1]["success"])
        self.assertEqual(
            execution.tool_calls[0]["result"]["metadata"]["exit_code"], 0
        )

    def test_structured_success_is_authoritative_over_error_like_output(self):
        successful_results = (
            {"metadata": {"exit_code": 0}, "output": "Error loading graph"},
            {"exit_code": "0", "output": "Traceback (most recent call last)"},
            {"status": "completed", "output": "Error: source-code fixture"},
            {"ok": True, "output": "command not found in documentation"},
            {"success": True, "output": "exception: expected example"},
        )

        for result in successful_results:
            with self.subTest(result=result):
                self.assertTrue(agent_protocol._result_success(result))

    def test_structured_failure_signals_are_retained_per_tool_call(self):
        failed_results = (
            {"metadata": {"exit_code": 2}, "output": "partial output"},
            {"exit_code": 1, "output": "partial output"},
            {"status": "timed-out", "output": "partial output"},
            {"timed_out": True, "output": "partial output"},
            {"is_error": True, "output": "partial output"},
            {"ok": False, "output": "partial output"},
            {"success": False, "output": "partial output"},
        )

        for result in failed_results:
            with self.subTest(result=result):
                self.assertFalse(agent_protocol._result_success(result))

    def test_error_markers_are_only_a_fallback_without_structured_signal(self):
        self.assertFalse(agent_protocol._result_success("Error: invalid node"))
        self.assertFalse(
            agent_protocol._result_success({"output": "command not found"})
        )
        self.assertTrue(agent_protocol._result_success("Error loading graph"))

    def test_unanswered_codex_function_call_is_retained_as_failed(self):
        execution = normalize_agent_payload(
            [_codex_call("pending", "shell", {"command": ["true"]})]
        )
        self.assertEqual(len(execution.tool_calls), 1)
        self.assertEqual(execution.tool_calls[0]["call_id"], "pending")
        self.assertIsNone(execution.tool_calls[0]["result"])
        self.assertFalse(execution.tool_calls[0]["success"])

    def test_parse_agent_stdout_accepts_one_json_line_after_diagnostics(self):
        parsed = parse_agent_stdout('diagnostic line\n{"status":"success","answer":4}\n')
        self.assertEqual(parsed["answer"], 4)

    def test_execute_agent_command_passes_json_envelope_and_attempt_environment(self):
        response = {
            "status": "success",
            "final_answer": {"answer": 3},
            "tool_calls": [],
        }
        created_processes = []

        class FakeProcess:
            def __init__(self, stdout, stderr):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = 0
                self.pid = 43210
                self.communicate_input = None
                self.communicate_timeout = None
                created_processes.append(self)

            def communicate(self, input=None, timeout=None):
                self.communicate_input = input
                self.communicate_timeout = timeout
                self.stdout.write(json.dumps(response).encode("utf-8"))
                self.stderr.write(b"")
                self.stdout.flush()
                self.stderr.flush()
                return None, None

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        def fake_popen(_command, **kwargs):
            return FakeProcess(kwargs["stdout"], kwargs["stderr"])

        with tempfile.TemporaryDirectory() as temporary_dir:
            with mock.patch(
                "benchmark_platform.agent_protocol.subprocess.Popen",
                side_effect=fake_popen,
            ) as popen:
                execution = execute_agent_command(
                    ["fake-agent", "--json"],
                    {"question_id": "ml/cora/task-1", "attempt": 2},
                    cwd=Path(temporary_dir),
                    environment={
                        "IGRAPH_VIRTUAL_ML_BROKER_ENDPOINT": "http://127.0.0.1:43123/invoke",
                        "IGRAPH_VIRTUAL_ML_BROKER_TOKEN": "attempt-token",
                    },
                    timeout_seconds=12,
                    inherit_environment=False,
                )

        self.assertEqual(execution.final_answer, {"answer": 3})
        self.assertEqual(len(created_processes), 1)
        process = created_processes[0]
        self.assertEqual(json.loads(process.communicate_input)["attempt"], 2)
        self.assertEqual(process.communicate_timeout, 12)
        kwargs = popen.call_args.kwargs
        self.assertEqual(
            kwargs["env"]["IGRAPH_VIRTUAL_ML_BROKER_ENDPOINT"],
            "http://127.0.0.1:43123/invoke",
        )
        self.assertEqual(
            kwargs["env"]["IGRAPH_VIRTUAL_ML_BROKER_TOKEN"], "attempt-token"
        )
        self.assertEqual(popen.call_args.args[0], ["fake-agent", "--json"])
        self.assertTrue(kwargs["text"])

    def test_execute_parses_full_jsonl_while_persisting_bounded_capture_tail(self):
        events = [
            _codex_call(
                "call-early",
                "get_density",
                {"dataset": "cora.pt", "padding": "x" * 512},
            ),
            _codex_output("call-early", '{"density": 0.25}'),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": '{"answer": 0.25}'}
                    ],
                },
            },
        ]
        stdout_payload = "\n".join(json.dumps(event) for event in events) + "\n"
        stderr_payload = b"diagnostic-" * 20

        class FakeProcess:
            def __init__(self, stdout, stderr):
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = 0
                self.pid = 43211

            def communicate(self, input=None, timeout=None):
                self.stdout.write(stdout_payload.encode("utf-8"))
                self.stderr.write(stderr_payload)
                self.stdout.flush()
                self.stderr.flush()
                return None, None

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        def fake_popen(_command, **kwargs):
            return FakeProcess(kwargs["stdout"], kwargs["stderr"])

        capture_limit = 64
        with tempfile.TemporaryDirectory() as temporary_dir:
            workspace = Path(temporary_dir)
            with mock.patch(
                "benchmark_platform.agent_protocol.subprocess.Popen",
                side_effect=fake_popen,
            ):
                execution = execute_agent_command(
                    ["fake-agent"],
                    {"question_id": "theory/cora/task-1"},
                    cwd=workspace,
                    max_capture_bytes=capture_limit,
                    inherit_environment=False,
                )

            stdout_log = Path(execution.stdout_log_path or "")
            stderr_log = Path(execution.stderr_log_path or "")
            self.assertTrue(stdout_log.is_absolute())
            self.assertTrue(stderr_log.is_absolute())
            self.assertTrue(stdout_log.is_file())
            self.assertTrue(stderr_log.is_file())
            self.assertEqual(stdout_log.read_bytes(), stdout_payload.encode("utf-8"))
            self.assertEqual(stderr_log.read_bytes(), stderr_payload)
            self.assertEqual(execution.stdout_bytes, len(stdout_payload.encode("utf-8")))
            self.assertEqual(execution.stderr_bytes, len(stderr_payload))
            self.assertTrue(execution.stdout_truncated)
            self.assertTrue(execution.stderr_truncated)
            self.assertLessEqual(len(execution.stdout.encode("utf-8")), capture_limit)
            self.assertLessEqual(len(execution.stderr.encode("utf-8")), capture_limit)

            # The first JSONL event is well outside the display tail, but the
            # full spool is used for normalization and trajectory extraction.
            self.assertEqual(execution.status, "success")
            self.assertEqual(execution.final_answer, {"answer": 0.25})
            self.assertEqual(len(execution.tool_calls), 1)
            self.assertEqual(execution.tool_calls[0]["call_id"], "call-early")
            self.assertEqual(execution.tool_calls[0]["name"], "get_density")
            self.assertTrue(execution.tool_calls[0]["success"])


if __name__ == "__main__":
    unittest.main()

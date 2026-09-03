from __future__ import annotations

import atexit
import unittest
from unittest.mock import MagicMock, patch

from backend.runtime.process_supervisor import ProcessSupervisor


class ProcessSupervisorStartTest(unittest.TestCase):
    def setUp(self) -> None:
        self._pid_lookup_patch = patch.object(
            ProcessSupervisor,
            "_find_local_pid_by_port",
            return_value=None,
        )
        self._pid_lookup_patch.start()
        self.addCleanup(self._pid_lookup_patch.stop)

    def _new_supervisor(self, graph_data_url: str | None = None) -> ProcessSupervisor:
        supervisor = ProcessSupervisor(
            base_dir=".",
            graph_data_port=19111,
            mcp_port=19110,
            graph_data_url=graph_data_url,
        )
        self.addCleanup(atexit.unregister, supervisor.stop_all)
        return supervisor

    @patch("backend.runtime.process_supervisor.os.killpg")
    @patch("backend.runtime.process_supervisor.os.getpgid")
    def test_stop_proc_rejects_non_integer_mock_pid(self, mock_getpgid, mock_killpg):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = MagicMock()

        ProcessSupervisor._stop_proc(proc)

        mock_getpgid.assert_not_called()
        mock_killpg.assert_not_called()
        proc.kill.assert_called_once_with()

    @patch.object(ProcessSupervisor, "_find_local_pid_by_port", return_value=None)
    @patch.object(ProcessSupervisor, "_is_http_ready", return_value=True)
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_skip_start_graph_data_when_health_is_ready(
        self, mock_popen, _mock_ready, _mock_find_pid
    ):
        supervisor = self._new_supervisor()
        supervisor.start_graph_data_server()
        mock_popen.assert_not_called()

    @patch.object(ProcessSupervisor, "_find_local_pid_by_port", return_value=None)
    @patch.object(ProcessSupervisor, "_is_http_ready", return_value=True)
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_skip_start_graph_data_when_external_url_is_ready(
        self, mock_popen, _mock_ready, _mock_find_pid
    ):
        supervisor = self._new_supervisor(graph_data_url="http://10.0.0.8:19111")
        supervisor.start_graph_data_server()
        mock_popen.assert_not_called()

    @patch.object(ProcessSupervisor, "_is_http_ready", return_value=False)
    @patch.object(ProcessSupervisor, "_wait_http_ready")
    @patch.object(ProcessSupervisor, "_is_port_open", return_value=False)
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_start_local_graph_data_when_external_url_unreachable(
        self, mock_popen, _mock_port, mock_wait, _mock_ready
    ):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        supervisor = self._new_supervisor(graph_data_url="http://10.0.0.8:19111")
        supervisor.start_graph_data_server()
        mock_popen.assert_called_once()
        mock_wait.assert_called_once()
        self.assertEqual(supervisor.effective_graph_data_url, "http://127.0.0.1:19111")

    @patch.object(ProcessSupervisor, "_is_http_ready", return_value=False)
    @patch.object(ProcessSupervisor, "_wait_http_ready")
    @patch.object(ProcessSupervisor, "_is_port_open", return_value=False)
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_start_graph_data_when_health_not_ready(
        self, mock_popen, _mock_port, mock_wait, _mock_ready
    ):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        supervisor = self._new_supervisor()
        supervisor.start_graph_data_server()
        mock_popen.assert_called_once()
        mock_wait.assert_called_once()

    @patch.object(ProcessSupervisor, "_find_local_pid_by_port", return_value=None)
    @patch.object(ProcessSupervisor, "_is_http_ready", side_effect=[False, True])
    @patch.object(ProcessSupervisor, "_is_port_open", return_value=False)
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_use_local_graph_data_when_external_unreachable_but_local_ready(
        self, mock_popen, _mock_port, _mock_ready, _mock_find_pid
    ):
        supervisor = self._new_supervisor(graph_data_url="http://10.0.0.8:19111")
        supervisor.start_graph_data_server()
        mock_popen.assert_not_called()
        self.assertEqual(supervisor.effective_graph_data_url, "http://127.0.0.1:19111")

    @patch.object(ProcessSupervisor, "_find_local_pid_by_port", return_value=None)
    @patch.object(ProcessSupervisor, "_is_http_ready", return_value=False)
    @patch.object(ProcessSupervisor, "_is_port_open", return_value=True)
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_use_local_graph_data_when_external_unreachable_but_local_port_open(
        self, mock_popen, _mock_port, _mock_ready, _mock_find_pid
    ):
        supervisor = self._new_supervisor(graph_data_url="http://10.0.0.8:19111")
        supervisor.start_graph_data_server()
        mock_popen.assert_not_called()
        self.assertEqual(supervisor.effective_graph_data_url, "http://127.0.0.1:19111")

    @patch.object(ProcessSupervisor, "_is_http_ready", return_value=False)
    @patch.object(
        ProcessSupervisor, "_wait_http_ready", side_effect=RuntimeError("timeout")
    )
    @patch.object(ProcessSupervisor, "_is_port_open", side_effect=[False, True])
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_start_graph_data_fallback_to_port_ready_when_health_timeout(
        self, mock_popen, _mock_port, _mock_wait, _mock_ready
    ):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        supervisor = self._new_supervisor()
        supervisor.start_graph_data_server()
        mock_popen.assert_called_once()

    @patch.object(ProcessSupervisor, "_find_local_pid_by_port", return_value=None)
    @patch.object(ProcessSupervisor, "_is_port_open", return_value=True)
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_skip_start_mcp_when_port_is_open(
        self, mock_popen, _mock_open, _mock_find_pid
    ):
        supervisor = self._new_supervisor()
        supervisor.start_mcp_server()
        mock_popen.assert_not_called()

    @patch.object(ProcessSupervisor, "_is_port_open", return_value=False)
    @patch.object(ProcessSupervisor, "_wait_port_ready")
    @patch("backend.runtime.process_supervisor.subprocess.Popen")
    def test_start_mcp_when_port_not_open(self, mock_popen, mock_wait, _mock_open):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        supervisor = self._new_supervisor()
        supervisor.start_mcp_server()
        mock_popen.assert_called_once()
        mock_wait.assert_called_once()

    @patch.object(ProcessSupervisor, "start_graph_data_server")
    @patch.object(ProcessSupervisor, "start_mcp_server")
    def test_start_all(self, mock_start_mcp, mock_start_graph_data):
        supervisor = self._new_supervisor()
        supervisor.start_all()
        mock_start_graph_data.assert_called_once()
        mock_start_mcp.assert_called_once()


if __name__ == "__main__":
    unittest.main()

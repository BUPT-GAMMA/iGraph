"""
子进程监管器

统一管理 GraphData Server 与 MCP Server 的启动、就绪探测与回收，
避免在多个入口重复实现同样的进程生命周期逻辑。
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional


class ProcessSupervisor:
    """管理 GraphData Server 与 MCP Server 的子进程生命周期。"""

    _LOOPBACK_HOST = "127.0.0.1"

    def __init__(
        self,
        base_dir: str,
        graph_data_port: int,
        mcp_port: int,
        graph_data_url: Optional[str] = None,
    ) -> None:
        self.base_dir = base_dir
        self.graph_data_port = graph_data_port
        self.mcp_port = mcp_port
        self.graph_data_url = (
            graph_data_url.rstrip("/")
            if isinstance(graph_data_url, str) and graph_data_url.strip()
            else f"http://{self._LOOPBACK_HOST}:{graph_data_port}"
        )

        self._graph_data_proc: Optional[subprocess.Popen] = None
        self._mcp_proc: Optional[subprocess.Popen] = None
        # PID of a locally-running service that we didn't start ourselves
        # (e.g. left over from a previous crashed run). Tracked so stop_all
        # can still clean it up.
        self._graph_data_local_pid: Optional[int] = None
        self._mcp_local_pid: Optional[int] = None
        self._local_graph_data_started: bool = False

        atexit.register(self.stop_all)

    @property
    def effective_graph_data_url(self) -> str:
        """返回实际可用的 GraphData Server URL。本地 fallback 启动后指向 localhost。"""
        if self._local_graph_data_started:
            return f"http://{self._LOOPBACK_HOST}:{self.graph_data_port}"
        return self.graph_data_url

    def start_graph_data_server(self) -> None:
        """
        启动 GraphData Server 并等待 health 检查通过。

        若配置的 GRAPH_DATA_SERVER_URL 已可达，则直接复用（不再拉起本地子进程）。
        """
        if self._graph_data_proc and self._graph_data_proc.poll() is None:
            return

        configured_health_url = f"{self.graph_data_url}/health"
        if self._is_http_ready(configured_health_url):
            if self._graph_data_url_is_local():
                self._local_graph_data_started = True
                self._graph_data_local_pid = self._find_local_pid_by_port(self.graph_data_port)
            return

        health_url = f"http://{self._LOOPBACK_HOST}:{self.graph_data_port}/health"
        if self._is_http_ready(health_url) or self._is_port_open(self._LOOPBACK_HOST, self.graph_data_port):
            self._local_graph_data_started = True
            self._graph_data_local_pid = self._find_local_pid_by_port(self.graph_data_port)
            return

        env = os.environ.copy()
        env["GRAPH_DATA_HOST"] = "0.0.0.0"
        self._graph_data_proc = subprocess.Popen(
            [sys.executable, "-m", "graph_data_service"],
            cwd=self.base_dir,
            env=env,
            start_new_session=True,   # 独立进程组，stop 时可整组杀
        )
        self._local_graph_data_started = True
        try:
            self._wait_http_ready(health_url)
        except RuntimeError:
            if self._is_port_open(self._LOOPBACK_HOST, self.graph_data_port):
                # 某些环境下 /health 探测可能受代理/网络策略影响，端口可连时允许继续。
                return
            raise

    def _graph_data_url_is_local(self) -> bool:
        try:
            parsed = urllib.parse.urlparse(self.graph_data_url)
            host = (parsed.hostname or "").strip().lower()
        except Exception:
            return False
        return host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}

    def start_mcp_server(self, transport: str = "streamable-http") -> None:
        """启动 MCP Server 并等待端口可连接。"""
        if self._mcp_proc and self._mcp_proc.poll() is None:
            return

        if self._is_port_open(self._LOOPBACK_HOST, self.mcp_port):
            self._mcp_local_pid = self._find_local_pid_by_port(self.mcp_port)
            return

        env = os.environ.copy()
        env["GRAPH_DATA_SERVER_URL"] = self.effective_graph_data_url
        self._mcp_proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_service", transport],
            cwd=self.base_dir,
            env=env,
            start_new_session=True,
        )
        self._wait_port_ready(self._LOOPBACK_HOST, self.mcp_port)

    def start_all(self, mcp_transport: str = "streamable-http") -> None:
        """按依赖顺序启动全部后端子服务。"""
        self.start_graph_data_server()
        self.start_mcp_server(transport=mcp_transport)

    def stop_all(self) -> None:
        """终止全部托管子进程（含接管的残留进程）。"""
        self._stop_proc(self._mcp_proc)
        self._stop_proc(self._graph_data_proc)
        if self._mcp_proc is None and self._mcp_local_pid is not None:
            self._stop_pid(self._mcp_local_pid)
        if self._graph_data_proc is None and self._graph_data_local_pid is not None:
            self._stop_pid(self._graph_data_local_pid)
        self._mcp_proc = None
        self._graph_data_proc = None
        self._mcp_local_pid = None
        self._graph_data_local_pid = None

    @staticmethod
    def _stop_proc(proc: Optional[subprocess.Popen]) -> None:
        if proc is None or proc.poll() is not None:
            return
        pid = getattr(proc, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
            return
        # Send SIGTERM to the entire process group so child workers
        # (ML job pool, etc.) are also terminated and release the port.
        try:
            pgid = os.getpgid(pid)
            if pgid <= 1:
                raise ValueError(f"refusing to signal unsafe process group: {pgid}")
            os.killpg(pgid, signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            # Fallback: force-kill the whole process group first. A direct
            # proc.kill() can leave worker children holding the service port.
            try:
                pgid = os.getpgid(pid)
                if pgid <= 1:
                    raise ValueError(f"refusing to signal unsafe process group: {pgid}")
                os.killpg(pgid, signal.SIGKILL)
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

    @staticmethod
    def _stop_pid(pid: int) -> None:
        """向指定 PID 所在进程组发送 SIGTERM，等待退出。"""
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            return
        try:
            pgid = os.getpgid(pid)
            if pgid <= 1:
                return
            os.killpg(pgid, signal.SIGTERM)
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except OSError:
                    break
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
            return
        try:
            os.kill(pid, 0)
        except OSError:
            return
        try:
            pgid = os.getpgid(pid)
            if pgid <= 1:
                return
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    @staticmethod
    def _find_local_pid_by_port(port: int) -> Optional[int]:
        """返回本地监听指定端口的进程 PID，找不到时返回 None。"""
        try:
            if sys.platform == "win32":
                out = subprocess.check_output(
                    ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
                )
                for line in out.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.split()
                        if parts:
                            return int(parts[-1])
            else:
                out = subprocess.check_output(
                    ["lsof", "-ti", f"TCP:{port}", "-s", "TCP:LISTEN"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                if out:
                    return int(out.splitlines()[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _wait_http_ready(url: str, retries: int = 1000, interval: float = 0.3) -> None:
        last_err: Optional[Exception] = None
        for _ in range(retries):
            try:
                ProcessSupervisor._http_open(url, timeout=1)
                return
            except Exception as e:
                last_err = e
                time.sleep(interval)
        raise RuntimeError(f"服务就绪检查失败: {url} ({last_err})")

    @staticmethod
    def _is_http_ready(url: str) -> bool:
        try:
            ProcessSupervisor._http_open(url, timeout=1)
            return True
        except Exception:
            return False

    @staticmethod
    def _http_open(url: str, timeout: float):
        """
        对本地健康检查禁用系统代理，避免 localhost/127.0.0.1 被代理劫持导致超时。
        """
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(url, timeout=timeout)

    @staticmethod
    def _wait_port_ready(host: str, port: int, retries: int = 300, interval: float = 0.3) -> None:
        last_err: Optional[Exception] = None
        for _ in range(retries):
            try:
                with socket.create_connection((host, port), timeout=1):
                    return
            except Exception as e:
                last_err = e
                time.sleep(interval)
        raise RuntimeError(f"端口就绪检查失败: {host}:{port} ({last_err})")

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except Exception:
            return False

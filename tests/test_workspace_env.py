import pytest

from agent_runtime.workspace_env import InvalidRequirement, validated_requirements


@pytest.mark.parametrize("value", [
    "requests",
    "torch==2.0.1",
    "numpy>=1.24",
    "scikit-learn",
    "pandas!=2.1.0",
    "Flask~=3.0",
    "typing_extensions",
])
def test_accepts_plain_pypi_requirements(value):
    assert validated_requirements([value]) == [value]


@pytest.mark.parametrize("value", [
    "git+https://github.com/psf/requests",
    "http://evil.example/pkg.whl",
    "https://evil.example/pkg.whl",
    "./local.whl",
    "/abs/path/pkg.whl",
    "../escape/pkg.whl",
    "requests; rm -rf /",
    "requests && curl evil.example",
    "requests`whoami`",
    "requests$(id)",
    "requests|tee",
    "--index-url=http://evil.example",
    "-e .",
    "requests\nnumpy",
    "",
    "   ",
])
def test_rejects_non_pypi_and_injection(value):
    with pytest.raises(InvalidRequirement):
        validated_requirements([value])


def test_rejects_empty_and_oversized_lists():
    with pytest.raises(InvalidRequirement):
        validated_requirements([])
    with pytest.raises(InvalidRequirement):
        validated_requirements([f"pkg{index}" for index in range(101)])


def test_strips_whitespace_and_deduplicates_preserving_order():
    assert validated_requirements(["  requests ", "numpy", "requests"]) == ["requests", "numpy"]


def test_rejects_non_string_entries():
    with pytest.raises(InvalidRequirement):
        validated_requirements(["requests", None])


import asyncio
import os
import sys

from agent_runtime.workspace_env import build_environment, environment_python_path


@pytest.mark.parametrize("termination", ["timeout", "cancel"])
def test_installer_termination_stops_process_group(tmp_path, termination):
    from agent_runtime.workspace_env import _run

    async def exercise():
        code = (
            "import subprocess, sys, time\nfrom pathlib import Path\n"
            "subprocess.Popen([sys.executable, '-c', "
            "\"import time; from pathlib import Path; time.sleep(1); Path('orphan').touch()\"])\n"
            "Path('started').touch()\ntime.sleep(2)\n"
        )
        task = asyncio.create_task(_run([sys.executable, "-c", code], cwd=tmp_path,
                                       env=dict(os.environ), timeout=0.3 if termination == "timeout" else 5))
        for _ in range(100):
            if (tmp_path / "started").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "started").exists()
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)[0] == 124
        await asyncio.sleep(1.2)
        assert not (tmp_path / "orphan").exists()

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["create", "install"])
def test_cancelled_build_removes_incomplete_environment(tmp_path, monkeypatch, stage):
    from agent_runtime import workspace_env

    monkeypatch.setattr(workspace_env, "_uv_binary", lambda: "/usr/bin/uv")
    monkeypatch.setattr(workspace_env, "_cache_dir", lambda _: tmp_path / "cache")
    venv = tmp_path / "Environment/venv"
    calls = 0

    async def run(*args, **kwargs):
        nonlocal calls
        calls += 1
        venv.mkdir(parents=True, exist_ok=True)
        if calls == (1 if stage == "create" else 2):
            raise asyncio.CancelledError
        return 0, ""

    monkeypatch.setattr(workspace_env, "_run", run)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(build_environment(tmp_path, ["humanize"], user_id="audit"))
    assert not venv.exists()


def test_environment_python_path_is_workspace_relative():
    assert environment_python_path() == "Environment/venv/bin/python"


def test_build_environment_rejects_bad_requirements_before_running_uv(tmp_path, monkeypatch):
    # uv 不可用也应立即因校验失败返回，证明校验先于任何子进程
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    result = asyncio.run(build_environment(
        tmp_path, ["git+https://evil.example/x"], user_id="u1"))
    assert result["success"] is False
    assert "PyPI" in result["error"]


def test_build_environment_reports_missing_uv(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    result = asyncio.run(build_environment(tmp_path, ["requests"], user_id="u1"))
    assert result["success"] is False
    assert "uv" in result["error"]


def test_build_environment_creates_venv_and_emits_progress(tmp_path):
    # 用标准库自身作为「已安装包」目标，避免测试依赖网络
    events = []

    async def record(payload):
        events.append(payload)

    result = asyncio.run(build_environment(
        tmp_path, ["setuptools"], user_id="u1", on_progress=record))

    if not result["success"]:
        pytest.skip(f"uv 不可用或无网络: {result.get('error')}")

    assert result["python"] == "Environment/venv/bin/python"
    assert (tmp_path / "Environment" / "venv" / "bin" / "python").exists()
    assert (tmp_path / "Environment" / ".packages.txt").read_text().strip() == "setuptools"
    assert [event["job"]["status"] for event in events][-1] == "done"


def test_failed_build_leaves_no_usable_venv(tmp_path):
    result = asyncio.run(build_environment(
        tmp_path, ["igraph-nonexistent-package-xyz-99999"], user_id="u1"))
    assert result["success"] is False
    # 失败必须清理，避免后续调用误用残缺环境
    assert not (tmp_path / "Environment" / "venv").exists()


def test_extra_packages_inherit_and_preserve_shared_compute(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_runtime import workspace_env

    monkeypatch.setattr(workspace_env, "distributions", lambda: [
        SimpleNamespace(metadata={"Name": "torch"}, version="2.14.0+cu130"),
        SimpleNamespace(metadata={"Name": "nvidia_cudnn_cu13"}, version="9.24.0.43"),
        SimpleNamespace(metadata={"Name": "cuda-toolkit"}, version="13.0.3.0"),
        SimpleNamespace(metadata={"Name": "pandas"}, version="3.0.0"),
    ])
    monkeypatch.setattr(workspace_env, "_uv_binary", lambda: "/usr/bin/uv")
    monkeypatch.setattr(workspace_env, "_cache_dir", lambda _: tmp_path / "cache")
    calls = []

    async def run(argv, **kwargs):
        calls.append(argv)
        return 0, ""

    monkeypatch.setattr(workspace_env, "_run", run)
    result = asyncio.run(build_environment(tmp_path, ["pandas"], user_id="u1"))
    assert result["success"]
    assert "--system-site-packages" in calls[0]
    assert calls[1][1:4] == ["-m", "pip", "install"]
    constraint_path = calls[1][calls[1].index("--constraint") + 1]
    from pathlib import Path
    assert set(Path(constraint_path).read_text().splitlines()) == {
        "torch==2.14.0+cu130", "nvidia-cudnn-cu13==9.24.0.43", "cuda-toolkit==13.0.3.0",
    }

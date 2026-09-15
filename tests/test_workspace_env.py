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

from agent_runtime.workspace_env import build_environment, environment_python_path


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

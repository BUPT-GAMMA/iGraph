import asyncio
import os
import subprocess
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.workspace_tools import create_workspace_tools, workspace_sandbox_startup_check


@pytest.fixture(autouse=True)
def local_workspace_environment(monkeypatch):
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")
    original = os.environ.get("IGRAPH_WORKSPACE_ROOT")
    yield
    if original is None:
        os.environ.pop("IGRAPH_WORKSPACE_ROOT", None)
    else:
        os.environ["IGRAPH_WORKSPACE_ROOT"] = original

def ctx(tmp_path, sid="s1", uid="u1"):
    os.environ["IGRAPH_WORKSPACE_ROOT"] = str(tmp_path)
    return SimpleNamespace(user_id=uid, session_id=sid)


def toolset():
    return {tool.__name__: tool for tool in create_workspace_tools()}


def test_workspace_bash_and_read_are_session_scoped(tmp_path):
    c = ctx(tmp_path)
    tools = toolset()
    assert set(tools) == {"workspace_list", "workspace_bash", "workspace_publish", "workspace_env_ensure"}
    result = asyncio.run(tools["workspace_bash"]("printf hello > Ephemeral/result.txt", c))
    assert result["success"]
    read = asyncio.run(tools["workspace_bash"]("cat Ephemeral/result.txt", c))
    assert read["output"] == "hello"
    assert asyncio.run(tools["workspace_list"](c))["files"] == []
    other = ctx(tmp_path, "s2")
    assert not asyncio.run(tools["workspace_bash"]("cat Ephemeral/result.txt", other))["success"]


def test_workspace_creates_isolated_lifecycle_zones(tmp_path):
    first = ctx(tmp_path, "s1")
    second = ctx(tmp_path, "s2", "u2")
    tools = toolset()

    asyncio.run(tools["workspace_bash"]("printf one > Ephemeral/a.txt", first))
    uploaded = tmp_path / "u2" / "s2" / "Uploaded"
    uploaded.mkdir(parents=True)
    (uploaded / "input.txt").write_text("input", encoding="utf-8")

    assert (tmp_path / "u1" / "s1" / "Ephemeral" / "a.txt").read_text() == "one"
    assert (tmp_path / "u2" / "s2" / "Uploaded" / "input.txt").read_text() == "input"
    assert asyncio.run(tools["workspace_bash"]("cat ../s2/Uploaded/input.txt", first))["success"] is False


def test_uploaded_files_are_immutable_to_write_exec_delete_and_rename(tmp_path):
    current = ctx(tmp_path)
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    source = uploaded / "input.txt"
    source.write_text("original", encoding="utf-8")
    tools = toolset()

    assert asyncio.run(tools["workspace_bash"]("printf changed > Uploaded/input.txt", current))["success"] is False
    assert asyncio.run(tools["workspace_bash"]("printf changed > Uploaded/input.txt || true", current))["success"] is False
    assert asyncio.run(tools["workspace_bash"]("rm Uploaded/input.txt", current))["success"] is False
    assert asyncio.run(tools["workspace_bash"]("mv Uploaded/input.txt renamed.txt", current))["success"] is False
    assert source.read_text(encoding="utf-8") == "original"
    assert not (uploaded / "renamed.txt").exists()


def test_workspace_bash_runs_uploaded_python_and_classifies_generated_file_ephemeral(tmp_path):
    current = ctx(tmp_path)
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    (uploaded / "make_file.py").write_text(
        "from pathlib import Path\nPath('generated.txt').write_text('generated')\n",
        encoding="utf-8",
    )
    result = asyncio.run(toolset()["workspace_bash"]("python3 Uploaded/make_file.py", current))

    assert result["success"] is True
    assert (tmp_path / "u1" / "s1" / "Ephemeral" / "generated.txt").read_text() == "generated"
    assert any(item["path"] == "Ephemeral/generated.txt" and item["zone"] == "Ephemeral" for item in result["created_artifacts"])


def test_workspace_bash_returns_exit_code_and_truncated_output(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    monkeypatch.setenv("IGRAPH_WORKSPACE_OUTPUT_LIMIT", "100")
    result = asyncio.run(toolset()["workspace_bash"]("printf '%0120d' 0; exit 7", current))

    assert result["success"] is False
    assert result["status"] == "completed"
    assert result["exit_code"] == 7
    assert result["output_truncated"] is True
    assert len(result["output"]) <= 100


def test_workspace_bash_timeout_is_structured(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    monkeypatch.setenv("IGRAPH_WORKSPACE_EXEC_TIMEOUT_SECONDS", "0.5")
    result = asyncio.run(toolset()["workspace_bash"]("printf partial; python3 -c 'import time; time.sleep(1)'", current))

    assert result["success"] is False
    assert result["status"] == "timeout"
    assert result["exit_code"] is None
    assert result["output_truncated"] is False


def test_workspace_list_hides_ephemeral_and_shows_uploaded_and_derived(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    (uploaded / "input.txt").write_text("input", encoding="utf-8")
    asyncio.run(tools["workspace_bash"]("printf report > Ephemeral/report.txt", current))
    published = asyncio.run(tools["workspace_publish"]("Ephemeral/report.txt", current))
    listing = asyncio.run(tools["workspace_list"](current))
    paths = {item["path"] for item in listing["files"]}

    assert "Uploaded/input.txt" in paths
    assert published["artifact"]["relative_name"] in paths
    assert "Ephemeral/report.txt" not in paths


def test_workspace_publish_only_ephemeral_and_never_overwrites_derived(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    (uploaded / "input.txt").write_text("input")
    assert asyncio.run(tools["workspace_publish"]("Uploaded/input.txt", current))["success"] is False
    asyncio.run(tools["workspace_bash"]("printf one > Ephemeral/report.txt", current))
    first = asyncio.run(tools["workspace_publish"]("Ephemeral/report.txt", current))
    asyncio.run(tools["workspace_bash"]("printf two > Ephemeral/report.txt", current))
    second = asyncio.run(tools["workspace_publish"]("Ephemeral/report.txt", current))

    assert first["success"] and second["success"]
    assert first["artifact_id"] == first["artifact"]["artifact_id"]
    assert first["relative_name"] == first["artifact"]["relative_name"]
    assert first["artifact"]["relative_name"] != second["artifact"]["relative_name"]
    assert first["artifact"]["artifact_id"] != second["artifact"]["artifact_id"]
    for artifact in (first["artifact"], second["artifact"]):
        assert set(("artifact_id", "relative_name", "mime_type", "size", "checksum", "zone", "visibility")) <= artifact.keys()
        assert artifact["zone"] == "Derived"
        assert artifact["visibility"] == "user"
        assert len(artifact["checksum"]) == 64
    assert (tmp_path / "u1" / "s1" / "Derived" / "report.txt").read_text() == "one"


def test_workspace_rejects_symlink_and_workspace_escape(tmp_path):
    current = ctx(tmp_path)
    root = tmp_path / "u1" / "s1"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    tools = toolset()

    assert asyncio.run(tools["workspace_publish"]("link.txt", current))["success"] is False
    assert asyncio.run(tools["workspace_publish"]("../../outside.txt", current))["success"] is False
    escaped = asyncio.run(tools["workspace_bash"]("cat /etc/passwd", current))
    assert escaped["success"] is False


def test_fallback_exec_restores_uploaded_file_after_symlink_replacement(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    source = uploaded / "input.txt"
    source.write_text("original", encoding="utf-8")
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")

    result = asyncio.run(
        toolset()["workspace_bash"](
            "python3 -c 'from pathlib import Path; p=Path(\"Uploaded/input.txt\"); p.unlink(); p.symlink_to(\"other.txt\")'",
            current,
        )
    )

    assert result["success"] is False
    assert source.is_file()
    assert not source.is_symlink()
    assert source.read_text(encoding="utf-8") == "original"


def test_workspace_bash_hides_fallback_server_path(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")

    result = asyncio.run(toolset()["workspace_bash"]("pwd", current))

    assert result["success"] is True
    assert str(tmp_path) not in result["output"]
    assert "/workspace" in result["output"]


def test_production_without_bwrap_never_executes_command(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_ENV", "production")
    marker = tmp_path / "u1" / "s1" / "marker.txt"

    result = asyncio.run(toolset()["workspace_bash"]("printf ran > marker.txt", current))

    assert result["success"] is False
    assert result["sandbox_mode"] == "unavailable"
    assert result["status"] == "sandbox_unavailable"
    assert result["sandbox_unavailable"] is True
    assert result["error"]["code"] == "sandbox_unavailable"
    assert not marker.exists()


def test_auto_mode_without_bwrap_does_not_enable_fallback(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_ENV", "development")
    monkeypatch.delenv("IGRAPH_WORKSPACE_SANDBOX_MODE", raising=False)

    result = asyncio.run(toolset()["workspace_bash"]("printf must-not-run > marker.txt", current))

    assert result["status"] == "sandbox_unavailable"
    assert result["sandbox_mode"] == "unavailable"
    assert not (tmp_path / "u1" / "s1" / "marker.txt").exists()


@pytest.mark.parametrize("environment", ["staging", "test", "ci", "qa"])
def test_required_environments_without_bwrap_are_unavailable_even_with_fallback_setting(
    tmp_path, monkeypatch, environment
):
    current = ctx(tmp_path)
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_ENV", environment)
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")

    result = asyncio.run(toolset()["workspace_bash"]("printf must-not-run > marker.txt", current))

    assert result["status"] == "sandbox_unavailable"
    assert not (tmp_path / "u1" / "s1" / "marker.txt").exists()


def test_runtime_startup_check_reports_missing_bwrap(monkeypatch):
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_ENV", "staging")

    with pytest.raises(RuntimeError, match="bubblewrap executable was not found"):
        workspace_sandbox_startup_check()


@pytest.mark.parametrize("binary", [None, "/usr/bin/bwrap"])
def test_explicitly_disabled_sandbox_starts_but_never_executes(tmp_path, monkeypatch, binary):
    current = ctx(tmp_path)
    monkeypatch.setenv("IGRAPH_ENV", "production")
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "disabled")
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: binary)
    workspace_sandbox_startup_check()
    result = asyncio.run(toolset()["workspace_bash"]("printf forbidden > marker.txt", current))
    assert result["status"] == "sandbox_unavailable"
    assert not (tmp_path / "u1" / "s1" / "marker.txt").exists()


def test_runtime_startup_check_reports_rootless_probe_failure(monkeypatch):
    monkeypatch.setenv("IGRAPH_ENV", "production")
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        "agent_runtime.workspace_tools.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["bwrap"], 1, stdout="", stderr="Operation not permitted"
        ),
    )

    with pytest.raises(RuntimeError, match="rootless bubblewrap probe failed"):
        workspace_sandbox_startup_check()


def test_development_fallback_reports_security_level_and_log_fields(tmp_path, monkeypatch, caplog):
    current = ctx(tmp_path)
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")

    with caplog.at_level("WARNING", logger="igraph"):
        result = asyncio.run(toolset()["workspace_bash"]("printf fallback", current))

    assert result["success"] is True
    assert result["sandbox_mode"] == "fallback"
    assert result["sandbox_security_level"] == "development-compatibility"
    assert result["sandbox_warning"]
    assert "sandbox_mode=fallback" in caplog.text


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap sandbox integration")
def test_uploaded_python_cannot_access_external_path_in_bwrap(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "bwrap")
    current = ctx(tmp_path)
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    (uploaded / "escape.py").write_text(
        "from pathlib import Path\nPath('/tmp/igraph-workspace-escape').write_text('escaped')\n",
        encoding="utf-8",
    )

    result = asyncio.run(toolset()["workspace_bash"]("python3 Uploaded/escape.py", current))

    assert result["sandbox_mode"] == "bwrap"
    assert result["success"] is False
    assert not Path("/tmp/igraph-workspace-escape").exists()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap sandbox integration")
@pytest.mark.parametrize("termination", ["timeout", "cancel", "exit"])
def test_bwrap_termination_stops_children(tmp_path, monkeypatch, termination):
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "bwrap")
    monkeypatch.setenv("IGRAPH_WORKSPACE_EXEC_TIMEOUT_SECONDS", "0.3" if termination == "timeout" else "5")
    current = ctx(tmp_path)
    from agent_runtime.workspace_tools import workspace_root

    root = workspace_root(current)
    (root / "Uploaded/probe.py").write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "subprocess.Popen([sys.executable, '-c', "
        "\"import time; from pathlib import Path; time.sleep(1); Path('Ephemeral/orphan').touch()\"], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "Path('Ephemeral/started').touch()\n"
        + ("time.sleep(2)\n" if termination != "exit" else ""),
        encoding="utf-8",
    )

    async def exercise():
        task = asyncio.create_task(toolset()["workspace_bash"]("python3 Uploaded/probe.py", current))
        for _ in range(100):
            if (root / "Ephemeral/started").exists() or task.done():
                break
            await asyncio.sleep(0.01)
        assert (root / "Ephemeral/started").exists(), "sandbox must execute the probe"
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["status"] == ("timeout" if termination == "timeout" else "completed")
            if termination == "exit":
                assert result["success"]
        await asyncio.sleep(1.2)
        assert not (root / "Ephemeral/orphan").exists()

    asyncio.run(exercise())


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Linux bubblewrap sandbox integration")
@pytest.mark.parametrize("escape", ["os.setsid()", "os.setpgid(0, 0)"])
def test_bwrap_blocks_process_group_escape(tmp_path, monkeypatch, escape):
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "bwrap")
    current = ctx(tmp_path)
    from agent_runtime.workspace_tools import workspace_root

    root = workspace_root(current)
    (root / "Uploaded/probe.py").write_text(
        "import subprocess, sys\n"
        f"result = subprocess.run([sys.executable, '-c', 'import os; {escape}'])\n"
        "assert result.returncode != 0, 'child escaped the process group'\n",
        encoding="utf-8",
    )
    result = asyncio.run(toolset()["workspace_bash"]("python3 Uploaded/probe.py", current))
    assert result["success"], result
    assert "Operation not permitted" in result["output"]


def test_fallback_exception_restores_uploaded_and_derived(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    root = tmp_path / "u1" / "s1"
    uploaded = root / "Uploaded"
    uploaded.mkdir(parents=True)
    source = uploaded / "input.txt"
    source.write_text("original", encoding="utf-8")
    (root / "Derived").mkdir(parents=True)
    derived = root / "Derived" / "result.txt"
    derived.write_text("published", encoding="utf-8")
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")

    result = asyncio.run(
        toolset()["workspace_bash"](
            "python3 -c 'from pathlib import Path; Path(\"Uploaded/input.txt\").write_text(\"bad\"); Path(\"Derived/result.txt\").write_text(\"bad\"); raise SystemExit(3)'",
            current,
        )
    )

    assert result["success"] is False
    assert source.read_text(encoding="utf-8") == "original"
    assert derived.read_text(encoding="utf-8") == "published"


def test_fallback_timeout_restores_uploaded_file(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    source = uploaded / "input.txt"
    source.write_text("original", encoding="utf-8")
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")
    monkeypatch.setenv("IGRAPH_WORKSPACE_EXEC_TIMEOUT_SECONDS", "0.2")

    result = asyncio.run(
        toolset()["workspace_bash"](
            "python3 -c 'from pathlib import Path; import time; Path(\"Uploaded/input.txt\").write_text(\"bad\"); time.sleep(2)'",
            current,
        )
    )

    assert result["status"] == "timeout"
    assert source.read_text(encoding="utf-8") == "original"


def test_cancelled_exec_restores_uploaded_file(tmp_path, monkeypatch):
    current = ctx(tmp_path)
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    source = uploaded / "input.txt"
    source.write_text("original", encoding="utf-8")
    monkeypatch.setattr("agent_runtime.workspace_tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")
    monkeypatch.setenv("IGRAPH_WORKSPACE_EXEC_TIMEOUT_SECONDS", "5")

    async def run_and_cancel():
        task = asyncio.create_task(
            toolset()["workspace_bash"](
                "python3 -c 'from pathlib import Path; import time; Path(\"Uploaded/input.txt\").write_text(\"bad\"); time.sleep(2)'",
                current,
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("workspace_bash cancellation was swallowed")

    asyncio.run(run_and_cancel())
    assert source.read_text(encoding="utf-8") == "original"


def test_workspace_identity_symlink_is_rejected(tmp_path):
    base = tmp_path / "workspaces"
    outside = tmp_path / "outside"
    outside.mkdir()
    base.mkdir()
    (base / "u1").symlink_to(outside, target_is_directory=True)
    os.environ["IGRAPH_WORKSPACE_ROOT"] = str(base)

    identity = SimpleNamespace(user_id="u1", session_id="s1")
    result = asyncio.run(toolset()["workspace_publish"]("Ephemeral/file.txt", identity))["success"]
    listed = asyncio.run(toolset()["workspace_list"](identity))
    executed = asyncio.run(toolset()["workspace_bash"]("pwd", identity))

    assert result is False
    assert listed["success"] is False
    assert executed["success"] is False
    assert not (outside / "s1").exists()


def test_workspace_zone_symlink_is_rejected(tmp_path):
    current = ctx(tmp_path)
    root = tmp_path / "u1" / "s1"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Ephemeral").symlink_to(outside, target_is_directory=True)

    result = asyncio.run(toolset()["workspace_bash"]("printf x > Ephemeral/out.txt", current))

    assert result["success"] is False
    assert not (outside / "out.txt").exists()
    assert not (outside / "Uploaded").exists()


def test_workspace_publish_rejects_derived_symlink_destination(tmp_path):
    current = ctx(tmp_path)
    root = tmp_path / "u1" / "s1"
    (root / "Ephemeral").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Derived").symlink_to(outside, target_is_directory=True)
    (root / "Ephemeral" / "report.txt").write_text("report", encoding="utf-8")

    result = asyncio.run(toolset()["workspace_publish"]("Ephemeral/report.txt", current))

    assert result["success"] is False
    assert not (outside / "report.txt").exists()


def test_workspace_bash_writes_literal_multiline_content(tmp_path):
    c = ctx(tmp_path)
    content = 'name,value\n"quoted",$HOME\n`pwd`,42\n'
    command = "cat <<'DATA' > Ephemeral/data.csv\n" + content + "DATA"
    result = asyncio.run(toolset()["workspace_bash"](command, c))
    assert result["success"]
    assert (tmp_path / "u1" / "s1" / "Ephemeral" / "data.csv").read_text() == content
    assert result["created_artifacts"][0]["visibility"] == "hidden"


def test_workspace_rejects_path_escape(tmp_path):
    c = ctx(tmp_path)
    result = asyncio.run(toolset()["workspace_bash"]("cat ../../secret", c))
    assert result["success"] is False


def test_workspace_identity_is_a_single_safe_directory(tmp_path):
    c = ctx(tmp_path, "../../session")
    result = asyncio.run(toolset()["workspace_bash"]("pwd", c))
    assert result["success"]
    assert ".." not in result["output"]


def test_workspace_bash_read_exposes_output_truncation(tmp_path, monkeypatch):
    c = ctx(tmp_path)
    monkeypatch.setenv("IGRAPH_WORKSPACE_OUTPUT_LIMIT", "100")
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    (uploaded / "large.txt").write_text("x" * 200_001)
    result = asyncio.run(toolset()["workspace_bash"]("cat Uploaded/large.txt", c))
    assert result["success"] is True
    assert result["output_truncated"] is True
    assert len(result["output"]) == 100


def test_workspace_bash_reads_progressive_chunks(tmp_path):
    c = ctx(tmp_path)
    uploaded = tmp_path / "u1" / "s1" / "Uploaded"
    uploaded.mkdir(parents=True)
    (uploaded / "chunks.txt").write_text("abc\ndef\n")
    bash = toolset()["workspace_bash"]
    first = asyncio.run(bash("sed -n '1p' Uploaded/chunks.txt", c))
    second = asyncio.run(bash("sed -n '2p' Uploaded/chunks.txt", c))
    assert first["output"] == "abc\n"
    assert second["output"] == "def\n"
    assert not first["output_truncated"] and not second["output_truncated"]


def test_environment_zone_exists_and_survives_bash(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    # 分区目录随工作区初始化一并建出
    asyncio.run(tools["workspace_bash"]("printf x > Ephemeral/probe.txt", current))
    assert (root / "Environment").is_dir()

    # 放在 Environment 下的文件不得被 _relocate_new_root_files 搬进 Ephemeral
    (root / "Environment" / "venv").mkdir(parents=True)
    (root / "Environment" / "venv" / "marker.txt").write_text("kept", encoding="utf-8")
    asyncio.run(tools["workspace_bash"]("printf y > Ephemeral/again.txt", current))
    assert (root / "Environment" / "venv" / "marker.txt").read_text() == "kept"
    assert not (root / "Ephemeral" / "Environment").exists()


def test_iter_files_skips_environment_subtree(tmp_path):
    from agent_runtime.workspace_tools import _iter_files

    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    asyncio.run(tools["workspace_bash"]("printf x > Ephemeral/probe.txt", current))

    venv = root / "Environment" / "venv" / "lib"
    venv.mkdir(parents=True)
    for index in range(50):
        (venv / f"module{index}.py").write_text(f"# {index}", encoding="utf-8")

    keys = _iter_files(root)
    assert "Ephemeral/probe.txt" in keys
    assert not any(key.startswith("Environment/") for key in keys)


def test_environment_files_are_not_reported_as_artifacts(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    (root / "Environment" / "venv").mkdir(parents=True, exist_ok=True)
    # 由服务端（而非 agent 命令）放入的 venv 文件
    (root / "Environment" / "venv" / "site.py").write_text("pkg", encoding="utf-8")

    # 与 Environment 无关的命令，其变更产物中不得出现 Environment 下的文件
    result = asyncio.run(tools["workspace_bash"]("printf ok > Ephemeral/out.txt", current))
    assert result["success"] is True
    paths = [item["path"] for item in result["artifacts"]]
    assert not any(path.startswith("Environment/") for path in paths)


def test_bash_cannot_write_into_environment(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    (root / "Environment" / "venv").mkdir(parents=True, exist_ok=True)

    # Environment 属受保护分区：写入命令应被拒绝，且不留下文件
    result = asyncio.run(tools["workspace_bash"](
        "printf pkg > Environment/venv/site.py", current))
    assert result["success"] is False
    assert not (root / "Environment" / "venv" / "site.py").exists()


def test_sandbox_command_preserves_process_group_and_mounts_environment(tmp_path, monkeypatch):
    from agent_runtime.workspace_tools import _sandbox_command, _ensure_layout

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    root = tmp_path / "ws"
    _ensure_layout(root)
    argv, _env = _sandbox_command(root, "echo hi")

    assert "--unshare-all" not in argv
    assert "--unshare-pid" not in argv
    assert "--new-session" not in argv
    for flag in ("--unshare-user", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup", "--unshare-net"):
        assert flag in argv, flag
    assert "--proc" in argv

    joined = " ".join(argv)
    assert f"--ro-bind {root / 'Environment'} /workspace/Environment" in joined
    # 网络仍然隔离：不得出现 share-net
    assert "--share-net" not in argv


def test_sandbox_exposes_only_granted_nvidia_character_devices(tmp_path, monkeypatch):
    from agent_runtime.workspace_tools import _sandbox_command, _ensure_layout

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    real_glob = Path.glob

    class Device:
        def __init__(self, path, character):
            self.path, self.character = path, character

        def __str__(self):
            return self.path

        def __lt__(self, other):
            return self.path < other.path

        def is_char_device(self):
            return self.character

    def glob(path, pattern):
        if str(path) == "/dev" and pattern == "nvidia*":
            return iter([Device("/dev/nvidia0", True), Device("/dev/nvidiactl", True),
                         Device("/dev/nvidia-uvm", True), Device("/dev/nvidia-caps", False)])
        return real_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", glob)
    root = tmp_path / "ws"
    _ensure_layout(root)
    argv, _ = _sandbox_command(root, "true")
    devices = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == "--dev-bind"]
    assert devices == [[value, value] for value in ("/dev/nvidia-uvm", "/dev/nvidia0", "/dev/nvidiactl")]
    assert "--unshare-net" in argv
    assert ["/dev", "/dev"] not in devices


def test_env_ensure_rejects_non_pypi_sources(tmp_path):
    current = ctx(tmp_path)
    result = asyncio.run(toolset()["workspace_env_ensure"](["git+https://evil.example/x"], current))
    assert result["success"] is False
    assert "PyPI" in result["error"]


def test_env_ensure_reports_fallback_security_level(tmp_path, monkeypatch):
    # fallback 下 Environment 的只读保证不成立，必须在返回值里说明
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    current = ctx(tmp_path)
    result = asyncio.run(toolset()["workspace_env_ensure"](["requests"], current))
    assert result["sandbox_mode"] == "fallback"
    assert result["sandbox_security_level"] == "development-compatibility"


def test_env_ensure_surfaces_missing_uv(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    current = ctx(tmp_path)
    result = asyncio.run(toolset()["workspace_env_ensure"](["requests"], current))
    assert result["success"] is False
    assert "uv" in result["error"]


def test_env_ensure_requires_session_identity(tmp_path):
    os.environ["IGRAPH_WORKSPACE_ROOT"] = str(tmp_path)
    broken = SimpleNamespace(user_id="u1", session_id="")
    result = asyncio.run(toolset()["workspace_env_ensure"](["requests"], broken))
    assert result["success"] is False


def test_environment_is_hidden_from_listings(tmp_path):
    from workspace_storage import visible_files

    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    asyncio.run(tools["workspace_bash"]("printf x > Ephemeral/probe.txt", current))
    (root / "Environment" / "venv" / "bin").mkdir(parents=True)
    (root / "Environment" / "venv" / "bin" / "python").write_text("#!", encoding="utf-8")
    (root / "Uploaded").mkdir(exist_ok=True)
    (root / "Uploaded" / "data.csv").write_text("a,b", encoding="utf-8")

    listed = asyncio.run(tools["workspace_list"](current))["files"]
    assert [item["path"] for item in listed] == ["Uploaded/data.csv"]
    assert not any(item["path"].startswith("Environment/") for item in visible_files("u1", "s1"))


def test_environment_cannot_be_modified_by_bash_in_fallback(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    (root / "Environment" / "venv").mkdir(parents=True)
    target = root / "Environment" / "venv" / "sitecustomize.py"
    target.write_text("original", encoding="utf-8")

    asyncio.run(tools["workspace_bash"]("printf tampered > Environment/venv/sitecustomize.py", current))
    # bwrap 下由 --ro-bind 保证；fallback 下至少不得静默成功地留下篡改
    assert target.read_text(encoding="utf-8") == "original"


def test_tool_descriptions_mention_environment_workflow():
    from agent_runtime.instructions import instructions_for

    tools = toolset()
    bash_doc = tools["workspace_bash"].__doc__ or ""
    assert "Environment" in bash_doc
    assert "workspace_env_ensure" in bash_doc

    text = " ".join(instructions_for(reasoning_mode="direct", planning_mode="auto"))
    assert "Environment" in text
    assert "workspace_env_ensure" in text


def test_tool_descriptions_explain_shared_cuda_runtime():
    from agent_runtime.instructions import instructions_for

    bash_doc = toolset()["workspace_bash"].__doc__ or ""
    text = " ".join(instructions_for(reasoning_mode="direct", planning_mode="auto"))
    assert "CUDA" in bash_doc
    assert "GPU" in bash_doc
    assert "shared" in bash_doc
    assert "CUDA" in text
    assert "GPU" in text
    assert "共享" in text


def test_mask_allows_dev_null_and_reads_from_protected_zones():
    from agent_runtime.workspace_tools import _command_rejects_workspace_mutation as masked

    assert masked("python3 Ephemeral/job.py > /dev/null 2>&1") is False
    assert masked("cat Uploaded/input.csv | head -5") is False
    assert masked("cp Uploaded/a.csv Ephemeral/") is False


def test_mask_rejects_protected_writes_destinations_and_nested_shells():
    from agent_runtime.workspace_tools import _command_rejects_workspace_mutation as masked

    assert masked("cp Ephemeral/a.csv Uploaded/") is True
    assert masked("printf x | tee Uploaded/a.csv") is True
    assert masked("dd if=Ephemeral/a of=Environment/x") is True
    assert masked("ln -s Ephemeral/a Uploaded/link") is True
    assert masked("rm Derived/report.md") is True
    assert masked("printf x > Derived/report.md") is True
    assert masked("HOME=Ephemeral rm Uploaded/x") is True
    assert masked("bash -c 'rm Uploaded/x'") is True
    assert masked("echo ok && cp x Uploaded/") is True
    assert masked("printf 'unclosed") is True


def test_fallback_exec_warns_on_smuggled_protected_file(tmp_path, caplog):
    # The mask cannot see filesystem writes made from inside an interpreter.
    # Fallback mode must fail the run and leave an audit trail for the file it
    # deliberately did not remove (concurrent publish/upload keeps priority).
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    (root / "Ephemeral").mkdir(parents=True, exist_ok=True)
    (root / "Ephemeral" / "smuggle.py").write_text(
        "from pathlib import Path\nPath('Uploaded/contraband.txt').write_text('planted')\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="igraph"):
        result = asyncio.run(tools["workspace_bash"]("python3 Ephemeral/smuggle.py", current))

    assert result["success"] is False
    assert result["error"] == "不能修改 Uploaded 或 Derived 文件"
    assert "protected_zone_new_files" in caplog.text

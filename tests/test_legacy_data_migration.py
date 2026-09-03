from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from agent_runtime.migrations import legacy_data
from agent_runtime.migrations.legacy_data import Migrator


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _seed_legacy_user(data_root: Path) -> Path:
    user_root = data_root / "users" / "alice"
    session_dir = user_root / "sessions" / "session-1"
    _write_json(
        session_dir / "session_meta.json",
        {"title": "Imported", "model": "legacy-model", "last_has_graph": True},
    )
    session_dir.joinpath("message_list.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"visible": True, "message": {"role": "user", "content": "hello"}, "ts": 1}),
                json.dumps({"visible": True, "message": {"role": "assistant", "content": "raw"}, "display_content": "shown", "ts": 2}),
                json.dumps({"visible": False, "message": {"role": "user", "content": "hidden"}}),
                json.dumps({"visible": True, "kind": "user_feedback", "message": {"role": "user"}}),
                json.dumps({"visible": True, "message": {"role": "tool", "content": "ignored"}}),
                "not-json",
            ]
        ),
        encoding="utf-8",
    )

    _write_json(user_root / "memory" / "profile.json", {"items": [{"key": "language", "value": "zh"}]})
    _write_json(
        user_root / "memory" / "facts.json",
        [{"key": "preferences", "scope": "session", "content": {"theme": "dark"}}],
    )
    _write_json(
        user_root / "playbooks.json",
        [{"problem_type": "community", "approach": "Use Leiden", "lifecycle_status": "validated"}],
    )

    approved = user_root / "skills" / "Graph_Helper"
    approved.mkdir(parents=True)
    approved.joinpath("SKILL.md").write_text(
        "---\nname: Graph_Helper\ndescription: Approved\n---\n\nBody",
        encoding="utf-8",
    )
    candidate = user_root / "skills" / ".review_queue" / "Candidate Skill"
    candidate.mkdir(parents=True)
    candidate.joinpath("SKILL.md").write_text(
        "---\nname: Candidate Skill\ndescription: Candidate\n---\n\nBody",
        encoding="utf-8",
    )
    return user_root


def test_migrator_imports_all_legacy_types_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    _seed_legacy_user(data_root)
    requests: list[tuple[str, str, str, dict]] = []

    migrator = Migrator(data_root=data_root, runtime_url="http://runtime/", dry_run=False)

    def fake_request(method: str, path: str, *, user_id: str, **kwargs):
        requests.append((method, path, user_id, kwargs))
        return {"success": True}

    monkeypatch.setattr(migrator, "_request", fake_request)
    report = migrator.run()

    assert report == {"sessions": 1, "memories": 3, "skills": 2, "skipped": 0, "errors": []}
    assert requests[0][:3] == ("POST", "/sessions", "alice")
    session_payload = requests[0][3]["json"]
    assert session_payload["session_name"] == "Imported"
    assert session_payload["session_state"]["legacy_messages"] == [
        {"role": "user", "content": "hello", "ts": 1},
        {"role": "assistant", "content": "shown", "ts": 2},
    ]
    memory_requests = [row for row in requests if row[1] == "/memories"]
    assert len(memory_requests) == 3
    assert any(row[3]["json"]["memory"] == '{"theme":"dark"}' for row in memory_requests)

    approved = data_root / "agent_runtime" / "skills" / "users" / "alice" / "graph-helper" / "SKILL.md"
    candidate = data_root / "agent_runtime" / "skills" / "candidates" / "alice" / "candidate-skill" / "SKILL.md"
    assert "name: graph-helper" in approved.read_text(encoding="utf-8")
    assert "name: candidate-skill" in candidate.read_text(encoding="utf-8")
    manifest = json.loads(migrator.manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert len(manifest["items"]) == 6

    rerun = Migrator(data_root=data_root, runtime_url="http://runtime", dry_run=False)

    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("idempotent migration must not call Agent Runtime again")

    monkeypatch.setattr(rerun, "_request", unexpected_request)
    assert rerun.run() == {
        "sessions": 0,
        "memories": 0,
        "skills": 0,
        "skipped": 6,
        "errors": [],
    }


def test_dry_run_reports_work_without_writing_or_requesting(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed_legacy_user(data_root)
    migrator = Migrator(data_root=data_root, runtime_url="http://runtime", dry_run=True)

    report = migrator.run()

    assert report["sessions"] == 1
    assert report["memories"] == 3
    assert report["skills"] == 2
    assert not migrator.manifest_path.exists()
    assert not (data_root / "agent_runtime" / "skills").exists()
    assert migrator._request("POST", "/ignored", user_id="alice") == {}


def test_migration_helpers_tolerate_corrupt_and_missing_input(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert legacy_data._read_json(missing, {"fallback": True}) == {"fallback": True}

    broken = tmp_path / "broken.json"
    broken.write_text("{broken", encoding="utf-8")
    assert legacy_data._read_json(broken, []) == []

    rows = tmp_path / "rows.jsonl"
    rows.write_text('{"ok": 1}\n[1, 2]\ninvalid\n{"ok": 2}', encoding="utf-8")
    assert legacy_data._read_jsonl(rows) == [{"ok": 1}, {"ok": 2}]
    assert legacy_data._read_jsonl(tmp_path / "absent.jsonl") == []

    assert legacy_data._skill_name("  Graph__Helper  ") == "graph-helper"
    assert legacy_data._skill_name("中文") == "migrated-skill"
    first = legacy_data._checksum(broken)
    broken.write_text("changed", encoding="utf-8")
    assert first != legacy_data._checksum(broken)
    assert legacy_data._checksum(tmp_path) == legacy_data._checksum(tmp_path)


def test_migrator_records_request_errors_and_leaves_item_unmarked(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    user_root = data_root / "users" / "alice"
    _write_json(user_root / "sessions" / "broken-session" / "session_meta.json", {})
    _write_json(user_root / "memory" / "facts.json", [{"value": "remember"}])

    migrator = Migrator(data_root=data_root, runtime_url="http://runtime", dry_run=False)

    def fail_request(*_args, **_kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(migrator, "_request", fail_request)
    report = migrator.run()

    assert report["sessions"] == 0
    assert report["memories"] == 0
    assert len(report["errors"]) == 2
    assert all(item["error"] == "runtime unavailable" for item in report["errors"])
    assert migrator.manifest["items"] == {}


def test_main_prints_json_report_for_empty_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["legacy-data", "--data-root", str(tmp_path), "--runtime-url", "http://runtime", "--dry-run"],
    )

    legacy_data.main()

    report = json.loads(capsys.readouterr().out)
    assert report == {"sessions": 0, "memories": 0, "skills": 0, "skipped": 0, "errors": []}


def test_memory_manifest_hash_is_based_on_rendered_content(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    user_root = data_root / "users" / "alice"
    _write_json(user_root / "memory" / "facts.json", [{"value": "remember"}])
    digest = hashlib.sha256(b"remember").hexdigest()
    key = f"memory:alice:{user_root / 'memory' / 'facts.json'}:0"
    manifest_path = data_root / "agent_runtime" / "migration-manifest.json"
    _write_json(manifest_path, {"version": 1, "items": {key: digest}})

    migrator = Migrator(data_root=data_root, runtime_url="http://runtime", dry_run=False)
    report = migrator.run()

    assert report["skipped"] == 1
    assert report["memories"] == 0

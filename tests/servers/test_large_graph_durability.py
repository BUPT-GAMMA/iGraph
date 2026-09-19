from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import graph_data_service.large_graph.durability as durability


def test_durable_publish_syncs_tree_and_parent_around_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / ".snap_test.staging"
    final = tmp_path / "snap_test"
    staging.mkdir()
    (staging / "manifest.json").write_text("{}", encoding="utf-8")
    events: list[tuple[str, Path]] = []

    def sync_tree(path: str | Path) -> None:
        events.append(("tree", Path(path)))

    def sync_directory(path: str | Path) -> None:
        events.append(("directory", Path(path)))

    def replace(source: str | Path, target: str | Path) -> None:
        events.append(("replace", Path(source)))
        os.rename(source, target)

    monkeypatch.setattr(durability, "fsync_publish_tree", sync_tree)
    monkeypatch.setattr(durability, "fsync_directory", sync_directory)
    monkeypatch.setattr(durability.os, "replace", replace)

    durability.durable_publish_tree(staging, final)

    assert events == [
        ("tree", staging),
        ("directory", tmp_path),
        ("replace", staging),
        ("directory", tmp_path),
    ]
    assert final.is_dir()
    assert not staging.exists()


def test_fsync_publish_tree_streams_regular_files_and_rejects_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / ".artifact.staging"
    nested = staging / "results"
    nested.mkdir(parents=True)
    result_file = nested / "part-00000.jsonl"
    result_file.write_text('{"node":"A"}\n', encoding="utf-8")
    manifest_file = staging / "manifest.json"
    manifest_file.write_text("{}", encoding="utf-8")

    synced_files: list[Path] = []
    synced_directories: list[Path] = []
    monkeypatch.setattr(
        durability, "_fsync_regular_file", lambda path: synced_files.append(Path(path))
    )
    monkeypatch.setattr(
        durability, "fsync_directory", lambda path: synced_directories.append(Path(path))
    )

    durability.fsync_publish_tree(staging)

    assert set(synced_files) == {manifest_file, result_file}
    assert set(synced_directories) == {staging, nested}

    linked_staging = tmp_path / ".linked.staging"
    linked_staging.mkdir()
    (linked_staging / "escape").symlink_to(result_file)
    synced_files.clear()
    with pytest.raises(ValueError, match="符号链接"):
        durability.fsync_publish_tree(linked_staging)
    assert synced_files == []


def test_private_tree_is_removed_before_parent_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / ".snap.staging" / "work"
    private.mkdir(parents=True)
    (private / "state.sqlite3").write_bytes(b"private")
    observations: list[tuple[Path, bool]] = []

    monkeypatch.setattr(
        durability,
        "fsync_directory",
        lambda path: observations.append((Path(path), private.exists())),
    )

    durability.remove_tree_and_fsync(private)

    assert observations == [(private.parent, False)]


def test_private_tree_cleanup_unlinks_a_symlink_without_touching_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    payload = target / "keep.txt"
    payload.write_text("keep", encoding="utf-8")
    private = tmp_path / ".snap.staging"
    private.symlink_to(target, target_is_directory=True)
    synced: list[Path] = []
    monkeypatch.setattr(
        durability,
        "fsync_directory",
        lambda path: synced.append(Path(path)),
    )

    durability.remove_tree_and_fsync(private)

    assert not os.path.lexists(private)
    assert payload.read_text(encoding="utf-8") == "keep"
    assert synced == [tmp_path]


def test_directory_sync_propagates_real_io_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()

    monkeypatch.setattr(
        durability.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(
            OSError(errno.EIO, "injected sync failure")
        ),
    )
    with pytest.raises(OSError) as caught:
        durability.fsync_directory(directory)
    assert caught.value.errno == errno.EIO


def test_directory_sync_ignores_verified_platform_capability_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    monkeypatch.setattr(
        durability.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(
            OSError(errno.EBADF, "directory fsync unsupported")
        ),
    )

    durability.fsync_directory(directory)


def test_durable_publish_never_overwrites_an_immutable_object(tmp_path: Path) -> None:
    staging = tmp_path / ".snap_test.staging"
    final = tmp_path / "snap_test"
    staging.mkdir()
    final.mkdir()

    with pytest.raises(FileExistsError, match="不可覆盖"):
        durability.durable_publish_tree(staging, final)

    assert staging.is_dir()
    assert final.is_dir()


def test_durable_publish_syncs_every_managed_ancestor_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_root = tmp_path / "large-graph"
    managed_root.mkdir()
    graph_root = (
        managed_root
        / "snapshots"
        / "scope__lease"
        / "graph"
        / "v00000001"
    )
    staging = graph_root / ".snap_test.staging"
    final = graph_root / "snap_test"
    staging.mkdir(parents=True)
    (staging / "manifest.json").write_text("{}", encoding="utf-8")
    events: list[tuple[str, Path]] = []

    monkeypatch.setattr(
        durability,
        "fsync_publish_tree",
        lambda path: events.append(("tree", Path(path))),
    )
    monkeypatch.setattr(
        durability,
        "fsync_directory",
        lambda path: events.append(("directory", Path(path))),
    )

    def replace(source: str | Path, target: str | Path) -> None:
        events.append(("replace", Path(source)))
        os.rename(source, target)

    monkeypatch.setattr(durability.os, "replace", replace)

    durability.durable_publish_tree(
        staging,
        final,
        durability_root=managed_root,
    )

    expected_ancestors = []
    cursor = graph_root
    while True:
        expected_ancestors.append(("directory", cursor))
        if cursor == managed_root:
            break
        cursor = cursor.parent
    assert events == [
        ("tree", staging),
        *expected_ancestors,
        ("replace", staging),
        ("directory", graph_root),
    ]


def test_directory_chain_rejects_a_non_ancestor_boundary(tmp_path: Path) -> None:
    directory = tmp_path / "managed" / "scope"
    directory.mkdir(parents=True)

    with pytest.raises(ValueError, match="祖先"):
        durability.fsync_directory_chain(
            directory,
            through=tmp_path / "other-root",
        )

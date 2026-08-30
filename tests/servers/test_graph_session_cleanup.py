from __future__ import annotations

from pathlib import Path

import numpy as np

from graph_data_service.graph_data import GraphData
from graph_data_service.session import GraphSession


def test_delete_storage_removes_session_files_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    session = GraphSession("conversation-a", user_id="alice")
    session.set_graph(
        GraphData(
            num_nodes=2,
            edge_index=np.asarray([[0], [1]], dtype=np.int64),
            id2node={0: "A", 1: "B"},
            node2id={"A": 0, "B": 1},
        )
    )
    upload_dir = tmp_path / "users" / "alice" / "uploads" / "conversation-a"
    upload_dir.mkdir(parents=True)
    (upload_dir / "attachment.txt").write_text("A B", encoding="utf-8")
    project_source = tmp_path / "projects" / "project-a" / "datasets" / "dataset-a" / "source.txt"
    project_source.parent.mkdir(parents=True)
    project_source.write_text("A B", encoding="utf-8")

    runtime_dir = tmp_path / "users" / "alice" / "graph_sessions" / "conversation-a"
    assert (runtime_dir / "graph_current.npz").is_file()

    session.delete_storage()

    assert not runtime_dir.exists()
    assert not upload_dir.exists()
    assert project_source.is_file()

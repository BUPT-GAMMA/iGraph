from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

from graph_data_service.app import GraphDataService, _detect_text_format_verbose
from graph_data_service.large_graph.preflight import (
    probe_binary_archive,
    probe_structured_source,
)


def _close(service: GraphDataService) -> None:
    service._job_manager.cancel_all()
    service._process_pool.shutdown(wait=True, cancel_futures=True)
    service._large_graph.close()
    service._nebula.close()


def _service(tmp_path: Path, monkeypatch) -> GraphDataService:
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("GRAPH_DATA_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GRAPH_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LARGE_GRAPH_EMBEDDED_WORKER", "false")
    return GraphDataService()


def test_probe_routes_high_cardinality_edgelist_before_edge_threshold(
    tmp_path: Path,
) -> None:
    source = tmp_path / "compact.edgelist"
    source.write_text("A B\nC D\nE F\nG H\n", encoding="utf-8")

    probe = probe_structured_source(
        source,
        input_format="edgelist",
        edge_threshold=100,
        node_threshold=8,
    )

    assert probe.node_threshold_reached is True
    assert probe.edge_threshold_reached is False
    assert probe.estimated_nodes >= 7
    assert probe.rows_scanned == 4


def test_format_detection_accepts_chinese_structured_node_names() -> None:
    edge_format, _ = _detect_text_format_verbose(
        "张三 李四\n李四 王五\n王五 赵六\n"
    )
    triple_format, _ = _detect_text_format_verbose(
        "张三 认识 李四\n李四 就职于 某公司\n王五 认识 赵六\n"
    )
    prose_format, _ = _detect_text_format_verbose(
        "这是一段自然语言，描述人物之间的关系。\n"
        "第二句话仍然没有结构化字段。\n"
    )

    assert edge_format == "edgelist"
    assert triple_format == "triples"
    assert prose_format == "freetext"


def test_probe_does_not_count_triple_relations_as_nodes(tmp_path: Path) -> None:
    source = tmp_path / "relations.csv"
    source.write_text(
        "source,relation,target\n"
        "A,relation-1,B\n"
        "A,relation-2,B\n"
        "A,relation-3,B\n",
        encoding="utf-8",
    )

    probe = probe_structured_source(
        source,
        input_format="triples",
        edge_threshold=100,
        node_threshold=5,
    )

    assert probe.node_threshold_reached is False
    assert probe.estimated_nodes == 2
    assert probe.rows_scanned == 4


def test_preflight_skips_oversized_record_with_bounded_reader(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_RECORD_BYTES", "1024")
    source = tmp_path / "oversized-then-tab.edgelist"
    source.write_text(
        "A " + "x" * 4096 + "\n" + "C\tD\t2\n",
        encoding="utf-8",
    )

    probe = probe_structured_source(
        source,
        input_format="edgelist",
        edge_threshold=2,
        node_threshold=100,
    )

    assert probe.delimiter == "tab"
    assert probe.rows_scanned == 2
    assert probe.edge_threshold_reached is True
    assert probe.estimated_nodes == 2


def test_delimiter_probe_skips_many_comments_and_oversized_records(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_RECORD_BYTES", "1024")
    source = tmp_path / "long-preamble.csv"
    source.write_text(
        "".join(f"# comment {index}\r\n" for index in range(40))
        + "x" * 2048
        + "\r\n甲,乙,1",
        encoding="utf-8",
    )

    probe = probe_structured_source(
        source,
        input_format="edgelist",
        edge_threshold=100,
        node_threshold=100,
    )

    assert probe.delimiter == "comma"
    assert probe.rows_scanned == 2
    assert probe.estimated_nodes == 2


def test_upload_auto_routes_on_streamed_node_estimate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_UPLOAD_BYTES_THRESHOLD", str(1024 * 1024))
    monkeypatch.setenv("LARGE_GRAPH_EDGE_THRESHOLD", "100")
    monkeypatch.setenv("LARGE_GRAPH_NODE_THRESHOLD", "8")
    service = _service(tmp_path, monkeypatch)
    try:
        source = tmp_path / "nodes.edgelist"
        source.write_text("A B\nC D\nE F\nG H\n", encoding="utf-8")
        session = service.get_or_create("s1", user_id="alice")
        session.register_upload("token", source.name, str(source))
        monkeypatch.setattr(
            service._large_graph,
            "submit_preparation",
            lambda **kwargs: {
                "success": True,
                "async": True,
                "job_id": "prepare-1",
                "graph_version": kwargs["graph_version"],
            },
        )

        result = asyncio.run(
            service.do_load_graph_from_upload(
                "s1", source.name, append=False, user_id="alice"
            )
        )

        assert result["success"] is True
        assert result["route_decision"]["path"] == "large_graph"
        assert "节点基数" in result["route_decision"]["reason"]
        assert result["route_decision"]["estimated_nodes"] >= 7
        assert result["route_decision"]["structured_rows_scanned"] == 4
        assert session.get_graph() is None
    finally:
        _close(service)


def test_compressed_binary_expansion_is_rejected_before_graphdata_load(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "compressed.npz"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"\0" * (200 * 1024))
    assert source.stat().st_size < 10 * 1024
    probe = probe_binary_archive(source)
    assert probe.is_archive is True
    assert probe.uncompressed_bytes == 200 * 1024

    monkeypatch.setenv("LARGE_GRAPH_UPLOAD_BYTES_THRESHOLD", str(1024 * 1024))
    monkeypatch.setenv("LARGE_GRAPH_BINARY_UPLOAD_BYTES_THRESHOLD", str(10 * 1024))
    service = _service(tmp_path, monkeypatch)
    try:
        session = service.get_or_create("s1", user_id="alice")
        session.register_upload("token", source.name, str(source))

        result = asyncio.run(
            service.do_load_graph_from_upload(
                "s1", source.name, append=False, user_id="alice"
            )
        )

        assert result["success"] is False
        assert result["code"] == "LARGE_GRAPH_FORMAT_UNSUPPORTED"
        assert result["source_bytes"] < result["binary_threshold_bytes"]
        assert result["archive_uncompressed_bytes"] > result["binary_threshold_bytes"]
        assert session.get_graph() is None
    finally:
        _close(service)

def test_only_exact_bundled_pt_can_pass_binary_size_guard(tmp_path, monkeypatch):
    import hashlib
    import json
    import torch
    import graph_engine.example_inputs as examples

    source = tmp_path / "arxiv.pt"
    torch.save({"edge_index": torch.tensor([[0, 1], [1, 2]]), "num_nodes": 3}, source)
    resources = tmp_path / "packaged"
    resources.mkdir()
    entry = {"filename": "arxiv.pt", "local_graph_loading": True,
             "size_bytes": source.stat().st_size,
             "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
             "metadata": {"num_nodes": 3, "num_edges": 2}}
    (resources / "manifest.json").write_text(json.dumps({"inputs": [entry]}))
    original = examples.verified_local_graph_source
    monkeypatch.setattr(examples, "verified_local_graph_source",
        lambda path, **limits: original(path, resources=resources, **limits))
    monkeypatch.setenv("LARGE_GRAPH_BINARY_UPLOAD_BYTES_THRESHOLD", "1")
    service = _service(tmp_path, monkeypatch)
    try:
        session = service.get_or_create("s1", user_id="alice")
        session.register_upload("token", source.name, str(source))
        result = asyncio.run(service.do_load_graph_from_upload("s1", source.name, append=False, user_id="alice"))
        assert result["success"] is True
        assert session.get_graph().num_nodes == 3
        assert session.get_graph().num_edges == 2
        appended = asyncio.run(service.do_load_graph_from_upload("s1", source.name, append=True, user_id="alice"))
        assert appended["code"] == "LARGE_GRAPH_APPEND_UNSUPPORTED"
        assert not original(source, resources=resources, max_nodes=3, max_edges=10)
        raw = source.read_bytes()
        source.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        rejected = asyncio.run(service.do_load_graph_from_upload("s1", source.name, append=False, user_id="alice"))
        assert rejected["code"] == "LARGE_GRAPH_FORMAT_UNSUPPORTED"
        assert session.get_graph().num_nodes == 3
    finally:
        _close(service)

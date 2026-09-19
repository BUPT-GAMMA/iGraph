#!/usr/bin/env python3
"""Generate and exercise a real partitioned large-graph snapshot.

This is an intentionally manual benchmark rather than a pytest test: its
default fixture contains ten million nodes and one hundred million directed
edges and can consume substantial time and disk space.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import socket
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from graph_data_service.large_graph.compatibility import (
    SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS,
    SMALL_GRAPH_ALGORITHM_MODES,
)
from graph_data_service.large_graph.service import LargeGraphService


TERMINAL_JOB_STATES = {
    "done",
    "failed",
    "cancelled",
    "resource_rejected",
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _generate_part(arguments: tuple[str, int, int, int, int]) -> dict[str, Any]:
    raw_path, start, stop, node_count, edges_per_node = arguments
    path = Path(raw_path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    line_count = 0
    started = time.monotonic()
    with temporary.open("w", encoding="ascii", buffering=16 * 1024 * 1024) as output:
        for block_start in range(start, stop, 20_000):
            block_stop = min(stop, block_start + 20_000)
            rows = [
                f"{source} {(source + offset) % node_count}\n"
                for source in range(block_start, block_stop)
                for offset in range(1, edges_per_node + 1)
            ]
            output.write("".join(rows))
            line_count += (block_stop - block_start) * edges_per_node
    temporary.replace(path)
    return {
        "path": str(path),
        "source_start": start,
        "source_stop": stop,
        "edges": line_count,
        "bytes": path.stat().st_size,
        "seconds": round(time.monotonic() - started, 3),
    }


def generate_dataset(
    root: Path,
    *,
    node_count: int,
    edges_per_node: int,
    workers: int,
) -> dict[str, Any]:
    dataset_path = root / "edges.txt"
    metadata_path = root / "dataset.json"
    expected_edges = node_count * edges_per_node
    if metadata_path.is_file() and dataset_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            int(metadata.get("node_count", -1)) == node_count
            and int(metadata.get("edge_count", -1)) == expected_edges
            and int(metadata.get("bytes", -1)) == dataset_path.stat().st_size
        ):
            print(
                json.dumps(
                    {"event": "dataset_reused", **metadata}, ensure_ascii=False
                ),
                flush=True,
            )
            return metadata

    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    part_count = max(1, workers * 2)
    sources_per_part = (node_count + part_count - 1) // part_count
    jobs: list[tuple[str, int, int, int, int]] = []
    for part_index in range(part_count):
        start = part_index * sources_per_part
        stop = min(node_count, start + sources_per_part)
        if start >= stop:
            break
        part_path = root / f"edges.part-{part_index:03d}"
        jobs.append((str(part_path), start, stop, node_count, edges_per_node))

    completed: list[dict[str, Any]] = []
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        futures = [pool.submit(_generate_part, job) for job in jobs]
        for future in as_completed(futures):
            result = future.result()
            completed.append(result)
            print(
                json.dumps(
                    {
                        "event": "dataset_part_ready",
                        "parts_completed": len(completed),
                        "parts_total": len(jobs),
                        **result,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    completed.sort(key=lambda row: int(row["source_start"]))
    combined = dataset_path.with_suffix(".txt.building")
    with combined.open("wb", buffering=16 * 1024 * 1024) as output:
        for row in completed:
            part_path = Path(str(row["path"]))
            with part_path.open("rb") as source:
                shutil.copyfileobj(source, output, length=16 * 1024 * 1024)
    combined.replace(dataset_path)
    for row in completed:
        Path(str(row["path"])).unlink(missing_ok=True)

    actual_edges = sum(int(row["edges"]) for row in completed)
    if actual_edges != expected_edges:
        raise RuntimeError(
            f"generated edge count {actual_edges} != expected {expected_edges}"
        )
    metadata = {
        "format": "directed_edgelist",
        "path": str(dataset_path),
        "node_count": node_count,
        "edge_count": actual_edges,
        "edges_per_node": edges_per_node,
        "bytes": dataset_path.stat().st_size,
        "generation_workers": workers,
        "generation_seconds": round(time.monotonic() - started, 3),
        "topology": "node i has edges to (i + 1..edges_per_node) mod node_count",
    }
    _write_json(metadata_path, metadata)
    print(json.dumps({"event": "dataset_ready", **metadata}, ensure_ascii=False), flush=True)
    return metadata


def _wait_for_job(
    service: LargeGraphService,
    job_id: str,
    *,
    user_id: str,
    session_id: str,
    poll_seconds: float,
    max_seconds: float | None = None,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    previous: tuple[Any, ...] | None = None
    last_report = 0.0
    while True:
        status = service.job_status(job_id, user_id=user_id, session_id=session_id)
        if status is None:
            raise RuntimeError(f"job disappeared: {job_id}")
        marker = (
            status.get("status"),
            status.get("stage"),
            status.get("progress"),
            status.get("message"),
        )
        now = time.monotonic()
        if marker != previous or now - last_report >= 30:
            print(
                json.dumps(
                    {
                        "event": "job_status",
                        "elapsed_seconds": round(now - started, 3),
                        **status,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            previous = marker
            last_report = now
        if str(status.get("status")) in TERMINAL_JOB_STATES:
            return status, now - started
        if max_seconds is not None and now - started >= max_seconds:
            service.cancel(job_id, user_id=user_id, session_id=session_id)
            print(
                json.dumps(
                    {
                        "event": "job_timeout_cancel_requested",
                        "job_id": job_id,
                        "max_seconds": max_seconds,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            max_seconds = None
        time.sleep(max(0.2, poll_seconds))


def prepare_snapshot(
    root: Path,
    dataset: dict[str, Any],
    *,
    partitions: int,
    workers: int,
    poll_seconds: float,
) -> dict[str, Any]:
    report_path = root / "prepare-report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status", {}).get("status") == "done":
            summary = dict(report.get("status", {}).get("summary") or {})
            print(
                json.dumps(
                    {
                        "event": "snapshot_reused",
                        "identity": report.get("identity"),
                        "elapsed_seconds": report.get("elapsed_seconds"),
                        "node_count": summary.get("node_count"),
                        "edge_count": summary.get("edge_count"),
                        "partitions": summary.get("partitions"),
                        "snapshot_id": summary.get("snapshot_id"),
                        "storage_bytes": (
                            summary.get("storage_bytes") or {}
                        ).get("total"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return report

    user_id = "scale-benchmark"
    session_id = "10m-nodes-100m-edges"
    graph_id = "synthetic-ring-10m-100m"
    service = LargeGraphService(root / "runtime", embedded_worker=True)
    try:
        source = Path(str(dataset["path"]))
        submitted = service.submit_preparation(
            user_id=user_id,
            session_id=session_id,
            graph_id=graph_id,
            source_path=str(source),
            source_name=source.name,
            source_token="synthetic-10m-100m-v1",
            directed=True,
            partitions=partitions,
            workers=workers,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        print(json.dumps({"event": "prepare_submitted", **submitted}, ensure_ascii=False), flush=True)
        if not submitted.get("success"):
            report = {"submission": submitted, "status": {"status": "rejected"}}
            _write_json(report_path, report)
            return report
        status, elapsed = _wait_for_job(
            service,
            str(submitted["job_id"]),
            user_id=user_id,
            session_id=session_id,
            poll_seconds=poll_seconds,
        )
        result = service.job_result(
            str(submitted["job_id"]), user_id=user_id, session_id=session_id
        )
        report = {
            "identity": {
                "user_id": user_id,
                "session_id": session_id,
                "graph_id": graph_id,
                "graph_version": int(submitted["graph_version"]),
            },
            "submission": submitted,
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
            "result": result,
        }
        _write_json(report_path, report)
        return report
    finally:
        service.close()


DEFAULT_COMPUTES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("global_metrics", {"metric": "density"}),
    ("degree", {"top_k": 20}),
    ("shortest_path", {"source": "0", "target": "1000"}),
    ("degree_analysis", {"limit": 20}),
    (
        "node_centrality",
        {
            "centrality_type": "pagerank",
            "max_iterations": 10,
            "tolerance": 1e-8,
            "top_k": 20,
        },
    ),
    (
        "link_prediction_score",
        {"method": "jaccard", "source": "0", "target": "1000"},
    ),
    (
        "path_analysis",
        {"method": "has_path", "source": "0", "target": "1000"},
    ),
    (
        "subgraph_extraction",
        {"method": "ego_graph", "node": "0", "k": 1, "create_version": True},
    ),
)


def _audit_parameters(algorithm: str, mode: str | None) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    selector = SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS.get(algorithm)
    if selector and mode is not None:
        parameters[selector] = mode
    if algorithm in {"shortest_path", "all_shortest_paths"}:
        parameters.update({"source": "0", "target": "1000"})
        if algorithm == "all_shortest_paths":
            parameters["limit"] = 1
        if mode == "yen_k_shortest":
            parameters["k"] = 1
    elif algorithm in {"maximum_flow", "minimum_cut"}:
        parameters.update({"source": "0", "sink": "1000"})
    elif algorithm == "global_minimum_cut":
        parameters["treat_as_undirected"] = True
    elif algorithm == "community_detection":
        parameters["max_iterations"] = 1
    elif algorithm == "node_centrality":
        parameters.update({"limit": 20, "max_iterations": 1})
        if mode in {"betweenness", "load", "percolation"}:
            parameters["sample_sources"] = 1
    elif algorithm in {"find_cycles", "maximum_cycle"}:
        parameters.update({"limit": 1, "max_cycles": 1})
    elif algorithm == "degree_analysis":
        parameters["limit"] = 20
    elif algorithm == "subgraph_extraction":
        parameters["create_version"] = False
        if mode == "ego_graph":
            parameters.update({"node": "0", "k": 1})
        elif mode == "node_subset":
            parameters["nodes"] = ["0", "1"]
        elif mode == "k_truss":
            parameters["k"] = 3
        else:
            parameters["k"] = 2
    elif algorithm == "link_prediction_score":
        parameters.update({"source": "0", "target": "1000"})
    elif algorithm == "path_analysis":
        if mode in {"all_simple_paths", "has_path"}:
            parameters.update({"source": "0", "target": "1000"})
        if mode == "all_simple_paths":
            parameters.update({"cutoff": 100, "limit": 1})
    elif algorithm == "clique_analysis" and mode == "find_cliques":
        parameters["limit"] = 1
    elif algorithm == "reachability":
        if mode in {"descendants", "ancestors"}:
            parameters["node"] = "0"
        else:
            parameters["limit"] = 1
    return parameters


def audit_routes(root: Path, prepare_report: dict[str, Any], *, workers: int) -> dict[str, Any]:
    if prepare_report.get("status", {}).get("status") != "done":
        raise RuntimeError("snapshot preparation did not complete successfully")
    summary = dict(prepare_report["status"]["summary"])
    service = LargeGraphService(root / "runtime", embedded_worker=False)
    cases: list[dict[str, Any]] = []
    try:
        for algorithm, modes in SMALL_GRAPH_ALGORITHM_MODES.items():
            concrete_modes: tuple[str | None, ...] = modes or (None,)
            for mode in concrete_modes:
                parameters = _audit_parameters(algorithm, mode)
                decision = service.router.route(
                    operation_scope="global",
                    node_count=int(summary["node_count"]),
                    edge_count=int(summary["edge_count"]),
                    algorithm=algorithm,
                    directed=bool(summary["directed"]),
                    weighted=False,
                    snapshot_available=True,
                    partitions=int(summary["partitions"]),
                    workers=workers,
                    force_large=True,
                    parameters=parameters,
                    snapshot_storage_bytes=summary["storage_bytes"],
                )
                cases.append(
                    {
                        "algorithm": algorithm,
                        "mode": mode or "default",
                        "parameters": parameters,
                        "accepted": decision.accepted,
                        "path": decision.path,
                        "code": decision.code,
                        "reason": decision.reason,
                        "estimated_work_units": (
                            decision.resource_estimate or {}
                        ).get("estimated_work_units"),
                    }
                )
    finally:
        service.close()
    report = {
        "node_count": int(summary["node_count"]),
        "edge_count": int(summary["edge_count"]),
        "directed": bool(summary["directed"]),
        "case_count": len(cases),
        "accepted_count": sum(bool(case["accepted"]) for case in cases),
        "rejected_count": sum(not bool(case["accepted"]) for case in cases),
        "cases": cases,
    }
    report_path = root / "route-audit-report.json"
    _write_json(report_path, report)
    print(
        json.dumps(
            {
                "event": "route_audit",
                "node_count": report["node_count"],
                "edge_count": report["edge_count"],
                "case_count": report["case_count"],
                "accepted_count": report["accepted_count"],
                "rejected_count": report["rejected_count"],
                "report_path": str(report_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


def run_computes(
    root: Path,
    prepare_report: dict[str, Any],
    *,
    workers: int,
    poll_seconds: float,
    selected_algorithms: set[str] | None,
) -> dict[str, Any]:
    if prepare_report.get("status", {}).get("status") != "done":
        raise RuntimeError("snapshot preparation did not complete successfully")
    identity = dict(prepare_report["identity"])
    report_path = root / "compute-report.json"
    existing: dict[str, Any] = {}
    if report_path.is_file():
        existing = json.loads(report_path.read_text(encoding="utf-8"))
    runs = list(existing.get("runs") or [])
    completed_keys = {
        (str(row.get("algorithm")), json.dumps(row.get("parameters"), sort_keys=True))
        for row in runs
        if row.get("status", {}).get("status") == "done"
    }

    computes = [(algorithm, dict(parameters)) for algorithm, parameters in DEFAULT_COMPUTES]
    prepared_node_count = int(
        prepare_report.get("status", {}).get("summary", {}).get("node_count", 0)
    )
    for algorithm, parameters in computes:
        if algorithm == "shortest_path":
            parameters["target"] = str(max(0, min(1000, prepared_node_count - 1)))

    service = LargeGraphService(root / "runtime", embedded_worker=True)
    try:
        for algorithm, parameters in computes:
            if selected_algorithms is not None and algorithm not in selected_algorithms:
                continue
            key = (algorithm, json.dumps(parameters, sort_keys=True))
            if key in completed_keys:
                print(
                    json.dumps(
                        {"event": "compute_reused", "algorithm": algorithm},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
            submitted = service.submit_compute(
                user_id=str(identity["user_id"]),
                session_id=str(identity["session_id"]),
                graph_id=str(identity["graph_id"]),
                graph_version=int(identity["graph_version"]),
                algorithm=algorithm,
                parameters=parameters,
                workers=workers,
            )
            print(
                json.dumps(
                    {"event": "compute_submitted", "algorithm": algorithm, **submitted},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if submitted.get("success"):
                status, elapsed = _wait_for_job(
                    service,
                    str(submitted["job_id"]),
                    user_id=str(identity["user_id"]),
                    session_id=str(identity["session_id"]),
                    poll_seconds=poll_seconds,
                )
                result = service.job_result(
                    str(submitted["job_id"]),
                    user_id=str(identity["user_id"]),
                    session_id=str(identity["session_id"]),
                )
            else:
                status = {"status": "rejected", "error": submitted.get("error")}
                elapsed = 0.0
                result = None
            runs.append(
                {
                    "algorithm": algorithm,
                    "parameters": parameters,
                    "submission": submitted,
                    "status": status,
                    "elapsed_seconds": round(elapsed, 3),
                    "result": result,
                }
            )
            _write_json(
                report_path,
                {"identity": identity, "host": socket.gethostname(), "runs": runs},
            )
            if status.get("status") not in {"done", "resource_rejected"}:
                break
    finally:
        service.close()
    report = {"identity": identity, "host": socket.gethostname(), "runs": runs}
    _write_json(report_path, report)
    return report


def _mode_key(algorithm: str, mode: str, parameters: dict[str, Any]) -> str:
    return json.dumps(
        [algorithm, mode, parameters],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def run_exhaustive_modes(
    root: Path,
    prepare_report: dict[str, Any],
    *,
    workers: int,
    poll_seconds: float,
    max_job_seconds: float | None,
) -> dict[str, Any]:
    """Submit every concrete small-graph-compatible mode on the full snapshot.

    Admission rejection is a tested terminal path, not silently bypassed with
    ``allow_expensive``.  The report is rewritten after submission and after
    completion so an interrupted multi-hour run can resume without duplicating
    a durable job.
    """
    if prepare_report.get("status", {}).get("status") != "done":
        raise RuntimeError("snapshot preparation did not complete successfully")
    identity = dict(prepare_report["identity"])
    report_path = root / "exhaustive-mode-report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {
            "identity": identity,
            "host": socket.gethostname(),
            "input_node_count": int(
                prepare_report["status"]["summary"]["node_count"]
            ),
            "input_edge_count": int(
                prepare_report["status"]["summary"]["edge_count"]
            ),
            "runs": [],
        }
    runs = list(report.get("runs") or [])
    by_key = {str(row.get("case_key")): row for row in runs}
    target_node = str(max(0, min(1000, int(report["input_node_count"]) - 1)))

    def persist() -> None:
        report["runs"] = runs
        report["updated_at_epoch"] = time.time()
        report["counts"] = {
            "total": len(runs),
            "done": sum(row.get("outcome") == "done" for row in runs),
            "expected_rejection": sum(
                row.get("outcome") == "expected_rejection" for row in runs
            ),
            "failed": sum(row.get("outcome") == "failed" for row in runs),
            "cancelled": sum(row.get("outcome") == "cancelled" for row in runs),
        }
        _write_json(report_path, report)

    service = LargeGraphService(root / "runtime", embedded_worker=True)
    try:
        case_number = 0
        for algorithm, modes in SMALL_GRAPH_ALGORITHM_MODES.items():
            for raw_mode in modes or (None,):
                case_number += 1
                mode = raw_mode or "default"
                parameters = _audit_parameters(algorithm, raw_mode)
                for endpoint_key in ("target", "sink"):
                    if endpoint_key in parameters:
                        parameters[endpoint_key] = target_node
                case_key = _mode_key(algorithm, mode, parameters)
                row = by_key.get(case_key)
                if row is not None and row.get("outcome") in {
                    "done", "expected_rejection", "failed", "cancelled"
                }:
                    print(
                        json.dumps(
                            {
                                "event": "exhaustive_case_reused",
                                "case": case_number,
                                "algorithm": algorithm,
                                "mode": mode,
                                "outcome": row.get("outcome"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    continue

                if row is None:
                    row = {
                        "case_key": case_key,
                        "case": case_number,
                        "algorithm": algorithm,
                        "mode": mode,
                        "parameters": parameters,
                    }
                    runs.append(row)
                    by_key[case_key] = row

                submitted = row.get("submission")
                job_id = str((submitted or {}).get("job_id") or "")
                if not job_id:
                    submitted = service.submit_compute(
                        user_id=str(identity["user_id"]),
                        session_id=str(identity["session_id"]),
                        graph_id=str(identity["graph_id"]),
                        graph_version=int(identity["graph_version"]),
                        algorithm=algorithm,
                        parameters=parameters,
                        workers=workers,
                    )
                    row["submission"] = submitted
                    row["submitted_at_epoch"] = time.time()
                    persist()
                    print(
                        json.dumps(
                            {
                                "event": "exhaustive_case_submitted",
                                "case": case_number,
                                "algorithm": algorithm,
                                "mode": mode,
                                "success": submitted.get("success"),
                                "code": submitted.get("code")
                                or (submitted.get("route_decision") or {}).get("code"),
                                "job_id": submitted.get("job_id"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    job_id = str(submitted.get("job_id") or "")

                if not (submitted or {}).get("success"):
                    row["outcome"] = "expected_rejection"
                    row["status"] = {
                        "status": "rejected",
                        "code": (submitted or {}).get("code"),
                        "error": (submitted or {}).get("error"),
                    }
                    row["elapsed_seconds"] = 0.0
                    persist()
                    continue

                status, elapsed = _wait_for_job(
                    service,
                    job_id,
                    user_id=str(identity["user_id"]),
                    session_id=str(identity["session_id"]),
                    poll_seconds=poll_seconds,
                    max_seconds=max_job_seconds,
                )
                row["status"] = status
                row["elapsed_seconds"] = round(elapsed, 3)
                row["result"] = service.job_result(
                    job_id,
                    user_id=str(identity["user_id"]),
                    session_id=str(identity["session_id"]),
                )
                terminal = str(status.get("status"))
                row["outcome"] = (
                    "done"
                    if terminal == "done"
                    else "cancelled"
                    if terminal == "cancelled"
                    else "failed"
                )
                persist()
    finally:
        service.close()
    persist()
    print(
        json.dumps(
            {
                "event": "exhaustive_modes_complete",
                "counts": report["counts"],
                "report_path": str(report_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/tmp/igraph-scale-10m-100m"),
    )
    parser.add_argument(
        "--phase",
        choices=("generate", "prepare", "compute", "audit", "exhaustive", "all"),
        default="all",
    )
    parser.add_argument("--nodes", type=int, default=10_000_000)
    parser.add_argument("--edges-per-node", type=int, default=10)
    parser.add_argument("--partitions", type=int, default=64)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument(
        "--max-job-seconds",
        type=float,
        default=3600.0,
        help="exhaustive phase per-job timeout; <=0 disables the benchmark timeout",
    )
    parser.add_argument(
        "--algorithms",
        help="comma-separated subset of the built-in compute benchmark",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.nodes < 2 or args.edges_per_node < 1:
        raise ValueError("nodes must be >= 2 and edges-per-node must be >= 1")
    if args.edges_per_node >= args.nodes:
        raise ValueError("edges-per-node must be smaller than nodes")
    args.root.mkdir(parents=True, exist_ok=True)
    dataset: dict[str, Any] | None = None
    prepare_report: dict[str, Any] | None = None
    if args.phase in {
        "generate", "prepare", "compute", "audit", "exhaustive", "all"
    }:
        dataset = generate_dataset(
            args.root,
            node_count=args.nodes,
            edges_per_node=args.edges_per_node,
            workers=args.workers,
        )
    if args.phase in {"prepare", "compute", "audit", "exhaustive", "all"}:
        assert dataset is not None
        prepare_report = prepare_snapshot(
            args.root,
            dataset,
            partitions=args.partitions,
            workers=args.workers,
            poll_seconds=args.poll_seconds,
        )
    if args.phase in {"compute", "all"}:
        assert prepare_report is not None
        selected = (
            {item.strip() for item in args.algorithms.split(",") if item.strip()}
            if args.algorithms
            else None
        )
        run_computes(
            args.root,
            prepare_report,
            workers=args.workers,
            poll_seconds=args.poll_seconds,
            selected_algorithms=selected,
        )
    if args.phase in {"audit", "all"}:
        assert prepare_report is not None
        audit_routes(args.root, prepare_report, workers=args.workers)
    if args.phase in {"exhaustive", "all"}:
        assert prepare_report is not None
        run_exhaustive_modes(
            args.root,
            prepare_report,
            workers=args.workers,
            poll_seconds=args.poll_seconds,
            max_job_seconds=(
                args.max_job_seconds if args.max_job_seconds > 0 else None
            ),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

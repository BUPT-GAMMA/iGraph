"""GraphScope native app invocation and bounded result adaptation."""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
import unicodedata
from itertools import chain
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import ADAPTER_VERSION
from .storage_codec import decode_edge_row, decode_node_row


def _emit_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    with path.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(
                json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                + "\n"
            )
            if len(preview) < 20:
                preview.append(row)
    return preview


def _safe_float(value: Any) -> float:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"\\x00", "\x00", "false"}:
            return 0.0
        if normalized in {"\\x01", "\x01", "true"}:
            return 1.0
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("GraphScope 返回了非有限数值")
    return parsed


def _control_float(value: Any) -> float:
    return round(_safe_float(value), 12)


def _native_rows(paths: list[Path]) -> Iterator[tuple[str, float]]:
    for path in paths:
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(
                line.replace("\x00", "0").replace("\x01", "1")
                for line in source
            )
            first = next(reader, None)
            if not first:
                continue
            lowered = [str(value).strip().lower() for value in first]
            has_header = any(value in {"node_id", "id"} for value in lowered) and any(
                value in {"value", "r"} for value in lowered
            )
            node_index = (
                lowered.index("node_id")
                if "node_id" in lowered
                else lowered.index("id")
                if "id" in lowered
                else 0
            )
            value_index = (
                lowered.index("value")
                if "value" in lowered
                else lowered.index("r")
                if "r" in lowered
                else 1
            )
            rows: Iterable[list[str]] = reader if has_header else chain((first,), reader)
            for row in rows:
                if len(row) <= max(node_index, value_index):
                    raise ValueError("GraphScope 原生结果缺少 node_id 或 value 列")
                yield str(row[node_index]), _safe_float(row[value_index])


def _native_label_rows(paths: list[Path]) -> Iterator[tuple[str, str]]:
    """Read WCC labels losslessly; labels may be 64-bit internal IDs."""

    for path in paths:
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source)
            first = next(reader, None)
            if not first:
                continue
            lowered = [str(value).strip().lower() for value in first]
            has_header = any(value in {"node_id", "id"} for value in lowered) and any(
                value in {"value", "r"} for value in lowered
            )
            node_index = (
                lowered.index("node_id")
                if "node_id" in lowered
                else lowered.index("id")
                if "id" in lowered
                else 0
            )
            value_index = (
                lowered.index("value")
                if "value" in lowered
                else lowered.index("r")
                if "r" in lowered
                else 1
            )
            rows: Iterable[list[str]] = reader if has_header else chain((first,), reader)
            for row in rows:
                if len(row) <= max(node_index, value_index):
                    raise ValueError("GraphScope WCC 结果缺少 node_id 或 value 列")
                yield str(row[node_index]), str(row[value_index])


def _native_property_rows(
    paths: list[Path], properties: tuple[str, ...]
) -> Iterator[tuple[str, list[float]]]:
    for path in paths:
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source)
            first = next(reader, None)
            if not first:
                continue
            lowered = [str(value).strip().lower() for value in first]
            has_header = any(value in {"node_id", "id"} for value in lowered) and all(
                name in lowered for name in properties
            )
            node_index = (
                lowered.index("node_id")
                if "node_id" in lowered
                else lowered.index("id")
                if "id" in lowered
                else len(properties)
            )
            value_indexes = (
                [lowered.index(name) for name in properties]
                if has_header
                else list(range(len(properties)))
            )
            rows: Iterable[list[str]] = reader if has_header else chain((first,), reader)
            for row in rows:
                if len(row) <= max([node_index, *value_indexes]):
                    raise ValueError("GraphScope 属性结果列不完整")
                yield str(row[node_index]), [
                    _safe_float(row[index]) for index in value_indexes
                ]


def _open_state(work_root: Path, manifest: dict[str, Any], snapshot_root: Path) -> sqlite3.Connection:
    work_root.mkdir(parents=True, exist_ok=True)
    state_path = work_root / "adapter-state.sqlite3"
    connection = sqlite3.connect(state_path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute(
        """
        CREATE TABLE nodes(
            node_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            ordinal INTEGER NOT NULL UNIQUE
        ) WITHOUT ROWID
        """
    )
    order_file = snapshot_root / str(manifest["node_order_file"])
    batch: list[tuple[str, str, int]] = []
    with order_file.open("r", encoding="utf-8") as source:
        for ordinal, line in enumerate(source):
            node_id, name, _ = decode_node_row(line)
            batch.append((node_id, name, ordinal))
            if len(batch) >= 4096:
                connection.executemany(
                    "INSERT INTO nodes(node_id,name,ordinal) VALUES(?,?,?)", batch
                )
                batch.clear()
    if batch:
        connection.executemany(
            "INSERT INTO nodes(node_id,name,ordinal) VALUES(?,?,?)", batch
        )
    connection.commit()
    return connection


def _display(connection: sqlite3.Connection, node_id: str) -> str:
    row = connection.execute(
        "SELECT name FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return str(row[0]) if row else node_id


def _normalise_node(value: str, mode: str) -> str:
    display = str(value or "").strip()
    if mode == "exact":
        return display
    folded = unicodedata.normalize("NFKC", display)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded.casefold()


def _resolve_source(
    connection: sqlite3.Connection, manifest: dict[str, Any], raw: Any
) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("缺少起点")
    mode = str(
        manifest.get("quality", {})
        .get("policy", {})
        .get("node_normalization", "nfkc_casefold")
    )
    canonical = _normalise_node(value, mode)
    row = connection.execute(
        "SELECT node_id FROM nodes WHERE node_id=?", (canonical,)
    ).fetchone()
    if row:
        return str(row[0])
    row = connection.execute(
        "SELECT node_id FROM nodes WHERE name=? ORDER BY ordinal LIMIT 1", (value,)
    ).fetchone()
    if row:
        return str(row[0])
    raise ValueError(f"起点 '{value}' 不存在")


def _load_values(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    *,
    value_type: str,
) -> None:
    connection.execute(
        "CREATE TABLE values_by_node(node_id TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
    )
    batch: list[tuple[str, float]] = []
    seen = 0
    for node_id, value in _native_rows(native_paths):
        batch.append((node_id, value))
        if len(batch) >= 4096:
            try:
                connection.executemany(
                    "INSERT INTO values_by_node(node_id,value) VALUES(?,?)", batch
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"GraphScope {value_type} 结果包含重复节点") from exc
            seen += len(batch)
            batch.clear()
    if batch:
        try:
            connection.executemany(
                "INSERT INTO values_by_node(node_id,value) VALUES(?,?)", batch
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"GraphScope {value_type} 结果包含重复节点") from exc
        seen += len(batch)
    unknown = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM values_by_node v
            LEFT JOIN nodes n ON n.node_id=v.node_id WHERE n.node_id IS NULL
            """
        ).fetchone()[0]
    )
    expected = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    if unknown or seen != expected:
        raise ValueError(
            f"GraphScope {value_type} 结果节点覆盖不完整(expected={expected}, actual={seen}, unknown={unknown})"
        )
    connection.commit()


def adapt_wcc(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    limit: int,
) -> dict[str, Any]:
    connection.execute(
        "CREATE TABLE labels_by_node(node_id TEXT PRIMARY KEY,label TEXT NOT NULL) WITHOUT ROWID"
    )
    batch: list[tuple[str, str]] = []
    seen = 0
    for node_id, label in _native_label_rows(native_paths):
        batch.append((node_id, label))
        if len(batch) >= 4096:
            try:
                connection.executemany(
                    "INSERT INTO labels_by_node(node_id,label) VALUES(?,?)", batch
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("GraphScope WCC 结果包含重复节点") from exc
            seen += len(batch)
            batch.clear()
    if batch:
        try:
            connection.executemany(
                "INSERT INTO labels_by_node(node_id,label) VALUES(?,?)", batch
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("GraphScope WCC 结果包含重复节点") from exc
        seen += len(batch)
    expected = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    unknown = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM labels_by_node v
            LEFT JOIN nodes n ON n.node_id=v.node_id WHERE n.node_id IS NULL
            """
        ).fetchone()[0]
    )
    if unknown or seen != expected:
        raise ValueError(
            f"GraphScope WCC 结果节点覆盖不完整(expected={expected}, actual={seen}, unknown={unknown})"
        )
    connection.executescript(
        """
        CREATE TABLE component_labels(
            native_value TEXT PRIMARY KEY,
            component INTEGER NOT NULL UNIQUE
        ) WITHOUT ROWID;
        INSERT INTO component_labels(native_value,component)
        SELECT label,ROW_NUMBER() OVER(ORDER BY label)-1
        FROM labels_by_node GROUP BY label;
        """
    )
    groups = list(
        connection.execute(
            """
            SELECT l.component,COUNT(*) AS size
            FROM labels_by_node v JOIN component_labels l ON l.native_value=v.label
            GROUP BY l.component ORDER BY size DESC,l.component LIMIT ?
            """,
            (limit,),
        )
    )
    total = int(connection.execute("SELECT COUNT(*) FROM component_labels").fetchone()[0])
    components: list[dict[str, Any]] = []
    for component, size in groups:
        names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT n.name FROM labels_by_node v
                JOIN component_labels l ON l.native_value=v.label
                JOIN nodes n ON n.node_id=v.node_id
                WHERE l.component=? ORDER BY n.ordinal LIMIT 10
                """,
                (component,),
            )
        ]
        components.append(
            {
                "size": int(size),
                "nodes_preview": names,
                "nodes_truncated": int(size) > len(names),
            }
        )

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, component in connection.execute(
            """
            SELECT n.node_id,n.name,l.component FROM nodes n
            JOIN labels_by_node v ON v.node_id=n.node_id
            JOIN component_labels l ON l.native_value=v.label
            ORDER BY l.component,n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "component": int(component)}

    output = result_root / "components.jsonl"
    preview = _emit_jsonl(output, rows())
    component_summary = {
        "count": total,
        "largest_size": int(groups[0][1]) if groups else 0,
        "components": components,
        "truncated": total > len(groups),
    }
    return {
        "summary": {
            "success": True,
            "directed": False,
            "is_connected": total == 1,
            "connected_components": component_summary,
            "execution_algorithm": "graphscope_wcc",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_centrality(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    kind: str,
    native_app: str,
    limit: int,
    positive_only: bool = False,
    approximate: bool = False,
    approximation_reasons: list[str] | None = None,
    value_transform: str | None = None,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type=kind)
    if value_transform == "reciprocal":
        connection.execute(
            "UPDATE values_by_node SET value=CASE WHEN value>0 THEN 1.0/value ELSE 0 END"
        )
    elif value_transform == "harmonic_sample_scale":
        node_count = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
        connection.execute(
            "UPDATE values_by_node SET value=value*?", (node_count / 500.0,)
        )
    connection.commit()
    if positive_only:
        # VoteRank writes zero for vertices that were not elected after all
        # remaining voting scores reached zero.  The existing iGraph contract
        # contains elected vertices only.
        connection.execute("DELETE FROM values_by_node WHERE value<=0")
        connection.commit()
    count = int(connection.execute("SELECT COUNT(*) FROM values_by_node").fetchone()[0])
    aggregate = connection.execute(
        "SELECT MIN(value),MAX(value),AVG(value) FROM values_by_node"
    ).fetchone()
    median_offset = max(0, (count - 1) // 2)
    median_take = 2 if count and count % 2 == 0 else 1
    median_values = [
        float(row[0])
        for row in connection.execute(
            "SELECT value FROM values_by_node ORDER BY value LIMIT ? OFFSET ?",
            (median_take, median_offset),
        )
    ]
    median = sum(median_values) / len(median_values) if median_values else 0.0
    top = [
        {"node": str(name), "value": _control_float(value)}
        for name, value in connection.execute(
            """
            SELECT n.name,v.value FROM values_by_node v
            JOIN nodes n ON n.node_id=v.node_id
            ORDER BY v.value DESC,n.ordinal LIMIT ?
            """,
            (limit,),
        )
    ]
    bottom_start = max(limit, count - 10)
    bottom = [
        {"node": str(name), "value": _control_float(value)}
        for name, value in connection.execute(
            """
            SELECT n.name,v.value FROM values_by_node v
            JOIN nodes n ON n.node_id=v.node_id
            ORDER BY v.value DESC,n.ordinal LIMIT 10 OFFSET ?
            """,
            (bottom_start,),
        )
    ]

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, value in connection.execute(
            """
            SELECT n.node_id,n.name,v.value FROM nodes n
            JOIN values_by_node v ON v.node_id=n.node_id ORDER BY n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "value": float(value)}

    output = result_root / f"centrality-{kind}.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "centrality_type": kind,
            "total_nodes": count,
            "execution_algorithm": f"graphscope_{native_app}",
            "execution_mode": "approximate" if approximate else "exact",
            "value_stats": {
                "min": _control_float((aggregate or (0, 0, 0))[0] or 0),
                "max": _control_float((aggregate or (0, 0, 0))[1] or 0),
                "mean": _control_float((aggregate or (0, 0, 0))[2] or 0),
                "median": _control_float(median),
            },
            "top_nodes": top,
            "bottom_nodes": bottom,
            "truncated": count > len(top),
            "approximate": approximate,
            **(
                {"approximation_reasons": list(approximation_reasons or [])}
                if approximation_reasons
                else {}
            ),
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_degree_score(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    native_app: str,
    limit: int,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type="degree_centrality")
    count = int(connection.execute("SELECT COUNT(*) FROM values_by_node").fetchone()[0])
    aggregate = connection.execute(
        "SELECT MIN(value),MAX(value),AVG(value) FROM values_by_node"
    ).fetchone()
    top = [
        {"node_id": str(node_id), "node": str(name), "score": _control_float(value)}
        for node_id, name, value in connection.execute(
            """
            SELECT n.node_id,n.name,v.value FROM values_by_node v
            JOIN nodes n ON n.node_id=v.node_id
            ORDER BY v.value DESC,n.ordinal LIMIT ?
            """,
            (limit,),
        )
    ]

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, value in connection.execute(
            """
            SELECT n.node_id,n.name,v.value FROM nodes n
            JOIN values_by_node v ON v.node_id=n.node_id ORDER BY n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "score": float(value)}

    output = result_root / "degree-centrality.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "algorithm": "degree_centrality",
            "result_count": count,
            "score_min": _control_float((aggregate or (0, 0, 0))[0] or 0),
            "score_max": _control_float((aggregate or (0, 0, 0))[1] or 0),
            "score_mean": _control_float((aggregate or (0, 0, 0))[2] or 0),
            "top": top,
            "truncated": count > len(top),
            "execution_algorithm": f"graphscope_{native_app}",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_scalar_result(
    *,
    algorithm: str,
    native_app: str,
    value: Any,
    field: str = "value",
    approximate: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = float(value)
    finite = math.isfinite(parsed)
    summary: dict[str, Any] = {
        "success": True,
        "algorithm": algorithm,
        field: (
            int(round(parsed))
            if finite and field == "count"
            else _control_float(parsed)
            if finite
            else None
        ),
        "defined": finite,
        "execution_algorithm": f"graphscope_{native_app}",
        "execution_mode": "approximate" if approximate else "exact",
    }
    summary.update(extra or {})
    return {"summary": summary, "preview": [], "result_files": []}


def adapt_average_degree_connectivity(
    result_root: Path, values: dict[Any, Any], *, limit: int
) -> dict[str, Any]:
    normalized = sorted(
        (int(degree), _safe_float(value)) for degree, value in values.items()
    )
    rows = [
        {"key": degree, "value": _control_float(value)}
        for degree, value in normalized
    ]
    output = result_root / "average-degree-connectivity.jsonl"
    preview = _emit_jsonl(output, iter(rows))
    return {
        "summary": {
            "success": True,
            "metric": "average_degree_connectivity",
            "count": len(rows),
            "values": rows[:limit],
            "truncated": len(rows) > limit,
            "execution_algorithm": "graphscope_average_degree_connectivity",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_component_labels(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    algorithm: str,
    native_app: str,
    limit: int,
) -> dict[str, Any]:
    _load_labels(connection, native_paths, value_type=algorithm)
    connection.executescript(
        """
        CREATE TABLE component_labels(
            native_value TEXT PRIMARY KEY,component INTEGER NOT NULL UNIQUE
        ) WITHOUT ROWID;
        INSERT INTO component_labels(native_value,component)
        SELECT label,ROW_NUMBER() OVER(ORDER BY label)-1
        FROM labels_by_node GROUP BY label;
        """
    )
    component_count = int(
        connection.execute("SELECT COUNT(*) FROM component_labels").fetchone()[0]
    )
    largest_size = int(
        connection.execute(
            "SELECT COUNT(*) FROM labels_by_node GROUP BY label ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()[0]
        if component_count
        else 0
    )

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, component in connection.execute(
            """
            SELECT n.node_id,n.name,c.component FROM nodes n
            JOIN labels_by_node l ON l.node_id=n.node_id
            JOIN component_labels c ON c.native_value=l.label
            ORDER BY c.component,n.ordinal
            """
        ):
            yield {
                "node_id": str(node_id),
                "node": str(name),
                "component": int(component),
            }

    output = result_root / f"{algorithm}.jsonl"
    preview = _emit_jsonl(output, rows())
    approximate = algorithm == "biconnected_components"
    native_note = (
        "GraphScope FLASH 以单标签表示双连通分组；割点的多分组成员关系不单独物化"
        if approximate
        else None
    )
    return {
        "summary": {
            "success": True,
            "algorithm": algorithm,
            "count": component_count,
            "largest_size": largest_size,
            "components_preview": [
                {"component": int(component), "size": int(size)}
                for component, size in connection.execute(
                    """
                    SELECT c.component,COUNT(*) FROM labels_by_node l
                    JOIN component_labels c ON c.native_value=l.label
                    GROUP BY c.component ORDER BY COUNT(*) DESC,c.component LIMIT ?
                    """,
                    (limit,),
                )
            ],
            "truncated": component_count > limit,
            "execution_algorithm": f"graphscope_{native_app}",
            "execution_mode": "approximate" if approximate else "exact",
            "approximate": approximate,
            **(
                {
                    "approximation_reasons": [
                        "GraphScope FLASH 单标签上下文不能表达割点属于多个双连通分量"
                    ]
                }
                if approximate
                else {}
            ),
            **({"note": native_note} if native_note else {}),
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_graph_coloring(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    limit: int,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type="graph_coloring")
    count = int(connection.execute("SELECT COUNT(*) FROM values_by_node").fetchone()[0])
    num_colors = int(
        connection.execute("SELECT COUNT(DISTINCT CAST(value AS INTEGER)) FROM values_by_node").fetchone()[0]
    )

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, color in connection.execute(
            """
            SELECT n.node_id,n.name,v.value FROM nodes n
            JOIN values_by_node v ON v.node_id=n.node_id
            ORDER BY CAST(v.value AS INTEGER),n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "color": int(color)}

    output = result_root / "graph-coloring.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "algorithm": "graph_coloring",
            "num_colors": num_colors,
            "node_count": count,
            "coloring": preview[:limit],
            "truncated": count > limit,
            "execution_algorithm": "graphscope_flash_color",
            "execution_mode": "approximate",
            "approximate": True,
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_k_shell(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    k: int,
    limit: int,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type="k_shell")
    connection.execute("DELETE FROM values_by_node WHERE value<=0")
    connection.commit()
    count = int(connection.execute("SELECT COUNT(*) FROM values_by_node").fetchone()[0])

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name in connection.execute(
            """
            SELECT n.node_id,n.name FROM nodes n
            JOIN values_by_node v ON v.node_id=n.node_id ORDER BY n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name)}

    output = result_root / "k-shell.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "algorithm": "k_shell",
            "k": int(k),
            "num_nodes": count,
            "nodes": [row["node"] for row in preview[:limit]],
            "truncated": count > limit,
            "execution_algorithm": "graphscope_k_shell",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_hits(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    limit: int,
) -> dict[str, Any]:
    connection.execute(
        "CREATE TABLE hits(node_id TEXT PRIMARY KEY,hub REAL NOT NULL,authority REAL NOT NULL) WITHOUT ROWID"
    )
    batch: list[tuple[str, float, float]] = []
    for node_id, values in _native_property_rows(native_paths, ("authority", "hub")):
        batch.append((node_id, values[1], values[0]))
        if len(batch) >= 4096:
            connection.executemany(
                "INSERT INTO hits(node_id,hub,authority) VALUES(?,?,?)", batch
            )
            batch.clear()
    if batch:
        connection.executemany(
            "INSERT INTO hits(node_id,hub,authority) VALUES(?,?,?)", batch
        )
    expected = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    actual = int(connection.execute("SELECT COUNT(*) FROM hits").fetchone()[0])
    unknown = int(
        connection.execute(
            "SELECT COUNT(*) FROM hits h LEFT JOIN nodes n ON n.node_id=h.node_id WHERE n.node_id IS NULL"
        ).fetchone()[0]
    )
    if actual != expected or unknown:
        raise ValueError(
            f"GraphScope HITS 结果节点覆盖不完整(expected={expected}, actual={actual}, unknown={unknown})"
        )
    connection.commit()

    def ranking(column: str) -> list[dict[str, Any]]:
        return [
            {"node": str(name), "value": _control_float(value)}
            for name, value in connection.execute(
                f"""
                SELECT n.name,h.{column} FROM hits h JOIN nodes n ON n.node_id=h.node_id
                ORDER BY h.{column} DESC,n.ordinal LIMIT ?
                """,
                (limit,),
            )
        ]

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, hub, authority in connection.execute(
            """
            SELECT n.node_id,n.name,h.hub,h.authority FROM nodes n
            JOIN hits h ON h.node_id=n.node_id ORDER BY n.ordinal
            """
        ):
            yield {
                "node_id": str(node_id),
                "node": str(name),
                "hub": float(hub),
                "authority": float(authority),
            }

    output = result_root / "hits.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "algorithm": "hits",
            "total_nodes": actual,
            "top_hubs": ranking("hub"),
            "top_authorities": ranking("authority"),
            "truncated": actual > limit,
            "execution_algorithm": "graphscope_hits",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_flash_betweenness(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    source_count: int,
    node_count: int,
    native_app: str,
    limit: int,
) -> dict[str, Any]:
    connection.execute(
        "CREATE TABLE values_by_node(node_id TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
    )
    connection.execute(
        "INSERT INTO values_by_node(node_id,value) SELECT node_id,0 FROM nodes"
    )
    batch: list[tuple[float, str]] = []
    for node_id, value in _native_rows(native_paths):
        batch.append((value, node_id))
        if len(batch) >= 4096:
            connection.executemany(
                "UPDATE values_by_node SET value=value+? WHERE node_id=?", batch
            )
            batch.clear()
    if batch:
        connection.executemany(
            "UPDATE values_by_node SET value=value+? WHERE node_id=?", batch
        )
    if source_count and source_count < node_count:
        connection.execute(
            "UPDATE values_by_node SET value=value*?", (node_count / source_count,)
        )
    if node_count > 2:
        connection.execute(
            "UPDATE values_by_node SET value=value*?",
            (1.0 / ((node_count - 1) * (node_count - 2)),),
        )
    connection.commit()
    # Reuse the bounded ranking/output contract after the native source
    # contexts have been reduced into the same table shape.
    count = int(connection.execute("SELECT COUNT(*) FROM values_by_node").fetchone()[0])
    aggregate = connection.execute(
        "SELECT MIN(value),MAX(value),AVG(value) FROM values_by_node"
    ).fetchone()
    top = [
        {"node": str(name), "value": _control_float(value)}
        for name, value in connection.execute(
            """
            SELECT n.name,v.value FROM values_by_node v JOIN nodes n ON n.node_id=v.node_id
            ORDER BY v.value DESC,n.ordinal LIMIT ?
            """,
            (limit,),
        )
    ]

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, value in connection.execute(
            """
            SELECT n.node_id,n.name,v.value FROM nodes n
            JOIN values_by_node v ON v.node_id=n.node_id ORDER BY n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "value": float(value)}

    output = result_root / "centrality-betweenness.jsonl"
    preview = _emit_jsonl(output, rows())
    approximate = True
    return {
        "summary": {
            "success": True,
            "centrality_type": "betweenness",
            "total_nodes": count,
            "execution_algorithm": f"graphscope_{native_app}",
            "execution_mode": "approximate" if approximate else "exact",
            "value_stats": {
                "min": _control_float((aggregate or (0, 0, 0))[0] or 0),
                "max": _control_float((aggregate or (0, 0, 0))[1] or 0),
                "mean": _control_float((aggregate or (0, 0, 0))[2] or 0),
            },
            "top_nodes": top,
            "truncated": count > len(top),
            "approximate": approximate,
            "sample_sources": source_count,
            "approximation_reasons": [
                "GraphScope FLASH 介数实现与标准归一化 Brandes 语义不同",
                *(
                    ["GraphScope FLASH Brandes 仅采样部分源点"]
                    if source_count < node_count
                    else []
                ),
            ],
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_pagerank(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    limit: int,
) -> dict[str, Any]:
    return adapt_centrality(
        connection,
        native_paths,
        result_root,
        kind="pagerank",
        native_app="pagerank_nx",
        limit=limit,
    )


def adapt_distances(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    source: str,
    weighted: bool,
    native_app: str,
    limit: int,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type=native_app.upper())
    node_count = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    if weighted:
        connection.execute("DELETE FROM values_by_node WHERE value<0 OR value>=1e300")
    else:
        connection.execute(
            "DELETE FROM values_by_node WHERE value<0 OR value>=?", (max(1, node_count),)
        )
    aggregate = connection.execute(
        """
        SELECT COUNT(*),AVG(value),MIN(value),MAX(value)
        FROM values_by_node WHERE node_id<>?
        """,
        (source,),
    ).fetchone()
    reachable = int(aggregate[0] or 0)
    nearest = [
        {"target": str(name), "distance": float(distance)}
        for name, distance in connection.execute(
            """
            SELECT n.name,v.value FROM values_by_node v
            JOIN nodes n ON n.node_id=v.node_id
            WHERE v.node_id<>? ORDER BY v.value,n.node_id LIMIT ?
            """,
            (source, limit),
        )
    ]

    def rows() -> Iterator[dict[str, Any]]:
        for name, distance in connection.execute(
            """
            SELECT n.name,v.value FROM values_by_node v
            JOIN nodes n ON n.node_id=v.node_id
            WHERE v.node_id<>? ORDER BY v.value,n.node_id
            """,
            (source,),
        ):
            yield {"target": str(name), "distance": float(distance)}

    output = result_root / "shortest-distances.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "found": reachable > 0,
            "source": _display(connection, source),
            "weighted": weighted,
            "result_detail": "distances",
            "reachable_count": reachable,
            "avg_distance": round(float(aggregate[1] or 0), 4),
            "min_distance": round(float(aggregate[2] or 0), 4),
            "max_distance": round(float(aggregate[3] or 0), 4),
            "nearest_distances": nearest,
            "truncated": reachable > len(nearest),
            "execution_algorithm": f"graphscope_{native_app}",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_bfs(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    source: str,
    limit: int,
) -> dict[str, Any]:
    return adapt_distances(
        connection,
        native_paths,
        result_root,
        source=source,
        weighted=False,
        native_app="bfs",
        limit=limit,
    )


def _edge_rows(
    manifest: dict[str, Any], snapshot_root: Path
) -> Iterator[tuple[str, str, float]]:
    for relative in manifest["edge_files"]:
        with (snapshot_root / str(relative)).open("r", encoding="utf-8") as source:
            for line in source:
                source_id, target_id, weight, _ = decode_edge_row(line)
                yield source_id, target_id, _safe_float(weight)


def _load_snapshot_edges(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    snapshot_root: Path,
) -> None:
    """Stage edges once for set-based result adaptation without RAM growth."""

    connection.execute(
        """
        CREATE TABLE snapshot_edges(
            ordinal INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            weight REAL NOT NULL
        )
        """
    )
    batch: list[tuple[int, str, str, float]] = []
    ordinal = 0
    for source, target, weight in _edge_rows(manifest, snapshot_root):
        batch.append((ordinal, source, target, weight))
        ordinal += 1
        if len(batch) >= 4096:
            connection.executemany(
                "INSERT INTO snapshot_edges(ordinal,source,target,weight) VALUES(?,?,?,?)",
                batch,
            )
            batch.clear()
    if batch:
        connection.executemany(
            "INSERT INTO snapshot_edges(ordinal,source,target,weight) VALUES(?,?,?,?)",
            batch,
        )
    connection.commit()


def _load_labels(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    *,
    value_type: str,
) -> None:
    connection.execute(
        "CREATE TABLE labels_by_node(node_id TEXT PRIMARY KEY,label TEXT NOT NULL) WITHOUT ROWID"
    )
    batch: list[tuple[str, str]] = []
    seen = 0
    for node_id, label in _native_label_rows(native_paths):
        batch.append((node_id, label))
        if len(batch) >= 4096:
            try:
                connection.executemany(
                    "INSERT INTO labels_by_node(node_id,label) VALUES(?,?)", batch
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"GraphScope {value_type} 结果包含重复节点") from exc
            seen += len(batch)
            batch.clear()
    if batch:
        try:
            connection.executemany(
                "INSERT INTO labels_by_node(node_id,label) VALUES(?,?)", batch
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"GraphScope {value_type} 结果包含重复节点") from exc
        seen += len(batch)
    expected = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    unknown = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM labels_by_node v
            LEFT JOIN nodes n ON n.node_id=v.node_id WHERE n.node_id IS NULL
            """
        ).fetchone()[0]
    )
    if unknown or seen != expected:
        raise ValueError(
            f"GraphScope {value_type} 结果节点覆盖不完整(expected={expected}, actual={seen}, unknown={unknown})"
        )
    connection.commit()


def _community_modularity(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    snapshot_root: Path,
) -> float | None:
    connection.execute(
        """
        CREATE TABLE community_stats(
            label TEXT PRIMARY KEY,
            degree REAL NOT NULL DEFAULT 0,
            internal_weight REAL NOT NULL DEFAULT 0
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        "INSERT INTO community_stats(label) SELECT label FROM labels_by_node GROUP BY label"
    )
    _load_snapshot_edges(connection, manifest, snapshot_root)
    missing_endpoints = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM snapshot_edges e
            LEFT JOIN labels_by_node s ON s.node_id=e.source
            LEFT JOIN labels_by_node t ON t.node_id=e.target
            WHERE s.node_id IS NULL OR t.node_id IS NULL
            """
        ).fetchone()[0]
    )
    if missing_endpoints:
        raise ValueError("GraphScope 社区结果无法覆盖快照边端点")
    connection.executescript(
        """
        CREATE TABLE community_degree(label TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
        INSERT INTO community_degree(label,value)
        SELECT label,SUM(weight) FROM (
            SELECT s.label AS label,MAX(0,e.weight) AS weight
            FROM snapshot_edges e JOIN labels_by_node s ON s.node_id=e.source
            UNION ALL
            SELECT t.label AS label,MAX(0,e.weight) AS weight
            FROM snapshot_edges e JOIN labels_by_node t ON t.node_id=e.target
        ) GROUP BY label;
        UPDATE community_stats SET degree=COALESCE(
            (SELECT value FROM community_degree d WHERE d.label=community_stats.label),0
        );
        CREATE TABLE community_internal(label TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
        INSERT INTO community_internal(label,value)
        SELECT s.label,SUM(MAX(0,e.weight)) FROM snapshot_edges e
        JOIN labels_by_node s ON s.node_id=e.source
        JOIN labels_by_node t ON t.node_id=e.target
        WHERE s.label=t.label GROUP BY s.label;
        UPDATE community_stats SET internal_weight=COALESCE(
            (SELECT value FROM community_internal i WHERE i.label=community_stats.label),0
        );
        """
    )
    edge_weight = float(
        connection.execute(
            "SELECT COALESCE(SUM(MAX(0,weight)),0) FROM snapshot_edges"
        ).fetchone()[0]
    )
    if edge_weight <= 0:
        return None
    return round(
        sum(
            float(internal) / edge_weight
            - (float(degree) / (2.0 * edge_weight)) ** 2
            for degree, internal in connection.execute(
                "SELECT degree,internal_weight FROM community_stats"
            )
        ),
        6,
    )


def adapt_community(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    manifest: dict[str, Any],
    snapshot_root: Path,
    algorithm: str,
    native_app: str,
    parameters: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    _load_labels(connection, native_paths, value_type=native_app)
    connection.execute(
        "CREATE TABLE community_sizes(label TEXT PRIMARY KEY,size INTEGER NOT NULL) WITHOUT ROWID"
    )
    connection.execute(
        "INSERT INTO community_sizes(label,size) SELECT label,COUNT(*) FROM labels_by_node GROUP BY label"
    )
    connection.commit()
    groups = list(
        connection.execute(
            "SELECT label,size FROM community_sizes ORDER BY size DESC,label LIMIT ?",
            (limit,),
        )
    )
    community_count = int(
        connection.execute("SELECT COUNT(*) FROM community_sizes").fetchone()[0]
    )
    aggregate = connection.execute(
        "SELECT MIN(size),MAX(size),AVG(size) FROM community_sizes"
    ).fetchone()
    median_offset = max(0, (community_count - 1) // 2)
    median_take = 2 if community_count and community_count % 2 == 0 else 1
    median_values = [
        float(row[0])
        for row in connection.execute(
            "SELECT size FROM community_sizes ORDER BY size LIMIT ? OFFSET ?",
            (median_take, median_offset),
        )
    ]
    communities: list[dict[str, Any]] = []
    for community_id, (label, size) in enumerate(groups):
        names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT n.name FROM labels_by_node l JOIN nodes n ON n.node_id=l.node_id
                WHERE l.label=? ORDER BY n.ordinal LIMIT 10
                """,
                (str(label),),
            )
        ]
        communities.append(
            {
                "community_id": community_id,
                "size": int(size),
                "nodes_preview": names,
                "nodes_truncated": int(size) > len(names),
            }
        )
    focus: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in parameters.get("focus_nodes") or []:
        try:
            node_id = _resolve_source(connection, manifest, raw)
        except ValueError:
            missing.append(str(raw))
            continue
        row = connection.execute(
            """
            SELECT l.label,s.size FROM labels_by_node l JOIN community_sizes s ON s.label=l.label
            WHERE l.node_id=?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            missing.append(str(raw))
            continue
        label, size = str(row[0]), int(row[1])
        representative = [
            str(item[0])
            for item in connection.execute(
                """
                SELECT n.name FROM labels_by_node l JOIN nodes n ON n.node_id=l.node_id
                WHERE l.label=? ORDER BY n.ordinal LIMIT 10
                """,
                (label,),
            )
        ]
        focus.append(
            {
                "node_id": node_id,
                "node": _display(connection, node_id),
                "community_id": label,
                "community_size": size,
                "representative_nodes": representative,
            }
        )

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, label in connection.execute(
            """
            SELECT n.node_id,n.name,l.label FROM nodes n
            JOIN labels_by_node l ON l.node_id=n.node_id
            ORDER BY l.label,n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "community": str(label)}

    output = result_root / "communities.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "algorithm": algorithm,
            "execution_algorithm": f"graphscope_{native_app}",
            "execution_mode": "heuristic",
            "approximate": True,
            "num_communities": community_count,
            "modularity": (
                None
                if native_app == "flash_fluid_community"
                else _community_modularity(connection, manifest, snapshot_root)
            ),
            "size_stats": {
                "min": float((aggregate or (0, 0, 0))[0] or 0),
                "max": float((aggregate or (0, 0, 0))[1] or 0),
                "mean": round(float((aggregate or (0, 0, 0))[2] or 0), 6),
                "median": round(sum(median_values) / len(median_values), 6)
                if median_values
                else 0.0,
            },
            "communities_shown": len(communities),
            "communities_truncated": community_count > len(communities),
            "communities": communities,
            "focus_node_communities": focus,
            "missing_focus_nodes": missing,
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_average_clustering(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type="clustering")
    value = float(
        connection.execute("SELECT COALESCE(AVG(value),0) FROM values_by_node").fetchone()[0]
    )

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, score in connection.execute(
            """
            SELECT n.node_id,n.name,v.value FROM nodes n
            JOIN values_by_node v ON v.node_id=n.node_id ORDER BY n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "value": float(score)}

    output = result_root / "clustering.jsonl"
    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "metric": "average_clustering",
            "value": round(value, 6),
            "defined": True,
            "execution_algorithm": "graphscope_clustering",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_transitivity(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    manifest: dict[str, Any],
    snapshot_root: Path,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type="triangles")
    connection.execute(
        "CREATE TABLE degrees(node_id TEXT PRIMARY KEY,value INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID"
    )
    connection.execute("INSERT INTO degrees(node_id) SELECT node_id FROM nodes")
    _load_snapshot_edges(connection, manifest, snapshot_root)
    connection.executescript(
        """
        CREATE TABLE degree_counts(node_id TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
        INSERT INTO degree_counts(node_id,value)
        SELECT node_id,COUNT(*) FROM (
            SELECT source AS node_id FROM snapshot_edges
            UNION ALL
            SELECT target AS node_id FROM snapshot_edges
        ) GROUP BY node_id;
        UPDATE degrees SET value=COALESCE(
            (SELECT value FROM degree_counts d WHERE d.node_id=degrees.node_id),0
        );
        """
    )
    triangle_sum = float(
        connection.execute("SELECT COALESCE(SUM(value),0) FROM values_by_node").fetchone()[0]
    )
    triplets = float(
        connection.execute(
            "SELECT COALESCE(SUM(value*(value-1)/2.0),0) FROM degrees"
        ).fetchone()[0]
    )
    value = triangle_sum / triplets if triplets else 0.0
    output = result_root / "triangles.jsonl"

    def rows() -> Iterator[dict[str, Any]]:
        for node_id, name, count in connection.execute(
            """
            SELECT n.node_id,n.name,v.value FROM nodes n
            JOIN values_by_node v ON v.node_id=n.node_id ORDER BY n.ordinal
            """
        ):
            yield {"node_id": str(node_id), "node": str(name), "triangles": int(count)}

    preview = _emit_jsonl(output, rows())
    return {
        "summary": {
            "success": True,
            "metric": "transitivity",
            "value": round(value, 6),
            "defined": True,
            "execution_algorithm": "graphscope_triangles",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def adapt_k_core(
    connection: sqlite3.Connection,
    native_paths: list[Path],
    result_root: Path,
    *,
    manifest: dict[str, Any],
    snapshot_root: Path,
    limit: int,
) -> dict[str, Any]:
    _load_values(connection, native_paths, value_type="k-core")
    connection.execute(
        "CREATE TABLE selected(node_id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute(
        "INSERT INTO selected(node_id) SELECT node_id FROM values_by_node WHERE value<>0"
    )
    connection.commit()
    node_count = int(connection.execute("SELECT COUNT(*) FROM selected").fetchone()[0])
    node_preview = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT n.name FROM nodes n JOIN selected s ON s.node_id=n.node_id
            ORDER BY n.ordinal LIMIT ?
            """,
            (limit,),
        )
    ]
    edge_preview: list[dict[str, Any]] = []
    edge_count = 0
    _load_snapshot_edges(connection, manifest, snapshot_root)
    output = result_root / "subgraph-k_core.jsonl"
    preview: list[dict[str, Any]] = []
    with output.open("w", encoding="utf-8") as target:
        for node_id, name in connection.execute(
            """
            SELECT n.node_id,n.name FROM nodes n JOIN selected s ON s.node_id=n.node_id
            ORDER BY n.ordinal
            """
        ):
            row = {"kind": "node", "node_id": str(node_id), "node": str(name)}
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            if len(preview) < 20:
                preview.append(row)
        for source, target_id, weight in connection.execute(
            """
            SELECT e.source,e.target,e.weight FROM snapshot_edges e
            JOIN selected s ON s.node_id=e.source
            JOIN selected t ON t.node_id=e.target ORDER BY e.ordinal
            """
        ):
            row = {
                "kind": "edge",
                "source_id": source,
                "target_id": target_id,
                "from": _display(connection, source),
                "to": _display(connection, target_id),
                "weight": weight,
            }
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            edge_count += 1
            if len(edge_preview) < limit:
                edge_preview.append({"from": row["from"], "to": row["to"], "weight": weight})
            if len(preview) < 20:
                preview.append(row)
    return {
        "summary": {
            "success": True,
            "method": "k_core",
            "num_nodes": node_count,
            "num_edges": edge_count,
            "nodes_preview": node_preview,
            "nodes_truncated": node_count > len(node_preview),
            "edges_preview": edge_preview,
            "edges_truncated": edge_count > len(edge_preview),
            "edge_scan_strategy": "partition-edge-scan",
            "execution_algorithm": "graphscope_k_core",
            "execution_mode": "exact",
        },
        "preview": preview,
        "result_files": [output.name],
    }


def _write_graph_csv(
    manifest: dict[str, Any],
    snapshot_root: Path,
    work_root: Path,
    *,
    require_nonnegative_weights: bool,
    skip_self_loops: bool = False,
) -> tuple[Path, Path]:
    vertices = work_root / "vertices.csv"
    edges = work_root / "edges.csv"
    with vertices.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["id"])
        order_path = snapshot_root / str(manifest["node_order_file"])
        with order_path.open("r", encoding="utf-8") as source:
            for line in source:
                writer.writerow([decode_node_row(line)[0]])
    with edges.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["source", "target", "weight"])
        for source, target, weight in _edge_rows(manifest, snapshot_root):
            if skip_self_loops and source == target:
                continue
            if require_nonnegative_weights and weight < 0:
                raise ValueError("GraphScope SSSP 仅支持非负边权")
            writer.writerow([source, target, weight])
    return vertices, edges


def _collect_native_paths(native_target: Path) -> list[Path]:
    candidates: list[Path] = []
    if native_target.is_file():
        candidates.append(native_target)
    if native_target.is_dir():
        candidates.extend(path for path in native_target.rglob("*") if path.is_file())
    candidates.extend(
        path
        for path in native_target.parent.glob(f"{native_target.name}*")
        if path.is_file() and path not in candidates
    )
    return sorted(path for path in candidates if path.stat().st_size > 0)


def _projected_edge_properties(native_app: str) -> list[str]:
    return ["weight"] if native_app in {"louvain", "sssp", "flash_msf"} else []


_FLASH_DIRECTED_LOADER_APPS = frozenset(
    {"flash_bcc", "flash_bridges", "flash_cut_point"}
)

_FLASH_EXTENDED_APPS = frozenset(
    {
        "flash_diameter_approximation",
        "flash_fluid_community",
        "flash_densest_subgraph",
        "flash_maximal_independent_set",
        "flash_minimal_dominating_set",
        "flash_minimal_vertex_cover",
        "flash_minimal_edge_cover",
        "flash_triangle_counting",
        "flash_rectangle_counting",
        "flash_k_clique_counting",
        "flash_diamond_counting",
        "flash_tailed_triangle_counting",
        "flash_three_path_counting",
        "flash_cycle_plus_triangle_counting",
        "flash_cyclic_triangle_counting",
        "flash_acyclic_triangle_counting",
        "flash_in_plus_triangle_counting",
        "flash_out_plus_triangle_counting",
    }
)

_FLASH_INDEPENDENT_COVER_APPS = {
    "flash_maximal_independent_set": "maximal_independent_set",
    "flash_minimal_dominating_set": "minimal_dominating_set",
    "flash_minimal_vertex_cover": "minimal_vertex_cover",
    "flash_minimal_edge_cover": "minimal_edge_cover",
}

_FLASH_MOTIF_APPS = {
    "flash_triangle_counting": ("triangle", "triangle_counting"),
    "flash_rectangle_counting": ("rectangle", "rectangle_counting"),
    "flash_k_clique_counting": ("k_clique", "k_clique_counting"),
    "flash_diamond_counting": ("diamond", "diamond_counting"),
    "flash_tailed_triangle_counting": (
        "tailed_triangle",
        "tailed_triangle_counting",
    ),
    "flash_three_path_counting": ("three_path", "three_path_counting"),
    "flash_cycle_plus_triangle_counting": (
        "cycle_plus_triangle",
        "cycle_plus_triangle_counting",
    ),
    "flash_cyclic_triangle_counting": (
        "cyclic_triangle",
        "cyclic_triangle_counting",
    ),
    "flash_acyclic_triangle_counting": (
        "acyclic_triangle",
        "acyclic_triangle_counting",
    ),
    "flash_in_plus_triangle_counting": (
        "in_plus_triangle",
        "in_plus_triangle_counting",
    ),
    "flash_out_plus_triangle_counting": (
        "out_plus_triangle",
        "out_plus_triangle_counting",
    ),
}


def _native_graph_directed(native_app: str, manifest: dict[str, Any]) -> bool:
    return bool(manifest.get("directed", True)) or native_app in _FLASH_DIRECTED_LOADER_APPS


def _scalar_context_value(context: Any) -> float:
    value: Any = context.to_numpy("r", axis=0)
    if hasattr(value, "tolist"):
        value = value.tolist()
    while isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("GraphScope 标量上下文为空")
        value = value[0]
    return _safe_float(value)


def _execute_native(
    *,
    native_app: str,
    manifest: dict[str, Any],
    snapshot_root: Path,
    work_root: Path,
    parameters: dict[str, Any],
    workers: int,
    source: str | None,
) -> tuple[list[Path], str, dict[str, Any]]:
    # Imported only in the isolated adapter image.  The main graph-data image
    # and every small-graph code path remain independent of GraphScope.
    import graphscope
    from graphscope.analytical.app import flash
    from graphscope.framework.loader import Loader

    vertices, edges = _write_graph_csv(
        manifest,
        snapshot_root,
        work_root,
        require_nonnegative_weights=native_app == "sssp",
        skip_self_loops=native_app in _FLASH_EXTENDED_APPS,
    )
    session = graphscope.session(
        cluster_type="hosts", num_workers=max(1, int(workers))
    )
    native_target = work_root / "native-output.csv"
    native_values: dict[str, Any] = {}
    try:
        graph = session.g(
            directed=_native_graph_directed(native_app, manifest),
            oid_type="string",
            generate_eid=True,
            retain_oid=True,
        )
        graph = graph.add_vertices(
            Loader(vertices.as_uri(), header_row=True),
            label="vertex",
            vid_field="id",
        )
        graph = graph.add_edges(
            Loader(edges.as_uri(), header_row=True),
            label="edge",
            src_label="vertex",
            dst_label="vertex",
            src_field="source",
            dst_field="target",
            properties=[("weight", "double")],
        )
        projected = graph.project(
            vertices={"vertex": []},
            edges={"edge": _projected_edge_properties(native_app)},
        )
        context: Any | None = None
        selector = {"node_id": "v.id", "value": "r"}
        if native_app == "wcc":
            context = graphscope.wcc(projected)
        elif native_app == "pagerank_nx":
            context = graphscope.pagerank_nx(
                projected,
                alpha=float(parameters.get("damping", 0.85)),
                max_iter=int(parameters.get("max_iterations", 100)),
                tol=float(parameters.get("tolerance", 1e-6)),
            )
        elif native_app == "bfs":
            if source is None:
                raise ValueError("GraphScope BFS 缺少规范化起点")
            context = graphscope.bfs(projected, src=source)
        elif native_app == "sssp":
            if source is None:
                raise ValueError("GraphScope SSSP 缺少规范化起点")
            context = graphscope.sssp(projected, src=source, weight="weight")
        elif native_app == "cdlp":
            context = graphscope.cdlp(
                projected,
                max_round=max(1, min(int(parameters.get("max_iterations", 10)), 500)),
            )
        elif native_app == "louvain":
            context = graphscope.louvain(projected)
        elif native_app == "clustering":
            context = graphscope.clustering(projected)
        elif native_app == "triangles":
            context = graphscope.triangles(projected)
        elif native_app == "k_core":
            context = graphscope.k_core(
                projected, k=max(1, int(parameters.get("k", 2)))
            )
        elif native_app == "eigenvector_centrality":
            context = graphscope.eigenvector_centrality(
                projected,
                tolerance=float(parameters.get("tolerance", 1e-6)),
                max_round=max(1, int(parameters.get("max_iterations", 100))),
            )
        elif native_app == "katz_centrality":
            context = graphscope.katz_centrality(
                projected,
                alpha=float(parameters.get("alpha", 0.1)),
                beta=float(parameters.get("beta", 1.0)),
                tolerance=float(parameters.get("tolerance", 1e-6)),
                max_round=max(1, int(parameters.get("max_iterations", 100))),
                normalized=True,
            )
        elif native_app == "voterank":
            context = graphscope.voterank(projected, num_of_nodes=0)
        elif native_app == "degree_centrality":
            centrality_type = str(parameters.get("centrality_direction", "both")).strip().lower()
            if centrality_type not in {"in", "out", "both"}:
                raise ValueError("centrality_direction 必须是 in、out 或 both")
            if not bool(manifest.get("directed", True)) and centrality_type == "both":
                centrality_type = "out"
            context = graphscope.degree_centrality(
                projected, centrality_type=centrality_type
            )
        elif native_app == "degree_assortativity_coefficient":
            native_values["value"] = float(
                graphscope.degree_assortativity_coefficient(
                    projected, x="out", y="in", weight=None
                )
            )
        elif native_app == "average_degree_connectivity":
            native_values["values"] = dict(
                graphscope.average_degree_connectivity(
                    projected, source="in+out", target="in+out", weight=None
                )
            )
        elif native_app == "average_shortest_path_length":
            native_values["value"] = float(
                graphscope.average_shortest_path_length(projected, weight=None)
            )
        elif native_app == "flash_betweenness":
            try:
                sample_cap = max(1, int(parameters.get("sample_sources", 64)))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("sample_sources 必须是正整数") from exc
            sources: list[str] = []
            with vertices.open("r", encoding="utf-8", newline="") as source_file:
                reader = csv.DictReader(source_file)
                for row in reader:
                    sources.append(str(row["id"]))
                    if len(sources) >= sample_cap:
                        break
            native_paths: list[Path] = []
            for index, sampled_source in enumerate(sources):
                target = work_root / f"native-betweenness-{index:05d}.csv"
                flash.betweenness_centrality(
                    projected, source=sampled_source
                ).output(target.as_uri(), selector=selector)
                native_paths.extend(_collect_native_paths(target))
            native_values.update(
                {
                    "source_count": len(sources),
                    "node_count": int(manifest.get("node_count", len(sources))),
                }
            )
            version = str(getattr(graphscope, "__version__", "unknown"))
            return sorted(set(native_paths)), version, native_values
        elif native_app == "flash_closeness":
            context = flash.closeness_centrality(projected)
        elif native_app == "flash_harmonic":
            context = flash.harmonic_centrality(projected)
        elif native_app == "flash_scc":
            context = flash.scc(projected)
        elif native_app == "flash_bcc":
            context = flash.bcc_2(projected)
        elif native_app == "flash_bridges":
            native_values["bridge_count"] = int(
                round(_scalar_context_value(flash.bridge_2(projected)))
            )
        elif native_app == "flash_cut_point":
            native_values["count"] = int(
                round(_scalar_context_value(flash.cut_point_2(projected)))
            )
        elif native_app == "flash_msf":
            native_values["total_weight"] = _scalar_context_value(
                flash.minimum_spanning_forest(projected)
            )
        elif native_app == "flash_color":
            context = flash.graph_coloring(projected)
        elif native_app == "flash_mm":
            native_values["count"] = int(
                round(_scalar_context_value(flash.maximal_matching(projected)))
            )
        elif native_app == "hits":
            context = graphscope.hits(
                projected,
                tolerance=float(parameters.get("tolerance", 1e-6)),
                max_round=max(1, int(parameters.get("max_iterations", 100))),
                normalized=bool(parameters.get("normalized", True)),
            )
            selector = {
                "node_id": "v.id",
                "hub": "r.hub",
                "authority": "r.auth",
            }
        elif native_app == "k_shell":
            context = graphscope.k_shell(projected, k=max(1, int(parameters["k"])))
        elif native_app == "flash_ppr":
            if source is None:
                raise ValueError("GraphScope 个性化 PageRank 缺少规范化起点")
            context = flash.personalized_pagerank(
                projected,
                source=source,
                max_round=max(1, int(parameters.get("max_iterations", 10))),
            )
        elif native_app == "flash_diameter_approximation":
            native_values["value"] = _scalar_context_value(
                flash.diameter_approximation(projected)
            )
        elif native_app == "flash_fluid_community":
            context = flash.fluid_community(projected)
        elif native_app == "flash_densest_subgraph":
            native_values["value"] = _scalar_context_value(
                flash.densest_subgraph_2_approximation(
                    projected,
                    d=max(1, min(int(parameters.get("rounds", 10)), 1000)),
                )
            )
        elif native_app in _FLASH_INDEPENDENT_COVER_APPS:
            native_values["count"] = int(
                round(
                    _scalar_context_value(
                        getattr(flash, _FLASH_INDEPENDENT_COVER_APPS[native_app])(
                            projected
                        )
                    )
                )
            )
        elif native_app in _FLASH_MOTIF_APPS:
            _, function_name = _FLASH_MOTIF_APPS[native_app]
            function = getattr(flash, function_name)
            context_value = (
                function(projected, k=max(2, min(int(parameters["k"]), 8)))
                if native_app == "flash_k_clique_counting"
                else function(projected)
            )
            native_values["count"] = int(
                round(_scalar_context_value(context_value))
            )
        else:
            raise ValueError(f"未允许的 GraphScope 原生算法: {native_app}")
        paths: list[Path] = []
        if context is not None:
            context.output(native_target.as_uri(), selector=selector)
            paths = _collect_native_paths(native_target)
        if context is not None and int(manifest.get("node_count", 0)) and not paths:
            raise ValueError("GraphScope 原生算法未生成结果文件")
        version = str(getattr(graphscope, "__version__", "unknown"))
        return paths, version, native_values
    finally:
        session.close()


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    native_app = str(payload.get("native_app") or "")
    allowed = {
        "wcc",
        "pagerank_nx",
        "bfs",
        "sssp",
        "cdlp",
        "louvain",
        "clustering",
        "triangles",
        "k_core",
        "eigenvector_centrality",
        "katz_centrality",
        "voterank",
        "degree_centrality",
        "degree_assortativity_coefficient",
        "average_degree_connectivity",
        "average_shortest_path_length",
        "flash_betweenness",
        "flash_closeness",
        "flash_harmonic",
        "flash_scc",
        "flash_bcc",
        "flash_bridges",
        "flash_cut_point",
        "flash_msf",
        "flash_color",
        "flash_mm",
        "hits",
        "k_shell",
        "flash_ppr",
        "flash_diameter_approximation",
        "flash_fluid_community",
        "flash_densest_subgraph",
        *_FLASH_INDEPENDENT_COVER_APPS,
        *_FLASH_MOTIF_APPS,
    }
    if native_app not in allowed:
        raise ValueError(f"未允许的 GraphScope 原生算法: {native_app!r}")
    manifest_path = Path(str(payload["manifest_path"])).resolve(strict=True)
    snapshot_root = manifest_path.parent
    result_root = Path(str(payload["result_root"])).resolve(strict=True)
    work_root = Path(str(payload["work_root"])).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("state") != "published":
        raise ValueError("GraphScope 只接受已发布的不可变快照")
    result_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    parameters = dict(payload.get("parameters") or {})
    connection = _open_state(work_root, manifest, snapshot_root)
    try:
        source = (
            _resolve_source(connection, manifest, parameters.get("source"))
            if native_app in {"bfs", "sssp", "flash_ppr"}
            else None
        )
        node_count = int(manifest.get("node_count", 0))
        if node_count:
            native_paths, graphscope_version, native_values = _execute_native(
                native_app=native_app,
                manifest=manifest,
                snapshot_root=snapshot_root,
                work_root=work_root,
                parameters=parameters,
                workers=int(payload.get("workers") or 1),
                source=source,
            )
        else:
            empty = work_root / "empty-native.csv"
            empty.write_text(
                "node_id,hub,authority\n"
                if native_app == "hits"
                else "node_id,value\n",
                encoding="utf-8",
            )
            native_paths, graphscope_version = [empty], "not-started-empty-graph"
            native_values = {
                "value": float("nan"),
                "values": {},
                "count": 0,
                "bridge_count": 0,
                "total_weight": 0.0,
                "source_count": 0,
                "node_count": 0,
            }
        raw_limit = parameters.get("limit", 20)
        limit = (
            200
            if isinstance(raw_limit, str) and raw_limit.strip().lower() == "all"
            else max(1, min(int(raw_limit or 20), 200))
        )
        if native_app == "wcc":
            result = adapt_wcc(connection, native_paths, result_root, limit=limit)
        elif native_app == "pagerank_nx":
            result = adapt_pagerank(
                connection, native_paths, result_root, limit=limit
            )
        elif native_app in {"bfs", "sssp"}:
            assert source is not None
            result = adapt_distances(
                connection,
                native_paths,
                result_root,
                source=source,
                weighted=native_app == "sssp",
                native_app=native_app,
                limit=limit,
            )
        elif native_app in {"cdlp", "louvain"}:
            result = adapt_community(
                connection,
                native_paths,
                result_root,
                manifest=manifest,
                snapshot_root=snapshot_root,
                algorithm=str(parameters.get("algorithm", "louvain")).strip().lower(),
                native_app=native_app,
                parameters=parameters,
                limit=limit,
            )
        elif native_app == "clustering":
            result = adapt_average_clustering(
                connection, native_paths, result_root
            )
        elif native_app == "triangles":
            result = adapt_transitivity(
                connection,
                native_paths,
                result_root,
                manifest=manifest,
                snapshot_root=snapshot_root,
            )
        elif native_app == "k_core":
            result = adapt_k_core(
                connection,
                native_paths,
                result_root,
                manifest=manifest,
                snapshot_root=snapshot_root,
                limit=limit,
            )
        elif native_app == "degree_centrality":
            if str(payload.get("algorithm")) == "degree_centrality":
                result = adapt_degree_score(
                    connection,
                    native_paths,
                    result_root,
                    native_app=native_app,
                    limit=limit,
                )
            else:
                result = adapt_centrality(
                    connection,
                    native_paths,
                    result_root,
                    kind="degree",
                    native_app=native_app,
                    limit=limit,
                )
        elif native_app == "degree_assortativity_coefficient":
            result = adapt_scalar_result(
                algorithm="global_metrics",
                native_app=native_app,
                value=native_values["value"],
                extra={"metric": "degree_assortativity"},
            )
        elif native_app == "average_degree_connectivity":
            result = adapt_average_degree_connectivity(
                result_root, dict(native_values["values"]), limit=limit
            )
        elif native_app == "average_shortest_path_length":
            result = adapt_scalar_result(
                algorithm="path_analysis",
                native_app=native_app,
                value=native_values["value"],
                extra={
                    "method": "average_shortest_path_length",
                    "scope": "full_graph",
                },
            )
        elif native_app == "flash_betweenness":
            result = adapt_flash_betweenness(
                connection,
                native_paths,
                result_root,
                source_count=int(native_values["source_count"]),
                node_count=int(native_values["node_count"]),
                native_app=native_app,
                limit=limit,
            )
        elif native_app in {"flash_closeness", "flash_harmonic"}:
            kind = "closeness" if native_app == "flash_closeness" else "harmonic"
            result = adapt_centrality(
                connection,
                native_paths,
                result_root,
                kind=kind,
                native_app=native_app,
                limit=limit,
                approximate=True,
                approximation_reasons=[
                    "GraphScope FLASH 使用固定 500 个随机源点估计距离中心性"
                ],
                value_transform=(
                    "reciprocal"
                    if native_app == "flash_closeness"
                    else None
                ),
            )
        elif native_app in {"flash_scc", "flash_bcc"}:
            algorithm = (
                "strongly_connected_components"
                if native_app == "flash_scc"
                else "biconnected_components"
            )
            result = adapt_component_labels(
                connection,
                native_paths,
                result_root,
                algorithm=algorithm,
                native_app=native_app,
                limit=limit,
            )
        elif native_app == "flash_bridges":
            result = adapt_scalar_result(
                algorithm="bridges",
                native_app=native_app,
                value=int(native_values["bridge_count"]),
                field="count",
                extra={
                    "materialized": False,
                    "note": "GraphScope FLASH 0.29 原生上下文仅返回桥数量",
                },
            )
        elif native_app == "flash_cut_point":
            result = adapt_scalar_result(
                algorithm="articulation_points",
                native_app=native_app,
                value=int(native_values["count"]),
                field="count",
                extra={
                    "materialized": False,
                    "note": "GraphScope FLASH 0.29 原生上下文仅返回割点数量",
                },
            )
        elif native_app == "flash_msf":
            result = adapt_scalar_result(
                algorithm="minimum_spanning_forest",
                native_app=native_app,
                value=native_values["total_weight"],
                field="total_weight",
                extra={
                    "materialized": False,
                    "note": "GraphScope FLASH 0.29 原生上下文仅返回生成森林总权重",
                },
            )
        elif native_app == "flash_color":
            result = adapt_graph_coloring(
                connection, native_paths, result_root, limit=limit
            )
        elif native_app == "flash_mm":
            result = adapt_scalar_result(
                algorithm="maximal_matching",
                native_app=native_app,
                value=int(native_values["count"]),
                field="count",
                extra={
                    "materialized": False,
                    "note": "GraphScope FLASH 0.29 原生上下文仅返回匹配边对数量",
                },
            )
        elif native_app == "hits":
            result = adapt_hits(
                connection, native_paths, result_root, limit=limit
            )
        elif native_app == "k_shell":
            result = adapt_k_shell(
                connection,
                native_paths,
                result_root,
                k=max(1, int(parameters["k"])),
                limit=limit,
            )
        elif native_app == "flash_ppr":
            result = adapt_centrality(
                connection,
                native_paths,
                result_root,
                kind="personalized_pagerank",
                native_app=native_app,
                limit=limit,
                approximate=True,
                approximation_reasons=[
                    "GraphScope FLASH PPR 使用固定 0.5 重启概率和有限轮次"
                ],
            )
        elif native_app == "flash_diameter_approximation":
            result = adapt_scalar_result(
                algorithm="approximate_diameter",
                native_app=native_app,
                value=native_values["value"],
                field="diameter",
                approximate=True,
                extra={
                    "approximation": "GraphScope FLASH diameter approximation",
                },
            )
        elif native_app == "flash_fluid_community":
            result = adapt_community(
                connection,
                native_paths,
                result_root,
                manifest=manifest,
                snapshot_root=snapshot_root,
                algorithm="fluid_community",
                native_app=native_app,
                parameters=parameters,
                limit=limit,
            )
        elif native_app == "flash_densest_subgraph":
            result = adapt_scalar_result(
                algorithm="densest_subgraph",
                native_app=native_app,
                value=native_values["value"],
                field="density",
                approximate=True,
                extra={
                    "rounds": max(1, min(int(parameters.get("rounds", 10)), 1000)),
                    "approximation": "GraphScope FLASH 2-approximation",
                    "materialized": False,
                },
            )
        elif native_app in _FLASH_INDEPENDENT_COVER_APPS:
            operation = _FLASH_INDEPENDENT_COVER_APPS[native_app]
            result = adapt_scalar_result(
                algorithm="independent_set_cover",
                native_app=native_app,
                value=native_values["count"],
                field="count",
                extra={
                    "operation": operation,
                    "materialized": False,
                    "note": "GraphScope FLASH 0.29 原生上下文仅返回集合或覆盖的大小",
                },
            )
        elif native_app in _FLASH_MOTIF_APPS:
            motif, _ = _FLASH_MOTIF_APPS[native_app]
            result = adapt_scalar_result(
                algorithm="motif_counting",
                native_app=native_app,
                value=native_values["count"],
                field="count",
                extra={
                    "motif": motif,
                    **(
                        {"k": max(2, min(int(parameters["k"]), 8))}
                        if motif == "k_clique"
                        else {}
                    ),
                },
            )
        else:
            kinds = {
                "eigenvector_centrality": "eigenvector",
                "katz_centrality": "katz",
                "voterank": "voterank",
            }
            result = adapt_centrality(
                connection,
                native_paths,
                result_root,
                kind=kinds[native_app],
                native_app=native_app,
                limit=limit,
                positive_only=native_app == "voterank",
            )
        if native_app in _FLASH_EXTENDED_APPS:
            result["summary"].setdefault(
                "graph_semantics",
                {
                    "topology": "simple_graph",
                    "directed": bool(manifest.get("directed", True)),
                    "self_loops": "ignored",
                    "edge_weights": "ignored",
                },
            )
        result["summary"].setdefault(
            "parallel",
            {
                "strategy": "graphscope-native-fragment-parallel",
                "workers": max(1, int(payload.get("workers") or 1)),
            },
        )
        result["summary"].setdefault(
            "result_processing",
            {
                "strategy": "graphscope-context-output/sqlite-stream-adapt",
                "workers": 1,
            },
        )
        result["backend_metadata"] = {
            "engine": "graphscope",
            "engine_version": graphscope_version,
            "adapter_version": ADAPTER_VERSION,
            "graph_cache_hit": False,
        }
        return result
    finally:
        connection.close()

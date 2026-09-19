"""Disk-backed kernels for dev's grouped graph-analysis operators.

The public tools group 55 concrete modes under five names.  The routines in
this mixin keep graph-sized mutable state in SQLite and stream complete row
results through ``LargeGraphAlgorithms._write_rows``.  A few algorithms whose
exact general-graph implementation is intrinsically memory-heavy (planarity,
weighted blossom matching, equitable colouring) use a deliberately bounded
native NetworkX path and switch to an explicitly labelled disk approximation
instead of silently materialising an unbounded snapshot.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import random
import sqlite3
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Iterator


_PREVIEW_LIMIT = 20
_MAX_LIMIT = 200
_NATIVE_NODE_LIMIT = 50_000
_NATIVE_EDGE_LIMIT = 250_000


def _bounded_limit(parameters: dict[str, Any], default: int = _PREVIEW_LIMIT) -> int:
    raw = parameters.get("limit", default)
    if isinstance(raw, str) and raw.strip().lower() == "all":
        return _MAX_LIMIT
    try:
        return max(1, min(int(raw), _MAX_LIMIT))
    except (TypeError, ValueError, OverflowError):
        return default


def _native_limits() -> tuple[int, int]:
    try:
        nodes = max(1, int(os.getenv("LARGE_GRAPH_NATIVE_EXACT_NODE_LIMIT", _NATIVE_NODE_LIMIT)))
    except (TypeError, ValueError, OverflowError):
        nodes = _NATIVE_NODE_LIMIT
    try:
        edges = max(1, int(os.getenv("LARGE_GRAPH_NATIVE_EXACT_EDGE_LIMIT", _NATIVE_EDGE_LIMIT)))
    except (TypeError, ValueError, OverflowError):
        edges = _NATIVE_EDGE_LIMIT
    return nodes, edges


class GroupedAnalysisMixin:
    """Mixin consumed by :class:`LargeGraphAlgorithms`.

    The host class supplies ``graph``, ``_connection``, ``_component_state``,
    ``_shortest_state``, ``_reachability_state``, ``_undirected_projection``,
    ``_union_find``, ``_write_rows`` and display helpers.
    """

    graph: Any
    work_root: Path

    @staticmethod
    def _group_result(
        summary: dict[str, Any],
        preview: list[Any] | None = None,
        result_files: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "summary": summary,
            "preview": list(preview or []),
            "result_files": list(result_files or []),
        }

    def _native_undirected_graph(self) -> Any | None:
        """Build a bounded exact NetworkX graph, never an unbounded fallback."""
        node_limit, edge_limit = _native_limits()
        if self.graph.node_count > node_limit or self.graph.edge_count > edge_limit:
            return None
        import networkx as nx

        graph = nx.Graph()
        graph.add_nodes_from(self.graph.iter_node_ids())
        for source, target, weight, _ in self.graph.iter_edges():
            graph.add_edge(source, target, weight=float(weight))
        return graph

    def _topological_state(self, stem: str) -> tuple[sqlite3.Connection, bool]:
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS grouped_indegree;
            DROP TABLE IF EXISTS grouped_ready;
            DROP TABLE IF EXISTS grouped_ordering;
            CREATE TABLE grouped_indegree(
                node TEXT PRIMARY KEY,value INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE grouped_ready(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE grouped_ordering(
                position INTEGER PRIMARY KEY,node TEXT NOT NULL UNIQUE
            );
            """
        )
        connection.executemany(
            "INSERT INTO grouped_indegree(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        for _source, target, _weight, _relation in self.graph.iter_edges():
            connection.execute(
                "UPDATE grouped_indegree SET value=value+1 WHERE node=?", (target,)
            )
        connection.execute(
            "INSERT INTO grouped_ready(node) "
            "SELECT node FROM grouped_indegree WHERE value=0"
        )
        position = 0
        while True:
            row = connection.execute(
                "SELECT node FROM grouped_ready ORDER BY node LIMIT 1"
            ).fetchone()
            if row is None:
                break
            node = str(row[0])
            connection.execute("DELETE FROM grouped_ready WHERE node=?", (node,))
            connection.execute(
                "INSERT INTO grouped_ordering(position,node) VALUES (?,?)",
                (position, node),
            )
            position += 1
            for neighbor, _weight in self.graph.iter_out_edges(node):
                connection.execute(
                    "UPDATE grouped_indegree SET value=value-1 WHERE node=?",
                    (neighbor,),
                )
                value = connection.execute(
                    "SELECT value FROM grouped_indegree WHERE node=?", (neighbor,)
                ).fetchone()
                if value is not None and int(value[0]) == 0:
                    connection.execute(
                        "INSERT OR IGNORE INTO grouped_ready(node) VALUES (?)",
                        (neighbor,),
                    )
            if position % 1000 == 0:
                connection.commit()
        connection.commit()
        return connection, position == self.graph.node_count

    def _disk_traversal(
        self,
        source: str,
        *,
        depth_limit: int | None,
        depth_first: bool,
        stem: str,
    ) -> sqlite3.Connection:
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS traversal_seen;
            DROP TABLE IF EXISTS traversal_pending;
            CREATE TABLE traversal_seen(
                node TEXT PRIMARY KEY,parent TEXT,depth INTEGER NOT NULL,
                ordinal INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE traversal_pending(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT NOT NULL,
                parent TEXT,depth INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO traversal_seen(node,parent,depth,ordinal) VALUES (?,?,0,0)",
            (source, None),
        )
        connection.execute(
            "INSERT INTO traversal_pending(node,parent,depth) VALUES (?,?,0)",
            (source, None),
        )
        ordinal = 1
        while True:
            order = "DESC" if depth_first else "ASC"
            row = connection.execute(
                f"SELECT seq,node,parent,depth FROM traversal_pending "
                f"ORDER BY seq {order} LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM traversal_pending WHERE seq=?", (int(row[0]),))
            node, parent, depth = str(row[1]), row[2], int(row[3])
            if depth_limit is not None and depth >= depth_limit:
                continue
            if depth_first:
                # Reverse iteration keeps DFS deterministic without retaining
                # a high-degree adjacency list in Python memory.
                neighbors = self.graph.iter_out_edges(node, reverse=True)
            else:
                neighbors = self.graph.iter_out_edges(node)
            for neighbor, _weight in neighbors:
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO traversal_seen(node,parent,depth,ordinal) "
                    "VALUES (?,?,?,?)",
                    (neighbor, node, depth + 1, ordinal),
                ).rowcount
                if inserted:
                    ordinal += 1
                    connection.execute(
                        "INSERT INTO traversal_pending(node,parent,depth) VALUES (?,?,?)",
                        (neighbor, node, depth + 1),
                    )
            if ordinal % 1000 == 0:
                connection.commit()
        connection.commit()
        return connection

    def _undirected_edge_count(self, connection: sqlite3.Connection) -> tuple[int, bool]:
        row = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(source=target),0) FROM projected_edges"
        ).fetchone()
        return int(row[0]), bool(row[1])

    def _is_adjacent_grouped(self, left: str, right: str) -> bool:
        return self.graph.edge_weight(left, right, undirected=True) is not None

    def _path_avoiding_closed_neighborhood(
        self, source: str, target: str, blocked: str, stem: str
    ) -> bool:
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS avoid_seen;
            DROP TABLE IF EXISTS avoid_queue;
            CREATE TABLE avoid_seen(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE avoid_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT);
            """
        )
        connection.execute("INSERT INTO avoid_seen(node) VALUES (?)", (source,))
        connection.execute("INSERT INTO avoid_queue(node) VALUES (?)", (source,))
        found = False
        while True:
            row = connection.execute(
                "SELECT seq,node FROM avoid_queue ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM avoid_queue WHERE seq=?", (int(row[0]),))
            node = str(row[1])
            if node == target:
                found = True
                break
            for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                if neighbor == blocked or self._is_adjacent_grouped(neighbor, blocked):
                    continue
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO avoid_seen(node) VALUES (?)", (neighbor,)
                ).rowcount
                if inserted:
                    connection.execute(
                        "INSERT INTO avoid_queue(node) VALUES (?)", (neighbor,)
                    )
        connection.close()
        return found

    def _is_at_free_disk(self) -> bool:
        # Definition-level exact kernel.  It is expensive (O(V^3) candidate
        # triples plus reachability), but all mutable search state is on disk.
        for left in self.graph.iter_node_ids():
            for middle in self.graph.iter_node_ids():
                if middle <= left or self._is_adjacent_grouped(left, middle):
                    continue
                for right in self.graph.iter_node_ids():
                    if right <= middle:
                        continue
                    if self._is_adjacent_grouped(left, right) or self._is_adjacent_grouped(middle, right):
                        continue
                    if not self._path_avoiding_closed_neighborhood(left, middle, right, "at-path-a"):
                        continue
                    if not self._path_avoiding_closed_neighborhood(left, right, middle, "at-path-b"):
                        continue
                    if self._path_avoiding_closed_neighborhood(middle, right, left, "at-path-c"):
                        return False
        return True

    def _bipartite_state(self, stem: str = "grouped-bipartite") -> tuple[sqlite3.Connection, bool]:
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS bipartite_color;
            DROP TABLE IF EXISTS bipartite_queue;
            CREATE TABLE bipartite_color(
                node TEXT PRIMARY KEY,color INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE bipartite_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT);
            """
        )
        valid = True
        for root in self.graph.iter_node_ids():
            if connection.execute(
                "SELECT 1 FROM bipartite_color WHERE node=?", (root,)
            ).fetchone():
                continue
            connection.execute(
                "INSERT INTO bipartite_color(node,color) VALUES (?,0)", (root,)
            )
            connection.execute("INSERT INTO bipartite_queue(node) VALUES (?)", (root,))
            while valid:
                row = connection.execute(
                    "SELECT seq,node FROM bipartite_queue ORDER BY seq LIMIT 1"
                ).fetchone()
                if row is None:
                    break
                connection.execute("DELETE FROM bipartite_queue WHERE seq=?", (int(row[0]),))
                node = str(row[1])
                color = int(connection.execute(
                    "SELECT color FROM bipartite_color WHERE node=?", (node,)
                ).fetchone()[0])
                for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                    found = connection.execute(
                        "SELECT color FROM bipartite_color WHERE node=?", (neighbor,)
                    ).fetchone()
                    if found is None:
                        connection.execute(
                            "INSERT INTO bipartite_color(node,color) VALUES (?,?)",
                            (neighbor, 1 - color),
                        )
                        connection.execute(
                            "INSERT INTO bipartite_queue(node) VALUES (?)", (neighbor,)
                        )
                    elif int(found[0]) == color:
                        valid = False
                        break
            if not valid:
                break
        connection.commit()
        return connection, valid

    def _wl_hash(self, iterations: int) -> str:
        connection = self._connection("grouped-wl-hash")
        connection.executescript(
            """
            DROP TABLE IF EXISTS wl_labels;
            DROP TABLE IF EXISTS wl_next;
            CREATE TABLE wl_labels(node TEXT PRIMARY KEY,label TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE wl_next(node TEXT PRIMARY KEY,label TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE wl_neighbor_labels(kind INTEGER NOT NULL,label TEXT NOT NULL);
            CREATE TABLE wl_hash_counts(
                round INTEGER,label TEXT,count INTEGER,
                PRIMARY KEY(round,label)
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO wl_labels(node,label) VALUES (?,?)",
            (
                (
                    node,
                    (
                        f"{self.graph.degree(node, direction='in')}_"
                        f"{self.graph.degree(node, direction='out')}"
                        if self.graph.directed
                        else str(self.graph.degree(node))
                    ),
                )
                for node in self.graph.iter_node_ids()
            ),
        )
        # NetworkX 3.5+ treats the degree initialisation as the first WL
        # iteration for unlabelled graphs.
        for current_round in range(max(0, iterations - 1)):
            connection.execute("DELETE FROM wl_next")
            for node in self.graph.iter_node_ids():
                own = str(connection.execute(
                    "SELECT label FROM wl_labels WHERE node=?", (node,)
                ).fetchone()[0])
                digest = hashlib.blake2b(digest_size=16)
                digest.update(own.encode("ascii"))
                connection.execute("DELETE FROM wl_neighbor_labels")
                if self.graph.directed:
                    for neighbor, _weight in self.graph.iter_out_edges(node):
                        row = connection.execute(
                            "SELECT label FROM wl_labels WHERE node=?", (neighbor,)
                        ).fetchone()
                        if row is not None:
                            connection.execute(
                                "INSERT INTO wl_neighbor_labels(kind,label) VALUES (0,?)",
                                ("s_" + str(row[0]),),
                            )
                    for neighbor, _weight in self.graph.iter_in_edges(node):
                        row = connection.execute(
                            "SELECT label FROM wl_labels WHERE node=?", (neighbor,)
                        ).fetchone()
                        if row is not None:
                            connection.execute(
                                "INSERT INTO wl_neighbor_labels(kind,label) VALUES (1,?)",
                                ("p_" + str(row[0]),),
                            )
                else:
                    for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                        row = connection.execute(
                            "SELECT label FROM wl_labels WHERE node=?", (neighbor,)
                        ).fetchone()
                        if row is not None:
                            connection.execute(
                                "INSERT INTO wl_neighbor_labels(kind,label) VALUES (0,?)",
                                (str(row[0]),),
                            )
                for (label,) in connection.execute(
                    "SELECT label FROM wl_neighbor_labels ORDER BY kind,label"
                ):
                    digest.update(str(label).encode("ascii"))
                connection.execute(
                    "INSERT INTO wl_next(node,label) VALUES (?,?)",
                    (node, digest.hexdigest()),
                )
            connection.execute("DELETE FROM wl_labels")
            connection.execute("INSERT INTO wl_labels SELECT * FROM wl_next")
            connection.execute(
                "INSERT INTO wl_hash_counts(round,label,count) "
                "SELECT ?,label,COUNT(*) FROM wl_labels GROUP BY label",
                (current_round,),
            )
            connection.commit()
        final = hashlib.blake2b(digest_size=16)
        rows = connection.execute(
            "SELECT label,count FROM wl_hash_counts ORDER BY round,label"
        )
        first = True
        final.update(b"(")
        for label, count in rows:
            if not first:
                final.update(b", ")
            final.update(f"('{label}', {int(count)})".encode("ascii"))
            first = False
        if not first and int(connection.execute(
            "SELECT COUNT(*) FROM wl_hash_counts"
        ).fetchone()[0]) == 1:
            final.update(b",")
        final.update(b")")
        connection.close()
        return final.hexdigest()

    def graph_structure_analysis(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        limit = _bounded_limit(p)
        known = {
            "is_tree", "is_forest", "is_directed_acyclic_graph", "bfs_tree",
            "dfs_tree", "lexicographical_topological_sort", "all_topological_sorts",
            "is_at_free", "check_planarity", "is_tournament", "score_sequence",
            "is_bipartite", "weisfeiler_lehman_graph_hash",
        }
        if method not in known:
            return self._group_result({"success": False, "error": f"未知方法 '{method}'"})

        if method in {"is_tree", "is_forest"}:
            if self.graph.node_count == 0:
                return self._group_result({
                    "success": False,
                    "code": "GRAPH_ALGORITHM_FAILED",
                    "error": "G has no nodes.",
                })
            projection = self._undirected_projection(f"grouped-{method}-projection")
            components = self._component_state(f"grouped-{method}-components", undirected=True)
            try:
                edges, has_loop = self._undirected_edge_count(projection)
                count = int(components.execute(
                    "SELECT COUNT(DISTINCT component) FROM components"
                ).fetchone()[0])
                forest = not has_loop and edges == self.graph.node_count - count
                value = forest and (self.graph.node_count > 0 and count == 1) if method == "is_tree" else forest
            finally:
                projection.close()
                components.close()
            return self._group_result({"success": True, "method": method, "value": bool(value)})

        if method == "is_directed_acyclic_graph":
            if not self.graph.directed:
                value = self.graph.node_count == 0
            else:
                state, value = self._topological_state("grouped-is-dag")
                state.close()
            return self._group_result({"success": True, "method": method, "value": bool(value)})

        if method in {"bfs_tree", "dfs_tree"}:
            source, error = self._required_node(p.get("source"), "节点")
            if error is not None:
                return self._group_result(error)
            raw_depth = p.get("depth_limit")
            depth_limit = None if raw_depth is None else max(0, int(raw_depth))
            state = self._disk_traversal(
                str(source), depth_limit=depth_limit,
                depth_first=method == "dfs_tree", stem=f"grouped-{method}",
            )
            try:
                def rows() -> Iterator[dict[str, Any]]:
                    for node, parent in state.execute(
                        "SELECT node,parent FROM traversal_seen WHERE parent IS NOT NULL ORDER BY ordinal"
                    ):
                        yield {"from": self._display(str(parent)), "to": self._display(str(node))}
                files, preview, edge_count = self._write_rows(f"{method}-edges", rows())
                node_count = int(state.execute("SELECT COUNT(*) FROM traversal_seen").fetchone()[0])
            finally:
                state.close()
            summary = {
                "success": True, "method": method, "source": self._display(str(source)),
                "num_nodes": node_count, "num_edges": edge_count,
                "edges_preview": preview[:limit], "truncated": edge_count > limit,
            }
            return self._group_result(summary, preview[:limit], files)

        if method == "lexicographical_topological_sort":
            if not self.graph.directed:
                return self._group_result({"success": False, "error": "该方法要求有向无环图（DAG）"})
            state, is_dag = self._topological_state("grouped-lex-topological")
            if not is_dag:
                state.close()
                return self._group_result({"success": False, "error": "该方法要求有向无环图（DAG）"})
            try:
                files, preview_rows, count = self._write_rows(
                    "lexicographical-topological-order",
                    ({"position": int(pos), "node": self._display(str(node))}
                     for pos, node in state.execute(
                         "SELECT position,node FROM grouped_ordering ORDER BY position"
                     )),
                )
            finally:
                state.close()
            order = [row["node"] for row in preview_rows[:limit]]
            return self._group_result(
                {"success": True, "method": method, "order": order,
                 "total_nodes": count, "truncated": count > len(order)},
                preview_rows, files,
            )

        if method == "all_topological_sorts":
            return self._all_topological_sorts(p)

        if method == "is_at_free":
            return self._group_result({
                "success": True, "method": method, "value": self._is_at_free_disk(),
                "execution_mode": "exact-disk-definition",
            })

        if method == "check_planarity":
            graph = self._native_undirected_graph()
            if graph is not None:
                import networkx as nx
                planar, _embedding = nx.check_planarity(graph)
                return self._group_result({
                    "success": True, "method": method, "is_planar": bool(planar),
                    "execution_mode": "bounded-native-exact", "exact": True,
                })
            # Euler bounds are exact non-planarity certificates.  An
            # inconclusive oversized graph remains machine-readable instead
            # of being optimistically labelled planar.
            projection = self._undirected_projection("grouped-planarity-projection")
            edges, has_loop = self._undirected_edge_count(projection)
            projection.close()
            bipartite_state, bipartite = self._bipartite_state("grouped-planarity-bipartite")
            bipartite_state.close()
            n = self.graph.node_count
            bound = (2 * n - 4) if bipartite and n >= 3 else (3 * n - 6 if n >= 3 else 1)
            if not has_loop and edges > bound:
                return self._group_result({
                    "success": True, "method": method, "is_planar": False,
                    "execution_mode": "disk-certificate", "exact": True,
                    "certificate": "euler-edge-bound",
                })
            return self._group_result({
                "success": False,
                "code": "PLANARITY_EXACT_LIMIT_EXCEEDED",
                "error": "图规模超过平面性精确内核上限，且外存 Euler 证书无法判定",
            })

        if method == "is_tournament":
            if not self.graph.directed:
                value = self.graph.node_count <= 1
            else:
                connection = self._connection("grouped-tournament")
                connection.execute(
                    "CREATE TABLE pairs(left_node TEXT,right_node TEXT,orientations INTEGER,"
                    "PRIMARY KEY(left_node,right_node)) WITHOUT ROWID"
                )
                has_loop = False
                for source, target, _weight, _relation in self.graph.iter_edges():
                    if source == target:
                        has_loop = True
                        continue
                    left, right = sorted((source, target))
                    connection.execute(
                        "INSERT INTO pairs(left_node,right_node,orientations) VALUES (?,?,1) "
                        "ON CONFLICT(left_node,right_node) DO UPDATE SET orientations=orientations+1",
                        (left, right),
                    )
                row = connection.execute(
                    "SELECT COUNT(*),COALESCE(MAX(orientations),0) FROM pairs"
                ).fetchone()
                value = (
                    not has_loop
                    and int(row[0]) == self.graph.node_count * (self.graph.node_count - 1) // 2
                    and int(row[1]) == 1
                )
                connection.close()
            return self._group_result({"success": True, "method": method, "value": bool(value)})

        if method == "score_sequence":
            if not self.graph.directed:
                return self._group_result({"success": False, "error": "score_sequence 要求有向图"})
            connection = self._connection("grouped-score-sequence")
            connection.execute("CREATE TABLE scores(node TEXT PRIMARY KEY,value INTEGER) WITHOUT ROWID")
            connection.executemany(
                "INSERT INTO scores(node,value) VALUES (?,?)",
                ((node, self.graph.degree(node, direction="out")) for node in self.graph.iter_node_ids()),
            )
            total = int(connection.execute("SELECT COALESCE(SUM(value),0) FROM scores").fetchone()[0])
            valid = total == self.graph.node_count * (self.graph.node_count - 1) // 2
            prefix = 0
            position = 0
            if valid:
                for (score,) in connection.execute("SELECT value FROM scores ORDER BY value,node"):
                    position += 1
                    prefix += int(score)
                    if int(score) < 0 or int(score) >= self.graph.node_count or prefix < position * (position - 1) // 2:
                        valid = False
                        break
            sequence = [int(row[0]) for row in connection.execute(
                "SELECT value FROM scores ORDER BY value DESC,node LIMIT ?", (limit,)
            )]
            connection.close()
            return self._group_result({
                "success": True, "method": method, "score_sequence": sequence,
                "total_nodes": self.graph.node_count,
                "truncated": self.graph.node_count > len(sequence),
                "is_tournament_score_sequence": valid,
            })

        if method == "is_bipartite":
            state, value = self._bipartite_state()
            state.close()
            return self._group_result({"success": True, "method": method, "value": value})

        iterations = max(1, int(p.get("iterations", 3)))
        return self._group_result({
            "success": True, "method": method, "iterations": iterations,
            "hash": self._wl_hash(iterations), "execution_mode": "exact-disk-iteration",
        })

    def _all_topological_sorts(self, p: dict[str, Any]) -> dict[str, Any]:
        if not self.graph.directed:
            return self._group_result({"success": False, "error": "该方法要求有向无环图（DAG）"})
        limit = _bounded_limit(p)
        state, is_dag = self._topological_state("grouped-all-topological-check")
        state.close()
        if not is_dag:
            return self._group_result({"success": False, "error": "该方法要求有向无环图（DAG）"})

        connection = self._connection("grouped-all-topological")
        connection.executescript(
            """
            CREATE TABLE indegree(node TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE used(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE ordering(depth INTEGER PRIMARY KEY,node TEXT NOT NULL);
            CREATE TABLE frames(depth INTEGER PRIMARY KEY,last_candidate TEXT NOT NULL);
            CREATE TABLE emitted(order_id INTEGER,position INTEGER,node TEXT,
                                 PRIMARY KEY(order_id,position)) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO indegree(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        for _source, target, _weight, _relation in self.graph.iter_edges():
            connection.execute("UPDATE indegree SET value=value+1 WHERE node=?", (target,))
        connection.execute("INSERT INTO frames(depth,last_candidate) VALUES (0,'')")
        depth = 0
        emitted = 0
        exhausted = False
        while emitted <= limit:
            if depth == self.graph.node_count:
                for position, node in connection.execute(
                    "SELECT depth,node FROM ordering ORDER BY depth"
                ):
                    connection.execute(
                        "INSERT INTO emitted(order_id,position,node) VALUES (?,?,?)",
                        (emitted, int(position), str(node)),
                    )
                emitted += 1
                if depth == 0:
                    exhausted = True
                    break
                depth -= 1
                selected = connection.execute(
                    "SELECT node FROM ordering WHERE depth=?", (depth,)
                ).fetchone()
                if selected is not None:
                    node = str(selected[0])
                    connection.execute("DELETE FROM ordering WHERE depth=?", (depth,))
                    connection.execute("DELETE FROM used WHERE node=?", (node,))
                    for neighbor, _weight in self.graph.iter_out_edges(node):
                        connection.execute("UPDATE indegree SET value=value+1 WHERE node=?", (neighbor,))
                continue

            frame = connection.execute(
                "SELECT last_candidate FROM frames WHERE depth=?", (depth,)
            ).fetchone()
            last = str(frame[0]) if frame else ""
            candidate = connection.execute(
                "SELECT node FROM indegree WHERE value=0 AND node>? "
                "AND node NOT IN (SELECT node FROM used) ORDER BY node LIMIT 1",
                (last,),
            ).fetchone()
            if candidate is not None:
                node = str(candidate[0])
                connection.execute("UPDATE frames SET last_candidate=? WHERE depth=?", (node, depth))
                connection.execute("INSERT INTO used(node) VALUES (?)", (node,))
                connection.execute("INSERT INTO ordering(depth,node) VALUES (?,?)", (depth, node))
                for neighbor, _weight in self.graph.iter_out_edges(node):
                    connection.execute("UPDATE indegree SET value=value-1 WHERE node=?", (neighbor,))
                depth += 1
                connection.execute(
                    "INSERT OR REPLACE INTO frames(depth,last_candidate) VALUES (?,'')", (depth,)
                )
                continue

            connection.execute("DELETE FROM frames WHERE depth=?", (depth,))
            if depth == 0:
                exhausted = True
                break
            depth -= 1
            selected = connection.execute(
                "SELECT node FROM ordering WHERE depth=?", (depth,)
            ).fetchone()
            if selected is not None:
                node = str(selected[0])
                connection.execute("DELETE FROM ordering WHERE depth=?", (depth,))
                connection.execute("DELETE FROM used WHERE node=?", (node,))
                for neighbor, _weight in self.graph.iter_out_edges(node):
                    connection.execute("UPDATE indegree SET value=value+1 WHERE node=?", (neighbor,))

        returned = min(emitted, limit)
        orders: list[list[str]] = []
        for order_id in range(returned):
            rows = [self._display(str(row[0])) for row in connection.execute(
                "SELECT node FROM emitted WHERE order_id=? ORDER BY position LIMIT ?",
                (order_id, _PREVIEW_LIMIT),
            )]
            orders.append(rows)

        def artifact_rows() -> Iterator[dict[str, Any]]:
            for order_id, position, node in connection.execute(
                "SELECT order_id,position,node FROM emitted WHERE order_id<? ORDER BY order_id,position",
                (returned,),
            ):
                yield {"order": int(order_id), "position": int(position), "node": self._display(str(node))}

        files, preview, _count = self._write_rows("all-topological-orders", artifact_rows())
        connection.close()
        return self._group_result({
            "success": True, "method": "all_topological_sorts", "orders": orders,
            "returned_count": returned, "truncated": not exhausted or emitted > limit,
            "hard_limit": _MAX_LIMIT,
            "order_previews_truncated": self.graph.node_count > _PREVIEW_LIMIT,
        }, preview, files)

    # ── Connectivity, cuts and biconnected structure ──────────────────

    @staticmethod
    def _flow_vertex(kind: str, node: str) -> str:
        return json.dumps([kind, node], ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _add_residual_arc(
        connection: sqlite3.Connection, source: str, target: str, capacity: int
    ) -> None:
        forward = int(connection.execute(
            "INSERT INTO residual_arcs(source,target,capacity,flow,reverse_id) "
            "VALUES (?,?,?,0,0) RETURNING id",
            (source, target, int(capacity)),
        ).fetchone()[0])
        reverse = int(connection.execute(
            "INSERT INTO residual_arcs(source,target,capacity,flow,reverse_id) "
            "VALUES (?,?,0,0,?) RETURNING id",
            (target, source, forward),
        ).fetchone()[0])
        connection.execute(
            "UPDATE residual_arcs SET reverse_id=? WHERE id=?", (reverse, forward)
        )

    def _unit_cut_state(
        self, source: str, target: str, *, vertex_cut: bool, stem: str
    ) -> tuple[sqlite3.Connection, int]:
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS residual_arcs;
            DROP TABLE IF EXISTS residual_parent;
            DROP TABLE IF EXISTS residual_queue;
            DROP TABLE IF EXISTS cut_reached;
            CREATE TABLE residual_arcs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,
                target TEXT NOT NULL,capacity INTEGER NOT NULL,flow INTEGER NOT NULL,
                reverse_id INTEGER NOT NULL
            );
            CREATE INDEX residual_source_idx ON residual_arcs(source,id);
            CREATE TABLE residual_parent(
                node TEXT PRIMARY KEY,arc_id INTEGER
            ) WITHOUT ROWID;
            CREATE TABLE residual_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT);
            CREATE TABLE cut_reached(node TEXT PRIMARY KEY) WITHOUT ROWID;
            """
        )
        projection = self._undirected_projection(f"{stem}-projection")
        if vertex_cut:
            infinity = max(2, self.graph.node_count + 1)
            for node in self.graph.iter_node_ids():
                capacity = infinity if node in {source, target} else 1
                self._add_residual_arc(
                    connection,
                    self._flow_vertex("in", node),
                    self._flow_vertex("out", node),
                    capacity,
                )
            for left, right in projection.execute(
                "SELECT source,target FROM projected_edges WHERE source<>target"
            ):
                left, right = str(left), str(right)
                edge_capacity = 1 if {left, right} == {source, target} else infinity
                self._add_residual_arc(
                    connection,
                    self._flow_vertex("out", left),
                    self._flow_vertex("in", right),
                    edge_capacity,
                )
                self._add_residual_arc(
                    connection,
                    self._flow_vertex("out", right),
                    self._flow_vertex("in", left),
                    edge_capacity,
                )
            flow_source = self._flow_vertex("out", source)
            flow_target = self._flow_vertex("in", target)
        else:
            for left, right in projection.execute(
                "SELECT source,target FROM projected_edges WHERE source<>target"
            ):
                left, right = str(left), str(right)
                self._add_residual_arc(connection, left, right, 1)
                self._add_residual_arc(connection, right, left, 1)
            flow_source, flow_target = source, target
        projection.close()
        connection.commit()

        flow = 0
        while True:
            connection.execute("DELETE FROM residual_parent")
            connection.execute("DELETE FROM residual_queue")
            connection.execute(
                "INSERT INTO residual_parent(node,arc_id) VALUES (?,NULL)",
                (flow_source,),
            )
            connection.execute("INSERT INTO residual_queue(node) VALUES (?)", (flow_source,))
            reached_target = False
            while True:
                queued = connection.execute(
                    "SELECT seq,node FROM residual_queue ORDER BY seq LIMIT 1"
                ).fetchone()
                if queued is None:
                    break
                connection.execute("DELETE FROM residual_queue WHERE seq=?", (int(queued[0]),))
                node = str(queued[1])
                for arc_id, neighbor in connection.execute(
                    "SELECT id,target FROM residual_arcs "
                    "WHERE source=? AND capacity-flow>0 ORDER BY id",
                    (node,),
                ):
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO residual_parent(node,arc_id) VALUES (?,?)",
                        (str(neighbor), int(arc_id)),
                    ).rowcount
                    if not inserted:
                        continue
                    if str(neighbor) == flow_target:
                        reached_target = True
                        break
                    connection.execute(
                        "INSERT INTO residual_queue(node) VALUES (?)", (str(neighbor),)
                    )
                if reached_target:
                    break
            if not reached_target:
                break
            cursor = flow_target
            bottleneck: int | None = None
            while cursor != flow_source:
                arc_id = int(connection.execute(
                    "SELECT arc_id FROM residual_parent WHERE node=?", (cursor,)
                ).fetchone()[0])
                row = connection.execute(
                    "SELECT source,capacity-flow,reverse_id FROM residual_arcs WHERE id=?",
                    (arc_id,),
                ).fetchone()
                residual = int(row[1])
                bottleneck = residual if bottleneck is None else min(bottleneck, residual)
                cursor = str(row[0])
            delta = int(bottleneck or 0)
            if delta <= 0:
                break
            cursor = flow_target
            while cursor != flow_source:
                arc_id = int(connection.execute(
                    "SELECT arc_id FROM residual_parent WHERE node=?", (cursor,)
                ).fetchone()[0])
                arc = connection.execute(
                    "SELECT source,reverse_id FROM residual_arcs WHERE id=?", (arc_id,)
                ).fetchone()
                reverse_id = int(arc[1])
                connection.execute(
                    "UPDATE residual_arcs SET flow=flow+? WHERE id=?", (delta, arc_id)
                )
                connection.execute(
                    "UPDATE residual_arcs SET flow=flow-? WHERE id=?", (delta, reverse_id)
                )
                cursor = str(arc[0])
            flow += delta
            connection.commit()

        connection.execute(
            "INSERT INTO cut_reached(node) SELECT node FROM residual_parent"
        )
        connection.commit()
        return connection, flow

    def _connectivity_pair(
        self, source: str, target: str, *, vertex_cut: bool, stem: str
    ) -> tuple[int, list[str] | list[dict[str, Any]]]:
        state, value = self._unit_cut_state(
            source, target, vertex_cut=vertex_cut, stem=stem
        )
        rows: list[Any] = []
        if vertex_cut:
            for node in self.graph.iter_node_ids():
                if node in {source, target}:
                    continue
                inside = state.execute(
                    "SELECT 1 FROM cut_reached WHERE node=?",
                    (self._flow_vertex("in", node),),
                ).fetchone()
                outside = state.execute(
                    "SELECT 1 FROM cut_reached WHERE node=?",
                    (self._flow_vertex("out", node),),
                ).fetchone()
                if inside is not None and outside is None:
                    if len(rows) < _MAX_LIMIT:
                        rows.append(self._display(node))
        else:
            projection = self._undirected_projection(f"{stem}-cut-projection")
            for left, right in projection.execute(
                "SELECT source,target FROM projected_edges WHERE source<>target ORDER BY source,target"
            ):
                left_reached = state.execute(
                    "SELECT 1 FROM cut_reached WHERE node=?", (str(left),)
                ).fetchone() is not None
                right_reached = state.execute(
                    "SELECT 1 FROM cut_reached WHERE node=?", (str(right),)
                ).fetchone() is not None
                if left_reached != right_reached:
                    if len(rows) < _MAX_LIMIT:
                        rows.append({"from": self._display(str(left)), "to": self._display(str(right))})
            projection.close()
        state.close()
        return value, rows

    def _global_connectivity_cut(
        self, *, vertex_cut: bool
    ) -> tuple[int, list[str] | list[dict[str, Any]]]:
        if self.graph.node_count < 2:
            return 0, []
        root = next(self.graph.iter_node_ids())
        best = self.graph.node_count
        best_rows: list[Any] = []
        if vertex_cut:
            projection = self._undirected_projection("grouped-global-node-cut-projection")
            minimum_degree = self.graph.node_count
            minimum_degree_node: str | None = None
            for node in self.graph.iter_node_ids():
                degree = int(projection.execute(
                    "SELECT COUNT(*) FROM projected_edges "
                    "WHERE source<>target AND (source=? OR target=?)",
                    (node, node),
                ).fetchone()[0])
                if degree < minimum_degree:
                    minimum_degree = degree
                    minimum_degree_node = node
            best = minimum_degree if self.graph.node_count else 0
            if minimum_degree_node is not None:
                for neighbor, _weight in self.graph.iter_neighbors(
                    minimum_degree_node, undirected=True
                ):
                    if neighbor != minimum_degree_node and len(best_rows) < _MAX_LIMIT:
                        best_rows.append(self._display(neighbor))
            # For complete graphs the minimum degree is the answer.  For all
            # non-adjacent pairs Menger's theorem yields the global cut.
            for left in self.graph.iter_node_ids():
                for right in self.graph.iter_node_ids():
                    if right <= left:
                        continue
                    adjacent = projection.execute(
                        "SELECT 1 FROM projected_edges WHERE source=? AND target=?",
                        tuple(sorted((left, right))),
                    ).fetchone()
                    if adjacent is not None:
                        continue
                    value, rows = self._connectivity_pair(
                        left,
                        right,
                        vertex_cut=True,
                        stem=(
                            "grouped-global-node-flow-"
                            + hashlib.sha256(f"{left}\0{right}".encode("utf-8")).hexdigest()[:16]
                        ),
                    )
                    if value < best:
                        best, best_rows = value, list(rows)
                        if best == 0:
                            projection.close()
                            return best, best_rows
            projection.close()
            return best, best_rows

        for sink in self.graph.iter_node_ids():
            if sink == root:
                continue
            value, rows = self._connectivity_pair(
                root,
                sink,
                vertex_cut=False,
                stem=(
                    "grouped-global-edge-flow-"
                    + hashlib.sha256(f"{root}\0{sink}".encode("utf-8")).hexdigest()[:16]
                ),
            )
            if value < best:
                best, best_rows = value, list(rows)
                if best == 0:
                    break
        return best, best_rows

    def _tarjan_undirected(self, stem: str = "grouped-tarjan") -> sqlite3.Connection:
        """Iterative disk-backed Tarjan DFS for cuts and edge blocks."""
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS tarjan_state;
            DROP TABLE IF EXISTS tarjan_events;
            DROP TABLE IF EXISTS tarjan_edge_stack;
            DROP TABLE IF EXISTS tarjan_bridges;
            DROP TABLE IF EXISTS tarjan_articulation;
            DROP TABLE IF EXISTS tarjan_bcc_edges;
            CREATE TABLE tarjan_state(
                node TEXT PRIMARY KEY,disc INTEGER NOT NULL,low INTEGER NOT NULL,
                parent TEXT,children INTEGER NOT NULL DEFAULT 0
            ) WITHOUT ROWID;
            CREATE TABLE tarjan_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
                source TEXT NOT NULL,target TEXT
            );
            CREATE TABLE tarjan_edge_stack(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT,target TEXT
            );
            CREATE TABLE tarjan_bridges(
                source TEXT,target TEXT,PRIMARY KEY(source,target)
            ) WITHOUT ROWID;
            CREATE TABLE tarjan_articulation(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE tarjan_bcc_edges(
                component INTEGER,position INTEGER,source TEXT,target TEXT,
                PRIMARY KEY(component,position)
            ) WITHOUT ROWID;
            """
        )
        timestamp = 0
        component = 0

        def discover(node: str, parent: str | None) -> None:
            nonlocal timestamp
            timestamp += 1
            connection.execute(
                "INSERT INTO tarjan_state(node,disc,low,parent,children) VALUES (?,?,?,?,0)",
                (node, timestamp, timestamp, parent),
            )
            connection.execute(
                "INSERT INTO tarjan_events(kind,source,target) VALUES ('finish',?,?)",
                (node, parent),
            )
            for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                if neighbor != node:
                    connection.execute(
                        "INSERT INTO tarjan_events(kind,source,target) VALUES ('edge',?,?)",
                        (node, neighbor),
                    )

        def emit_component(stop_source: str, stop_target: str) -> None:
            nonlocal component
            component += 1
            position = 0
            while True:
                row = connection.execute(
                    "SELECT seq,source,target FROM tarjan_edge_stack ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    break
                connection.execute("DELETE FROM tarjan_edge_stack WHERE seq=?", (int(row[0]),))
                connection.execute(
                    "INSERT INTO tarjan_bcc_edges(component,position,source,target) VALUES (?,?,?,?)",
                    (component, position, str(row[1]), str(row[2])),
                )
                position += 1
                if str(row[1]) == stop_source and str(row[2]) == stop_target:
                    break

        for root in self.graph.iter_node_ids():
            if connection.execute(
                "SELECT 1 FROM tarjan_state WHERE node=?", (root,)
            ).fetchone():
                continue
            discover(root, None)
            while True:
                event = connection.execute(
                    "SELECT seq,kind,source,target FROM tarjan_events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                if event is None:
                    break
                connection.execute("DELETE FROM tarjan_events WHERE seq=?", (int(event[0]),))
                kind, source = str(event[1]), str(event[2])
                target = None if event[3] is None else str(event[3])
                if kind == "edge":
                    assert target is not None
                    child = connection.execute(
                        "SELECT disc FROM tarjan_state WHERE node=?", (target,)
                    ).fetchone()
                    parent = connection.execute(
                        "SELECT parent FROM tarjan_state WHERE node=?", (source,)
                    ).fetchone()[0]
                    if child is None:
                        connection.execute(
                            "UPDATE tarjan_state SET children=children+1 WHERE node=?", (source,)
                        )
                        connection.execute(
                            "INSERT INTO tarjan_edge_stack(source,target) VALUES (?,?)", (source, target)
                        )
                        connection.execute(
                            "INSERT INTO tarjan_events(kind,source,target) VALUES ('return',?,?)",
                            (source, target),
                        )
                        discover(target, source)
                    elif target != parent:
                        disc_target = int(child[0])
                        disc_source = int(connection.execute(
                            "SELECT disc FROM tarjan_state WHERE node=?", (source,)
                        ).fetchone()[0])
                        if disc_target < disc_source:
                            connection.execute(
                                "UPDATE tarjan_state SET low=MIN(low,?) WHERE node=?",
                                (disc_target, source),
                            )
                            connection.execute(
                                "INSERT INTO tarjan_edge_stack(source,target) VALUES (?,?)",
                                (source, target),
                            )
                elif kind == "return":
                    assert target is not None
                    parent_row = connection.execute(
                        "SELECT disc,low,parent FROM tarjan_state WHERE node=?", (source,)
                    ).fetchone()
                    child_row = connection.execute(
                        "SELECT low FROM tarjan_state WHERE node=?", (target,)
                    ).fetchone()
                    child_low = int(child_row[0])
                    connection.execute(
                        "UPDATE tarjan_state SET low=MIN(low,?) WHERE node=?", (child_low, source)
                    )
                    if child_low > int(parent_row[0]):
                        left, right = sorted((source, target))
                        connection.execute(
                            "INSERT OR IGNORE INTO tarjan_bridges(source,target) VALUES (?,?)",
                            (left, right),
                        )
                    if child_low >= int(parent_row[0]):
                        if parent_row[2] is not None:
                            connection.execute(
                                "INSERT OR IGNORE INTO tarjan_articulation(node) VALUES (?)", (source,)
                            )
                        emit_component(source, target)
                else:  # finish
                    row = connection.execute(
                        "SELECT parent,children FROM tarjan_state WHERE node=?", (source,)
                    ).fetchone()
                    if row[0] is None and int(row[1]) > 1:
                        connection.execute(
                            "INSERT OR IGNORE INTO tarjan_articulation(node) VALUES (?)", (source,)
                        )
            # A root-only residual stack is one final block.
            if connection.execute("SELECT 1 FROM tarjan_edge_stack LIMIT 1").fetchone():
                emit_component("", "")
        connection.commit()
        return connection

    def _shortest_avoiding_edge(
        self, source: str, target: str, removed_left: str, removed_right: str
    ) -> float | None:
        connection = self._connection("grouped-local-bridge-span")
        connection.executescript(
            """
            DROP TABLE IF EXISTS span_distance;
            DROP TABLE IF EXISTS span_queue;
            CREATE TABLE span_distance(node TEXT PRIMARY KEY,value REAL) WITHOUT ROWID;
            CREATE TABLE span_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT,value REAL);
            """
        )
        connection.execute("INSERT INTO span_distance(node,value) VALUES (?,0)", (source,))
        connection.execute("INSERT INTO span_queue(node,value) VALUES (?,0)", (source,))
        answer: float | None = None
        while True:
            row = connection.execute(
                "SELECT seq,node,value FROM span_queue ORDER BY value,seq LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM span_queue WHERE seq=?", (int(row[0]),))
            node, distance = str(row[1]), float(row[2])
            current = connection.execute(
                "SELECT value FROM span_distance WHERE node=?", (node,)
            ).fetchone()
            if current is None or distance != float(current[0]):
                continue
            if node == target:
                answer = distance
                break
            for neighbor, weight in self.graph.iter_neighbors(node, undirected=True):
                if {node, neighbor} == {removed_left, removed_right}:
                    continue
                candidate = distance + float(weight)
                previous = connection.execute(
                    "SELECT value FROM span_distance WHERE node=?", (neighbor,)
                ).fetchone()
                if previous is None or candidate < float(previous[0]):
                    connection.execute(
                        "INSERT INTO span_distance(node,value) VALUES (?,?) "
                        "ON CONFLICT(node) DO UPDATE SET value=excluded.value",
                        (neighbor, candidate),
                    )
                    connection.execute(
                        "INSERT INTO span_queue(node,value) VALUES (?,?)", (neighbor, candidate)
                    )
        connection.close()
        return answer

    def connectivity_analysis(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        limit = _bounded_limit(p)
        known = {
            "node_connectivity", "edge_connectivity", "minimum_node_cut",
            "minimum_edge_cut", "articulation_points", "bridges", "local_bridges",
            "biconnected_component_edges", "bridge_components", "condensation",
        }
        if method not in known:
            return self._group_result({"success": False, "error": f"未知方法 '{method}'"})
        source_raw, target_raw = p.get("source"), p.get("target")
        if method in {"node_connectivity", "edge_connectivity", "minimum_node_cut", "minimum_edge_cut"}:
            if bool(source_raw) != bool(target_raw):
                return self._group_result({"success": False, "error": "source 与 target 必须同时提供或同时省略"})
            vertex_cut = method in {"node_connectivity", "minimum_node_cut"}
            if source_raw:
                source, source_error = self._required_node(source_raw, "source")
                target, target_error = self._required_node(target_raw, "target")
                if source_error or target_error:
                    return self._group_result(source_error or target_error or {})
                assert source is not None and target is not None
                value, rows = self._connectivity_pair(
                    source, target, vertex_cut=vertex_cut, stem=f"grouped-{method}-flow"
                )
                if method == "minimum_node_cut" and self._is_adjacent_grouped(source, target):
                    rows = []
                    value = 0
            else:
                components = self._component_state(f"grouped-{method}-connected", undirected=True)
                count = int(components.execute(
                    "SELECT COUNT(DISTINCT component) FROM components"
                ).fetchone()[0])
                components.close()
                if self.graph.node_count == 0 or count != 1:
                    if method.startswith("minimum_"):
                        return self._group_result({"success": False, "error": "全局最小割要求非空连通图"})
                    value, rows = 0, []
                else:
                    value, rows = self._global_connectivity_cut(vertex_cut=vertex_cut)
            if method in {"node_connectivity", "edge_connectivity"}:
                return self._group_result({
                    "success": True, "method": method,
                    "source": self._display(source) if source_raw else None,
                    "target": self._display(target) if target_raw else None,
                    "value": int(value), "execution_mode": "exact-disk-flow",
                })
            key = "nodes" if vertex_cut else "edges"
            preview = list(rows[:limit])
            return self._group_result({
                "success": True, "method": method, "size": int(value),
                key: preview, "truncated": int(value) > len(preview),
                "execution_mode": "exact-disk-flow",
            }, preview)

        if method == "condensation":
            if not self.graph.directed:
                return self._group_result({"success": False, "error": "condensation 要求有向图"})
            components = self._component_state("grouped-condensation")
            connection = self._connection("grouped-condensation-output")
            connection.executescript(
                """
                CREATE TABLE component_edges(
                    source INTEGER,target INTEGER,PRIMARY KEY(source,target)
                ) WITHOUT ROWID;
                """
            )
            for source, target, _weight, _relation in self.graph.iter_edges():
                left = int(components.execute(
                    "SELECT component FROM components WHERE node=?", (source,)
                ).fetchone()[0])
                right = int(components.execute(
                    "SELECT component FROM components WHERE node=?", (target,)
                ).fetchone()[0])
                if left != right:
                    connection.execute(
                        "INSERT OR IGNORE INTO component_edges(source,target) VALUES (?,?)",
                        (left, right),
                    )
            component_count = int(components.execute(
                "SELECT COUNT(DISTINCT component) FROM components"
            ).fetchone()[0])

            def rows() -> Iterator[dict[str, Any]]:
                for component_id in range(component_count):
                    members = [self._display(str(row[0])) for row in components.execute(
                        "SELECT node FROM components WHERE component=? ORDER BY node LIMIT ?",
                        (component_id, _PREVIEW_LIMIT),
                    )]
                    size = int(components.execute(
                        "SELECT COUNT(*) FROM components WHERE component=?", (component_id,)
                    ).fetchone()[0])
                    yield {"type": "component", "id": component_id, "members": members,
                           "member_count": size, "members_truncated": size > len(members)}
                for left, right in connection.execute(
                    "SELECT source,target FROM component_edges ORDER BY source,target"
                ):
                    yield {"type": "edge", "from": int(left), "to": int(right)}

            files, preview, _count = self._write_rows("condensation", rows())
            edge_count = int(connection.execute("SELECT COUNT(*) FROM component_edges").fetchone()[0])
            components.close()
            connection.close()
            node_preview = [row for row in preview if row["type"] == "component"][:limit]
            edge_preview = [row for row in preview if row["type"] == "edge"][:limit]
            return self._group_result({
                "success": True, "method": method, "num_components": component_count,
                "nodes": node_preview, "edges": edge_preview,
                "num_edges": edge_count,
                "truncated": component_count + edge_count > len(preview),
            }, preview, files)

        tarjan = self._tarjan_undirected(f"grouped-{method}-tarjan")
        try:
            if method == "articulation_points":
                total = int(tarjan.execute("SELECT COUNT(*) FROM tarjan_articulation").fetchone()[0])
                nodes = [self._display(str(row[0])) for row in tarjan.execute(
                    "SELECT node FROM tarjan_articulation ORDER BY node LIMIT ?", (limit,)
                )]
                return self._group_result({
                    "success": True, "method": method, "count": total,
                    "nodes": nodes, "truncated": total > len(nodes),
                }, nodes)
            if method == "bridges":
                def bridge_rows() -> Iterator[dict[str, Any]]:
                    for left, right in tarjan.execute(
                        "SELECT source,target FROM tarjan_bridges ORDER BY source,target"
                    ):
                        yield {"from": self._display(str(left)), "to": self._display(str(right))}
                files, preview, count = self._write_rows("bridges", bridge_rows())
                return self._group_result({
                    "success": True, "method": method, "count": count,
                    "items": preview[:limit], "truncated": count > limit,
                }, preview, files)
            if method == "local_bridges":
                projection = self._undirected_projection("grouped-local-bridges-projection")
                def local_rows() -> Iterator[dict[str, Any]]:
                    for left, right in projection.execute(
                        "SELECT source,target FROM projected_edges WHERE source<>target ORDER BY source,target"
                    ):
                        left, right = str(left), str(right)
                        common = False
                        for neighbor, _weight in self.graph.iter_neighbors(left, undirected=True):
                            if neighbor != right and self._is_adjacent_grouped(neighbor, right):
                                common = True
                                break
                        if common:
                            continue
                        span = self._shortest_avoiding_edge(left, right, left, right)
                        yield {"from": self._display(left), "to": self._display(right), "span": span}
                files, preview, count = self._write_rows("local-bridges", local_rows())
                projection.close()
                return self._group_result({
                    "success": True, "method": method, "count": count,
                    "items": preview[:limit], "truncated": count > limit,
                }, preview, files)
            if method == "biconnected_component_edges":
                component_count = int(tarjan.execute(
                    "SELECT COUNT(DISTINCT component) FROM tarjan_bcc_edges"
                ).fetchone()[0])
                rows: list[list[dict[str, Any]]] = []
                for component_id in range(1, min(component_count, limit) + 1):
                    rows.append([
                        {"from": self._display(str(left)), "to": self._display(str(right))}
                        for left, right in tarjan.execute(
                            "SELECT source,target FROM tarjan_bcc_edges "
                            "WHERE component=? ORDER BY position LIMIT ?",
                            (component_id, _PREVIEW_LIMIT),
                        )
                    ])
                def bcc_rows() -> Iterator[dict[str, Any]]:
                    for component_id, position, left, right in tarjan.execute(
                        "SELECT component,position,source,target FROM tarjan_bcc_edges "
                        "ORDER BY component,position"
                    ):
                        yield {"component": int(component_id), "position": int(position),
                               "from": self._display(str(left)), "to": self._display(str(right))}
                files, preview, _edge_count = self._write_rows("biconnected-component-edges", bcc_rows())
                return self._group_result({
                    "success": True, "method": method, "count": component_count,
                    "items": rows, "truncated": component_count > len(rows),
                }, preview, files)

            # bridge_components: traverse the graph while skipping Tarjan bridges.
            components = self._connection("grouped-bridge-components")
            components.executescript(
                """
                CREATE TABLE assigned(node TEXT PRIMARY KEY,component INTEGER) WITHOUT ROWID;
                CREATE TABLE queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT);
                """
            )
            component_id = 0
            for root in self.graph.iter_node_ids():
                if components.execute("SELECT 1 FROM assigned WHERE node=?", (root,)).fetchone():
                    continue
                components.execute("INSERT INTO assigned(node,component) VALUES (?,?)", (root, component_id))
                components.execute("INSERT INTO queue(node) VALUES (?)", (root,))
                while True:
                    queued = components.execute("SELECT seq,node FROM queue ORDER BY seq LIMIT 1").fetchone()
                    if queued is None:
                        break
                    components.execute("DELETE FROM queue WHERE seq=?", (int(queued[0]),))
                    node = str(queued[1])
                    for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                        left, right = sorted((node, neighbor))
                        if tarjan.execute(
                            "SELECT 1 FROM tarjan_bridges WHERE source=? AND target=?", (left, right)
                        ).fetchone():
                            continue
                        inserted = components.execute(
                            "INSERT OR IGNORE INTO assigned(node,component) VALUES (?,?)",
                            (neighbor, component_id),
                        ).rowcount
                        if inserted:
                            components.execute("INSERT INTO queue(node) VALUES (?)", (neighbor,))
                component_id += 1
            sizes = list(components.execute(
                "SELECT component,COUNT(*) FROM assigned GROUP BY component ORDER BY COUNT(*) DESC,component LIMIT ?",
                (limit,),
            ))
            previews = [[self._display(str(row[0])) for row in components.execute(
                "SELECT node FROM assigned WHERE component=? ORDER BY node LIMIT ?",
                (int(component), _PREVIEW_LIMIT),
            )] for component, _size in sizes]
            def component_rows() -> Iterator[dict[str, Any]]:
                for node, component in components.execute(
                    "SELECT node,component FROM assigned ORDER BY component,node"
                ):
                    yield {"node": self._display(str(node)), "component": int(component)}
            files, preview, _count = self._write_rows("bridge-components", component_rows())
            components.close()
            return self._group_result({
                "success": True, "method": method, "count": component_id,
                "components": previews, "truncated": component_id > len(previews),
            }, preview, files)
        finally:
            tarjan.close()

    # ── Trees, DAGs and dominance ─────────────────────────────────────

    def _spanning_tree(
        self, *, maximum: bool, seed: int, stem: str
    ) -> tuple[list[str], list[dict[str, Any]], int, float, bool]:
        connection = self._undirected_projection(f"{stem}-projection")
        connection.execute(
            "ALTER TABLE projected_edges ADD COLUMN random_key REAL NOT NULL DEFAULT 0"
        )
        if not maximum:
            rng = random.Random(seed)
            for left, right in connection.execute(
                "SELECT source,target FROM projected_edges ORDER BY source,target"
            ):
                connection.execute(
                    "UPDATE projected_edges SET random_key=? WHERE source=? AND target=?",
                    (rng.random(), str(left), str(right)),
                )
        find, union = self._union_find(connection)
        edge_count = 0
        total_weight = 0.0
        order = "weight DESC,source,target" if maximum else "random_key,source,target"

        def rows() -> Iterator[dict[str, Any]]:
            nonlocal edge_count, total_weight
            for left, right, weight in connection.execute(
                f"SELECT source,target,weight FROM projected_edges "
                f"WHERE source<>target ORDER BY {order}"
            ):
                if not union(str(left), str(right)):
                    continue
                edge_count += 1
                total_weight += float(weight)
                yield {"from": self._display(str(left)), "to": self._display(str(right)),
                       "weight": float(weight)}

        files, preview, _ = self._write_rows(stem, rows())
        connection.execute(
            "CREATE TABLE grouped_roots(node TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        for node in self.graph.iter_node_ids():
            connection.execute(
                "INSERT OR IGNORE INTO grouped_roots(node) VALUES (?)", (find(node),)
            )
        components = int(connection.execute("SELECT COUNT(*) FROM grouped_roots").fetchone()[0])
        connection.close()
        return files, preview, edge_count, total_weight, components <= 1

    def _dfs_reverse_postorder(self, source: str, stem: str) -> sqlite3.Connection:
        connection = self._connection(stem)
        connection.executescript(
            """
            CREATE TABLE dfs_seen(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE dfs_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,node TEXT
            );
            CREATE TABLE reverse_postorder(
                position INTEGER PRIMARY KEY,node TEXT UNIQUE
            );
            """
        )
        connection.execute("INSERT INTO dfs_events(kind,node) VALUES ('enter',?)", (source,))
        finish_position = 0
        while True:
            row = connection.execute(
                "SELECT seq,kind,node FROM dfs_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM dfs_events WHERE seq=?", (int(row[0]),))
            node = str(row[2])
            if str(row[1]) == "exit":
                connection.execute(
                    "INSERT OR IGNORE INTO reverse_postorder(position,node) VALUES (?,?)",
                    (finish_position, node),
                )
                finish_position += 1
                continue
            inserted = connection.execute(
                "INSERT OR IGNORE INTO dfs_seen(node) VALUES (?)", (node,)
            ).rowcount
            if not inserted:
                continue
            connection.execute("INSERT INTO dfs_events(kind,node) VALUES ('exit',?)", (node,))
            for neighbor, _weight in self.graph.iter_out_edges(node):
                if connection.execute(
                    "SELECT 1 FROM dfs_seen WHERE node=?", (neighbor,)
                ).fetchone() is None:
                    connection.execute(
                        "INSERT INTO dfs_events(kind,node) VALUES ('enter',?)", (neighbor,)
                    )
        # Finish positions are postorder; reverse them into dense RPO ranks.
        total = int(connection.execute("SELECT COUNT(*) FROM reverse_postorder").fetchone()[0])
        connection.execute("ALTER TABLE reverse_postorder ADD COLUMN rpo INTEGER")
        connection.execute(
            "UPDATE reverse_postorder SET rpo=?-1-position", (total,)
        )
        connection.execute(
            "CREATE UNIQUE INDEX grouped_rpo_idx ON reverse_postorder(rpo)"
        )
        connection.commit()
        return connection

    @staticmethod
    def _dominator_intersect(
        connection: sqlite3.Connection, left: str, right: str
    ) -> str:
        finger_left, finger_right = left, right
        while finger_left != finger_right:
            left_rank = int(connection.execute(
                "SELECT rpo FROM reverse_postorder WHERE node=?", (finger_left,)
            ).fetchone()[0])
            right_rank = int(connection.execute(
                "SELECT rpo FROM reverse_postorder WHERE node=?", (finger_right,)
            ).fetchone()[0])
            while left_rank > right_rank:
                finger_left = str(connection.execute(
                    "SELECT dominator FROM immediate_dominator WHERE node=?", (finger_left,)
                ).fetchone()[0])
                left_rank = int(connection.execute(
                    "SELECT rpo FROM reverse_postorder WHERE node=?", (finger_left,)
                ).fetchone()[0])
            while right_rank > left_rank:
                finger_right = str(connection.execute(
                    "SELECT dominator FROM immediate_dominator WHERE node=?", (finger_right,)
                ).fetchone()[0])
                right_rank = int(connection.execute(
                    "SELECT rpo FROM reverse_postorder WHERE node=?", (finger_right,)
                ).fetchone()[0])
        return finger_left

    def _dominator_state(self, source: str, stem: str) -> sqlite3.Connection:
        connection = self._dfs_reverse_postorder(source, stem)
        connection.executescript(
            """
            CREATE TABLE immediate_dominator(
                node TEXT PRIMARY KEY,dominator TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE dominance_frontier(
                node TEXT,frontier TEXT,PRIMARY KEY(node,frontier)
            ) WITHOUT ROWID;
            """
        )
        connection.execute(
            "INSERT INTO immediate_dominator(node,dominator) VALUES (?,?)",
            (source, source),
        )
        changed = True
        while changed:
            changed = False
            for (node,) in connection.execute(
                "SELECT node FROM reverse_postorder WHERE node<>? ORDER BY rpo", (source,)
            ):
                node = str(node)
                new_idom: str | None = None
                for predecessor, _weight in self.graph.iter_in_edges(node):
                    if connection.execute(
                        "SELECT 1 FROM immediate_dominator WHERE node=?", (predecessor,)
                    ).fetchone() is None:
                        continue
                    new_idom = predecessor if new_idom is None else self._dominator_intersect(
                        connection, predecessor, new_idom
                    )
                if new_idom is None:
                    continue
                old = connection.execute(
                    "SELECT dominator FROM immediate_dominator WHERE node=?", (node,)
                ).fetchone()
                if old is None or str(old[0]) != new_idom:
                    connection.execute(
                        "INSERT INTO immediate_dominator(node,dominator) VALUES (?,?) "
                        "ON CONFLICT(node) DO UPDATE SET dominator=excluded.dominator",
                        (node, new_idom),
                    )
                    changed = True
            connection.commit()

        for (node,) in connection.execute(
            "SELECT node FROM reverse_postorder ORDER BY rpo"
        ):
            node = str(node)
            predecessor_count = sum(1 for _ in self.graph.iter_in_edges(node))
            if predecessor_count < 2:
                continue
            stop_row = connection.execute(
                "SELECT dominator FROM immediate_dominator WHERE node=?", (node,)
            ).fetchone()
            if stop_row is None:
                continue
            stop = str(stop_row[0])
            for predecessor, _weight in self.graph.iter_in_edges(node):
                if connection.execute(
                    "SELECT 1 FROM immediate_dominator WHERE node=?", (predecessor,)
                ).fetchone() is None:
                    continue
                runner = predecessor
                while runner != stop:
                    connection.execute(
                        "INSERT OR IGNORE INTO dominance_frontier(node,frontier) VALUES (?,?)",
                        (runner, node),
                    )
                    parent = connection.execute(
                        "SELECT dominator FROM immediate_dominator WHERE node=?", (runner,)
                    ).fetchone()
                    if parent is None or str(parent[0]) == runner:
                        break
                    runner = str(parent[0])
        connection.commit()
        return connection

    def _dag_longest_state(self, stem: str) -> tuple[sqlite3.Connection, bool]:
        connection, is_dag = self._topological_state(stem)
        if not is_dag:
            return connection, False
        connection.executescript(
            """
            CREATE TABLE longest_distance(
                node TEXT PRIMARY KEY,value REAL NOT NULL,predecessor TEXT
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO longest_distance(node,value,predecessor) VALUES (?,0,NULL)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        for _position, node in connection.execute(
            "SELECT position,node FROM grouped_ordering ORDER BY position"
        ):
            node = str(node)
            distance = float(connection.execute(
                "SELECT value FROM longest_distance WHERE node=?", (node,)
            ).fetchone()[0])
            for neighbor, weight in self.graph.iter_out_edges(node):
                candidate = distance + float(weight)
                old = float(connection.execute(
                    "SELECT value FROM longest_distance WHERE node=?", (neighbor,)
                ).fetchone()[0])
                if candidate > old:
                    connection.execute(
                        "UPDATE longest_distance SET value=?,predecessor=? WHERE node=?",
                        (candidate, node, neighbor),
                    )
        connection.commit()
        return connection, True

    def _bounded_antichains(self, limit: int) -> tuple[list[list[str]], bool]:
        topo, is_dag = self._topological_state("grouped-antichains-topology")
        if not is_dag:
            topo.close()
            raise ValueError("antichains 要求 DAG")
        connection = self._connection("grouped-antichains")
        connection.executescript(
            """
            CREATE TABLE frames(id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE frame_members(frame INTEGER,node TEXT,PRIMARY KEY(frame,node)) WITHOUT ROWID;
            CREATE TABLE frame_candidates(frame INTEGER,ordinal INTEGER,node TEXT,
                                          PRIMARY KEY(frame,ordinal)) WITHOUT ROWID;
            CREATE TABLE closure(source TEXT,target TEXT,PRIMARY KEY(source,target)) WITHOUT ROWID;
            """
        )
        # Persist the transitive closure one source at a time.  This may use
        # O(V²) disk in dense DAGs but never O(V²) resident Python memory.
        for source in self.graph.iter_node_ids():
            reached = self._reachability_state(
                source, direction="out", stem="grouped-antichain-reach"
            )
            for (target,) in reached.execute(
                "SELECT node FROM reached WHERE node<>?", (source,)
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO closure(source,target) VALUES (?,?)",
                    (source, str(target)),
                )
            reached.close()
        root = int(connection.execute("INSERT INTO frames DEFAULT VALUES RETURNING id").fetchone()[0])
        connection.executemany(
            "INSERT INTO frame_candidates(frame,ordinal,node) VALUES (?,?,?)",
            ((root, int(position), str(node)) for position, node in topo.execute(
                "SELECT position,node FROM grouped_ordering ORDER BY position DESC"
            )),
        )
        topo.close()
        output: list[list[str]] = []
        truncated = False
        while True:
            frame_row = connection.execute("SELECT id FROM frames ORDER BY id DESC LIMIT 1").fetchone()
            if frame_row is None:
                break
            frame = int(frame_row[0])
            members_preview = [self._display(str(row[0])) for row in connection.execute(
                "SELECT node FROM frame_members WHERE frame=? ORDER BY node LIMIT ?",
                (frame, _PREVIEW_LIMIT),
            )]
            if len(output) >= limit:
                truncated = True
                break
            output.append(members_preview)
            last_ordinal = -1
            while True:
                candidate = connection.execute(
                    "SELECT ordinal,node FROM frame_candidates "
                    "WHERE frame=? AND ordinal>? ORDER BY ordinal LIMIT 1",
                    (frame, last_ordinal),
                ).fetchone()
                if candidate is None:
                    break
                candidate_ordinal, node = int(candidate[0]), str(candidate[1])
                last_ordinal = candidate_ordinal
                if connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0] > limit:
                    truncated = True
                    break
                child = int(connection.execute("INSERT INTO frames DEFAULT VALUES RETURNING id").fetchone()[0])
                connection.execute(
                    "INSERT INTO frame_members(frame,node) "
                    "SELECT ?,node FROM frame_members WHERE frame=?",
                    (child, frame),
                )
                connection.execute(
                    "INSERT INTO frame_members(frame,node) VALUES (?,?)", (child, str(node))
                )
                ordinal = 0
                for _other_ordinal, other in connection.execute(
                    "SELECT ordinal,node FROM frame_candidates "
                    "WHERE frame=? AND ordinal>? ORDER BY ordinal",
                    (frame, candidate_ordinal),
                ):
                    comparable = connection.execute(
                        "SELECT 1 FROM closure WHERE (source=? AND target=?) "
                        "OR (source=? AND target=?) LIMIT 1",
                        (str(node), str(other), str(other), str(node)),
                    ).fetchone()
                    if comparable is None:
                        connection.execute(
                            "INSERT INTO frame_candidates(frame,ordinal,node) VALUES (?,?,?)",
                            (child, ordinal, str(other)),
                        )
                        ordinal += 1
            connection.execute("DELETE FROM frames WHERE id=?", (frame,))
            connection.execute("DELETE FROM frame_members WHERE frame=?", (frame,))
            connection.execute("DELETE FROM frame_candidates WHERE frame=?", (frame,))
        connection.close()
        return output, truncated

    def tree_dag_analysis(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        limit = _bounded_limit(p)
        known = {
            "maximum_spanning_tree", "random_spanning_tree", "lowest_common_ancestor",
            "immediate_dominators", "dominance_frontiers", "dag_longest_path",
            "dag_longest_path_length", "antichains", "is_aperiodic", "colliders",
            "v_structures",
        }
        if method not in known:
            return self._group_result({"success": False, "error": f"未知方法 '{method}'"})
        if method in {"maximum_spanning_tree", "random_spanning_tree"}:
            files, preview, count, total_weight, connected = self._spanning_tree(
                maximum=method == "maximum_spanning_tree",
                seed=int(p.get("seed", 42)), stem=f"grouped-{method}",
            )
            if self.graph.node_count and not connected:
                return self._group_result({"success": False, "error": "该方法要求连通图"})
            summary = {
                "success": True, "method": method, "num_edges": count,
                "total_weight": round(total_weight, 6), "edges": preview[:limit],
                "truncated": count > limit,
                "execution_mode": "exact-disk-kruskal" if method == "maximum_spanning_tree" else "seeded-disk-randomized-kruskal",
            }
            if method == "random_spanning_tree":
                summary.update({"exact_distribution": False, "seed": int(p.get("seed", 42))})
            return self._group_result(summary, preview, files)

        if method == "lowest_common_ancestor":
            first, first_error = self._required_node(p.get("node1"), "node1")
            second, second_error = self._required_node(p.get("node2"), "node2")
            if first_error or second_error:
                return self._group_result({"success": False, "error": "node1 和 node2 必须是图中的节点"})
            topo, is_dag = self._topological_state("grouped-lca-topology")
            if not is_dag:
                topo.close()
                return self._group_result({"success": False, "error": "lowest_common_ancestor 要求 DAG"})
            left = self._reachability_state(str(first), direction="in", stem="grouped-lca-left")
            right = self._reachability_state(str(second), direction="in", stem="grouped-lca-right")
            connection = self._connection("grouped-lca-common")
            connection.execute("CREATE TABLE common(node TEXT PRIMARY KEY) WITHOUT ROWID")
            for (node,) in left.execute("SELECT node FROM reached"):
                if right.execute("SELECT 1 FROM reached WHERE node=?", (str(node),)).fetchone():
                    connection.execute("INSERT INTO common(node) VALUES (?)", (str(node),))
            ancestor = None
            # SQLite connections cannot join each other's files; stream the
            # topological order backwards and probe the common-node table.
            for (node,) in topo.execute(
                "SELECT node FROM grouped_ordering ORDER BY position DESC"
            ):
                if connection.execute("SELECT 1 FROM common WHERE node=?", (str(node),)).fetchone():
                    ancestor = str(node)
                    break
            left.close(); right.close(); topo.close(); connection.close()
            return self._group_result({
                "success": True, "method": method,
                "node1": self._display(str(first)), "node2": self._display(str(second)),
                "ancestor": self._display(ancestor) if ancestor is not None else None,
            })

        if method in {"immediate_dominators", "dominance_frontiers"}:
            if not self.graph.directed:
                return self._group_result({"success": False, "error": "支配关系分析要求有向图"})
            source, error = self._required_node(p.get("source"), "起点")
            if error:
                return self._group_result(error)
            state = self._dominator_state(str(source), f"grouped-{method}")
            if method == "immediate_dominators":
                def rows() -> Iterator[dict[str, Any]]:
                    for node, value in state.execute(
                        "SELECT node,dominator FROM immediate_dominator "
                        "WHERE node<>? ORDER BY node",
                        (str(source),),
                    ):
                        yield {"node": self._display(str(node)), "value": self._display(str(value))}
            else:
                def rows() -> Iterator[dict[str, Any]]:
                    for (node,) in state.execute(
                        "SELECT node FROM immediate_dominator ORDER BY node"
                    ):
                        values = [self._display(str(row[0])) for row in state.execute(
                            "SELECT frontier FROM dominance_frontier WHERE node=? ORDER BY frontier",
                            (str(node),),
                        )]
                        yield {"node": self._display(str(node)), "value": values}
            files, preview, count = self._write_rows(method, rows())
            state.close()
            return self._group_result({
                "success": True, "method": method, "source": self._display(str(source)),
                "count": count, "items": preview[:limit], "truncated": count > limit,
            }, preview, files)

        if method in {"dag_longest_path", "dag_longest_path_length"}:
            state, is_dag = self._dag_longest_state(f"grouped-{method}")
            if not is_dag:
                state.close()
                return self._group_result({"success": False, "error": "该方法要求 DAG"})
            row = state.execute(
                "SELECT node,value FROM longest_distance ORDER BY value DESC,node LIMIT 1"
            ).fetchone()
            endpoint = str(row[0]) if row else None
            value = float(row[1]) if row else 0.0
            if method == "dag_longest_path_length":
                state.close()
                return self._group_result({"success": True, "method": method, "value": value})
            state.execute(
                "CREATE TABLE longest_path(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT)"
            )
            cursor = endpoint
            while cursor is not None:
                state.execute("INSERT INTO longest_path(node) VALUES (?)", (cursor,))
                parent = state.execute(
                    "SELECT predecessor FROM longest_distance WHERE node=?", (cursor,)
                ).fetchone()
                cursor = None if parent is None or parent[0] is None else str(parent[0])
            path_count = int(state.execute("SELECT COUNT(*) FROM longest_path").fetchone()[0])
            display_path = [self._display(str(row[0])) for row in state.execute(
                "SELECT node FROM longest_path ORDER BY seq DESC LIMIT ?", (limit,)
            )]
            def path_rows() -> Iterator[dict[str, Any]]:
                position = 0
                for (node,) in state.execute("SELECT node FROM longest_path ORDER BY seq DESC"):
                    yield {"position": position, "node": self._display(str(node))}
                    position += 1
            files, preview, _count = self._write_rows("dag-longest-path", path_rows())
            state.close()
            return self._group_result({
                "success": True, "method": method, "path": display_path[:limit],
                "path_nodes": path_count, "truncated": path_count > limit,
            }, preview, files)

        if method == "antichains":
            try:
                rows, truncated = self._bounded_antichains(limit)
            except ValueError as exc:
                return self._group_result({"success": False, "error": str(exc)})
            return self._group_result({
                "success": True, "method": method, "antichains": rows,
                "truncated": truncated, "hard_limit": _MAX_LIMIT,
            }, rows[:_PREVIEW_LIMIT])

        if method == "is_aperiodic":
            if not self.graph.directed:
                return self._group_result({"success": False, "error": "is_aperiodic 要求有向图"})
            components = self._component_state("grouped-aperiodic-scc")
            count = int(components.execute(
                "SELECT COUNT(DISTINCT component) FROM components"
            ).fetchone()[0])
            components.close()
            if self.graph.node_count == 0 or count != 1:
                return self._group_result({"success": False, "error": "is_aperiodic 要求非空强连通有向图"})
            root = next(self.graph.iter_node_ids())
            reached = self._reachability_state(root, direction="out", stem="grouped-aperiodic-levels")
            period = 0
            for source, target, _weight, _relation in self.graph.iter_edges():
                source_depth = int(reached.execute(
                    "SELECT depth FROM reached WHERE node=?", (source,)
                ).fetchone()[0])
                target_depth = int(reached.execute(
                    "SELECT depth FROM reached WHERE node=?", (target,)
                ).fetchone()[0])
                period = math.gcd(period, source_depth - target_depth + 1)
            reached.close()
            return self._group_result({
                "success": True, "method": method, "value": period == 1,
                "period": abs(period),
            })

        if not self.graph.directed:
            return self._group_result({"success": False, "error": "碰撞结构分析要求有向图"})
        count = 0
        returned: list[dict[str, Any]] = []
        truncated = False
        for center in self.graph.iter_node_ids():
            for left, _left_weight in self.graph.iter_in_edges(center):
                for right, _right_weight in self.graph.iter_in_edges(center):
                    if right <= left:
                        continue
                    if method == "v_structures" and self._is_adjacent_grouped(left, right):
                        continue
                    count += 1
                    if len(returned) < limit:
                        returned.append({
                            "left": self._display(left), "center": self._display(center),
                            "right": self._display(right),
                        })
                    else:
                        truncated = True
                        if method == "v_structures":
                            break
                if truncated and method == "v_structures":
                    break
            if truncated and method == "v_structures":
                break
        exact = method == "colliders" or not truncated
        return self._group_result({
            "success": True, "method": method, "count": count if exact else None,
            "returned_count": len(returned), "count_is_exact": exact,
            "structures": returned, "truncated": truncated, "hard_limit": _MAX_LIMIT,
        }, returned)

    # ── Matching and colouring ────────────────────────────────────────

    def _matching_rows_from_pairs(
        self, pairs: Iterable[tuple[str, str]], projection: sqlite3.Connection
    ) -> Iterator[dict[str, Any]]:
        for raw_left, raw_right in pairs:
            left, right = sorted((str(raw_left), str(raw_right)))
            row = projection.execute(
                "SELECT weight FROM projected_edges WHERE source=? AND target=?",
                (left, right),
            ).fetchone()
            yield {
                "from": self._display(left), "to": self._display(right),
                "weight": float(row[0]) if row is not None else 1.0,
            }

    def _greedy_matching_state(
        self, *, ascending: bool, stem: str
    ) -> tuple[sqlite3.Connection, sqlite3.Connection]:
        projection = self._undirected_projection(f"{stem}-projection")
        state = self._connection(stem)
        state.executescript(
            """
            CREATE TABLE matched(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE selected(
                source TEXT,target TEXT,weight REAL,PRIMARY KEY(source,target)
            ) WITHOUT ROWID;
            """
        )
        direction = "ASC" if ascending else "DESC"
        for left, right, weight in projection.execute(
            f"SELECT source,target,weight FROM projected_edges "
            f"WHERE source<>target ORDER BY weight {direction},source,target"
        ):
            left, right = str(left), str(right)
            occupied = state.execute(
                "SELECT 1 FROM matched WHERE node IN (?,?) LIMIT 1", (left, right)
            ).fetchone()
            if occupied:
                continue
            state.execute("INSERT INTO matched(node) VALUES (?),(?)", (left, right))
            state.execute(
                "INSERT INTO selected(source,target,weight) VALUES (?,?,?)",
                (left, right, float(weight)),
            )
        state.commit()
        return state, projection

    def _matching_output(
        self,
        method: str,
        pairs: Iterable[tuple[str, str]],
        projection: sqlite3.Connection,
        limit: int,
        *,
        exact: bool,
        result_key: str = "matching",
    ) -> dict[str, Any]:
        total_weight = 0.0
        count = 0

        def rows() -> Iterator[dict[str, Any]]:
            nonlocal total_weight, count
            for row in self._matching_rows_from_pairs(pairs, projection):
                count += 1
                total_weight += float(row["weight"])
                yield row

        files, preview, _ = self._write_rows(f"{method}-edges", rows())
        summary = {
            "success": True, "method": method, "size": count,
            result_key: preview[:limit], "truncated": count > limit,
            "exact": exact,
            "execution_mode": "bounded-native-exact" if exact else "disk-greedy-approximation",
        }
        if result_key == "matching":
            summary["total_weight"] = round(total_weight, 6)
        return self._group_result(summary, preview, files)

    def _disk_coloring(
        self, *, num_colors: int | None, strategy: str, equitable: bool, stem: str
    ) -> tuple[sqlite3.Connection, int, str]:
        state = self._connection(stem)
        state.executescript(
            """
            CREATE TABLE degree(node TEXT PRIMARY KEY,value INTEGER) WITHOUT ROWID;
            CREATE TABLE coloring(node TEXT PRIMARY KEY,color INTEGER) WITHOUT ROWID;
            CREATE TABLE forbidden(color INTEGER PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE color_count(color INTEGER PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
            """
        )
        state.executemany(
            "INSERT INTO degree(node,value) VALUES (?,?)",
            ((node, sum(1 for _ in self.graph.iter_neighbors(node, undirected=True)))
             for node in self.graph.iter_node_ids()),
        )
        effective_strategy = strategy
        supported = {
            "largest_first", "random_sequential", "smallest_last", "independent_set",
            "connected_sequential_bfs", "connected_sequential_dfs", "connected_sequential",
            "saturation_largest_first", "dsatur",
        }
        if strategy not in supported:
            state.close()
            raise ValueError(f"未知着色策略 '{strategy}'")
        if strategy != "largest_first":
            # The disk kernel deliberately uses one stable largest-first order
            # for non-native execution.  The requested strategy remains in the
            # response and exact=False communicates the semantic difference.
            effective_strategy = "largest_first"
        if equitable:
            assert num_colors is not None
            state.executemany(
                "INSERT INTO color_count(color,value) VALUES (?,0)",
                ((color,) for color in range(num_colors)),
            )
        for (node,) in state.execute("SELECT node FROM degree ORDER BY value DESC,node"):
            node = str(node)
            state.execute("DELETE FROM forbidden")
            for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                row = state.execute(
                    "SELECT color FROM coloring WHERE node=?", (neighbor,)
                ).fetchone()
                if row is not None:
                    state.execute(
                        "INSERT OR IGNORE INTO forbidden(color) VALUES (?)", (int(row[0]),)
                    )
            if equitable:
                chosen = state.execute(
                    "SELECT color FROM color_count WHERE color NOT IN "
                    "(SELECT color FROM forbidden) ORDER BY value,color LIMIT 1"
                ).fetchone()
                if chosen is None:
                    state.close()
                    raise ValueError("均衡着色未找到可用颜色")
                color = int(chosen[0])
                state.execute("UPDATE color_count SET value=value+1 WHERE color=?", (color,))
            else:
                color = 0
                while state.execute(
                    "SELECT 1 FROM forbidden WHERE color=?", (color,)
                ).fetchone():
                    color += 1
            state.execute("INSERT INTO coloring(node,color) VALUES (?,?)", (node, color))
        state.commit()
        used = int(state.execute("SELECT COUNT(DISTINCT color) FROM coloring").fetchone()[0])
        return state, used, effective_strategy

    def matching_coloring_analysis(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        limit = _bounded_limit(p)
        known = {
            "max_weight_matching", "min_weight_matching", "maximal_matching",
            "min_edge_cover", "is_matching", "greedy_color", "equitable_color",
        }
        if method not in known:
            return self._group_result({"success": False, "error": f"未知方法 '{method}'"})

        if method == "is_matching":
            used: set[str] = set()
            count = 0
            valid = True
            for edge in p.get("edges") or []:
                if isinstance(edge, (list, tuple)) and len(edge) == 2:
                    raw_left, raw_right = edge
                elif isinstance(edge, dict) and "from" in edge and "to" in edge:
                    raw_left, raw_right = edge["from"], edge["to"]
                else:
                    return self._group_result({"success": False, "error": "edges 中每项必须是 [u,v] 或 {from,to}"})
                left = self.graph.resolve_node(str(raw_left))
                right = self.graph.resolve_node(str(raw_right))
                count += 1
                if left is None or right is None or left == right or left in used or right in used:
                    valid = False
                    continue
                if not self._is_adjacent_grouped(left, right):
                    valid = False
                    continue
                used.add(left); used.add(right)
            return self._group_result({
                "success": True, "method": method, "value": valid, "edge_count": count,
            })

        if method in {"greedy_color", "equitable_color"}:
            strategy = str(p.get("strategy", "largest_first")).strip().lower()
            equitable = method == "equitable_color"
            num_colors = None
            if equitable:
                try:
                    num_colors = int(p.get("num_colors"))
                except (TypeError, ValueError, OverflowError):
                    num_colors = 0
                if num_colors < 1:
                    return self._group_result({"success": False, "error": "equitable_color 要求 num_colors >= 1"})
                maximum_degree = max(
                    (sum(1 for _ in self.graph.iter_neighbors(node, undirected=True))
                     for node in self.graph.iter_node_ids()),
                    default=0,
                )
                if num_colors <= maximum_degree:
                    return self._group_result({
                        "success": False,
                        "error": f"equitable_color 要求 num_colors > 最大度数 {maximum_degree}",
                    })
            native = self._native_undirected_graph()
            if equitable and native is not None:
                import networkx as nx
                coloring = nx.equitable_color(native, int(num_colors))
                state = self._connection("grouped-equitable-native")
                state.execute("CREATE TABLE coloring(node TEXT PRIMARY KEY,color INTEGER) WITHOUT ROWID")
                state.executemany(
                    "INSERT INTO coloring(node,color) VALUES (?,?)",
                    ((str(node), int(color)) for node, color in coloring.items()),
                )
                used_colors = len(set(coloring.values()))
                effective_strategy = "networkx-equitable"
                exact = True
            else:
                try:
                    state, used_colors, effective_strategy = self._disk_coloring(
                        num_colors=num_colors, strategy=strategy, equitable=equitable,
                        stem=f"grouped-{method}",
                    )
                except ValueError as exc:
                    return self._group_result({"success": False, "error": str(exc)})
                exact = not equitable and strategy == "largest_first"

            def coloring_rows() -> Iterator[dict[str, Any]]:
                for node, color in state.execute(
                    "SELECT node,color FROM coloring ORDER BY color,node"
                ):
                    yield {"node": self._display(str(node)), "color": int(color)}

            files, preview, count = self._write_rows(f"{method}-coloring", coloring_rows())
            state.close()
            return self._group_result({
                "success": True, "method": method, "num_colors": used_colors,
                "coloring": preview[:limit], "truncated": count > limit,
                "exact": exact, "requested_strategy": strategy,
                "effective_strategy": effective_strategy,
                "execution_mode": "bounded-native-exact" if exact and equitable else "disk-greedy",
            }, preview, files)

        native = self._native_undirected_graph()
        if native is not None and method in {"max_weight_matching", "min_weight_matching", "min_edge_cover"}:
            import networkx as nx
            if method == "max_weight_matching":
                pairs = nx.max_weight_matching(
                    native, maxcardinality=bool(p.get("maxcardinality", False)), weight="weight"
                )
                result_key = "matching"
            elif method == "min_weight_matching":
                pairs = nx.min_weight_matching(native, weight="weight")
                result_key = "matching"
            else:
                if any(degree == 0 for _node, degree in native.degree()):
                    return self._group_result({"success": False, "error": "存在孤立节点，无法计算边覆盖"})
                pairs = nx.min_edge_cover(native)
                result_key = "edges"
            projection = self._undirected_projection(f"grouped-{method}-native-projection")
            try:
                return self._matching_output(
                    method, ((str(tuple(pair)[0]), str(tuple(pair)[1])) for pair in pairs),
                    projection, limit, exact=True, result_key=result_key,
                )
            finally:
                projection.close()

        state, projection = self._greedy_matching_state(
            ascending=method == "min_weight_matching", stem=f"grouped-{method}-greedy"
        )
        if method == "min_edge_cover":
            isolated = False
            for node in self.graph.iter_node_ids():
                if not any(True for _ in self.graph.iter_neighbors(node, undirected=True)):
                    isolated = True
                    break
            if isolated:
                state.close(); projection.close()
                return self._group_result({"success": False, "error": "存在孤立节点，无法计算边覆盖"})
            state.execute(
                "CREATE TABLE cover(source TEXT,target TEXT,weight REAL,PRIMARY KEY(source,target)) WITHOUT ROWID"
            )
            state.execute("INSERT INTO cover SELECT source,target,weight FROM selected")
            for node in self.graph.iter_node_ids():
                if state.execute("SELECT 1 FROM matched WHERE node=?", (node,)).fetchone():
                    continue
                candidates = []
                for neighbor, weight in self.graph.iter_neighbors(node, undirected=True):
                    left, right = sorted((node, neighbor))
                    candidates.append((float(weight), left, right))
                    if len(candidates) > 1024:
                        # Keep candidate selection bounded for a huge hub.
                        candidates = [min(candidates)]
                if candidates:
                    weight, left, right = min(candidates)
                    state.execute(
                        "INSERT OR IGNORE INTO cover(source,target,weight) VALUES (?,?,?)",
                        (left, right, weight),
                    )
            pairs_iter = ((str(left), str(right)) for left, right in state.execute(
                "SELECT source,target FROM cover ORDER BY source,target"
            ))
            result_key = "edges"
        else:
            pairs_iter = ((str(left), str(right)) for left, right in state.execute(
                "SELECT source,target FROM selected ORDER BY source,target"
            ))
            result_key = "matching"
        try:
            return self._matching_output(
                method, pairs_iter, projection, limit,
                exact=method == "maximal_matching", result_key=result_key,
            )
        finally:
            state.close(); projection.close()

    # ── Extended distance and structural measures ─────────────────────

    def _largest_component(
        self, stem: str
    ) -> tuple[sqlite3.Connection, int | None, int]:
        components = self._component_state(stem, undirected=True)
        row = components.execute(
            "SELECT component,COUNT(*) AS size FROM components "
            "GROUP BY component ORDER BY size DESC,component LIMIT 1"
        ).fetchone()
        if row is None:
            return components, None, 0
        return components, int(row[0]), int(row[1])

    def _distance_profiles(
        self, *, weighted: bool, stem: str
    ) -> tuple[sqlite3.Connection, sqlite3.Connection, int | None, int]:
        components, component_id, size = self._largest_component(f"{stem}-components")
        profiles = self._connection(stem)
        profiles.execute(
            "CREATE TABLE profiles(node TEXT PRIMARY KEY,eccentricity REAL,total REAL,"
            "reachable INTEGER) WITHOUT ROWID"
        )
        if component_id is None:
            return profiles, components, None, 0
        for (source,) in components.execute(
            "SELECT node FROM components WHERE component=? ORDER BY node", (component_id,)
        ):
            source = str(source)
            distances = self._shortest_state(
                source, weighted=weighted, direction="both", stem=f"{stem}-source"
            )
            row = distances.execute(
                "SELECT COALESCE(MAX(distance),0),COALESCE(SUM(distance),0),COUNT(*) "
                "FROM distances"
            ).fetchone()
            profiles.execute(
                "INSERT INTO profiles(node,eccentricity,total,reachable) VALUES (?,?,?,?)",
                (source, float(row[0]), float(row[1]), int(row[2])),
            )
            distances.close()
        profiles.commit()
        return profiles, components, component_id, size

    def _global_efficiency_disk(self) -> float:
        total = 0.0
        for source in self.graph.iter_node_ids():
            distances = self._shortest_state(
                source, weighted=False, direction="both", stem="grouped-global-efficiency-source"
            )
            total += float(distances.execute(
                "SELECT COALESCE(SUM(1.0/distance),0) FROM distances WHERE distance>0"
            ).fetchone()[0])
            distances.close()
        denominator = self.graph.node_count * (self.graph.node_count - 1)
        return total / denominator if denominator else 0.0

    def _local_efficiency_disk(self) -> float:
        aggregate = 0.0
        for center in self.graph.iter_node_ids():
            state = self._connection("grouped-local-efficiency")
            state.executescript(
                """
                DROP TABLE IF EXISTS allowed;
                DROP TABLE IF EXISTS local_seen;
                DROP TABLE IF EXISTS local_queue;
                CREATE TABLE allowed(node TEXT PRIMARY KEY) WITHOUT ROWID;
                CREATE TABLE local_seen(node TEXT PRIMARY KEY,depth INTEGER) WITHOUT ROWID;
                CREATE TABLE local_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT,depth INTEGER);
                """
            )
            state.executemany(
                "INSERT OR IGNORE INTO allowed(node) VALUES (?)",
                ((neighbor,) for neighbor, _weight in self.graph.iter_neighbors(center, undirected=True)
                 if neighbor != center),
            )
            degree = int(state.execute("SELECT COUNT(*) FROM allowed").fetchone()[0])
            if degree < 2:
                state.close()
                continue
            inverse_sum = 0.0
            for (source,) in state.execute("SELECT node FROM allowed ORDER BY node"):
                source = str(source)
                state.execute("DELETE FROM local_seen")
                state.execute("DELETE FROM local_queue")
                state.execute("INSERT INTO local_seen(node,depth) VALUES (?,0)", (source,))
                state.execute("INSERT INTO local_queue(node,depth) VALUES (?,0)", (source,))
                while True:
                    row = state.execute(
                        "SELECT seq,node,depth FROM local_queue ORDER BY seq LIMIT 1"
                    ).fetchone()
                    if row is None:
                        break
                    state.execute("DELETE FROM local_queue WHERE seq=?", (int(row[0]),))
                    node, depth = str(row[1]), int(row[2])
                    for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                        if state.execute("SELECT 1 FROM allowed WHERE node=?", (neighbor,)).fetchone() is None:
                            continue
                        inserted = state.execute(
                            "INSERT OR IGNORE INTO local_seen(node,depth) VALUES (?,?)",
                            (neighbor, depth + 1),
                        ).rowcount
                        if inserted:
                            state.execute(
                                "INSERT INTO local_queue(node,depth) VALUES (?,?)", (neighbor, depth + 1)
                            )
                inverse_sum += float(state.execute(
                    "SELECT COALESCE(SUM(1.0/depth),0) FROM local_seen WHERE depth>0"
                ).fetchone()[0])
            aggregate += inverse_sum / (degree * (degree - 1))
            state.close()
        return aggregate / self.graph.node_count if self.graph.node_count else 0.0

    def _neighbor_degree_state(self) -> sqlite3.Connection:
        state = self._connection("grouped-neighbor-degree")
        state.executescript(
            """
            CREATE TABLE degree(
                node TEXT PRIMARY KEY,out_value INTEGER,total_value INTEGER,
                out_strength REAL,total_strength REAL
            ) WITHOUT ROWID;
            CREATE TABLE neighbor_degree(
                node TEXT PRIMARY KEY,value REAL,connectivity_sum REAL,
                connectivity_norm REAL
            ) WITHOUT ROWID;
            """
        )
        for node in self.graph.iter_node_ids():
            out_value = self.graph.degree(node, direction="out") if self.graph.directed else self.graph.degree(node)
            total_value = self.graph.degree(node)
            out_strength = sum(
                float(weight) * (2.0 if not self.graph.directed and neighbor == node else 1.0)
                for neighbor, weight in self.graph.iter_out_edges(node)
            )
            if self.graph.directed:
                total_strength = out_strength + sum(
                    float(weight) for _neighbor, weight in self.graph.iter_in_edges(node)
                )
            else:
                total_strength = sum(
                    float(weight) for _neighbor, weight in self.graph.iter_neighbors(node, undirected=True)
                )
            state.execute(
                "INSERT INTO degree(node,out_value,total_value,out_strength,total_strength) "
                "VALUES (?,?,?,?,?)",
                (node, out_value, total_value, out_strength, total_strength),
            )
        state.commit()
        for node in self.graph.iter_node_ids():
            average_sum = 0.0
            connectivity_sum = 0.0
            source = state.execute(
                "SELECT out_strength,total_strength FROM degree WHERE node=?", (node,)
            ).fetchone()
            for neighbor, weight in self.graph.iter_out_edges(node):
                row = state.execute(
                    "SELECT out_value,total_value FROM degree WHERE node=?", (neighbor,)
                ).fetchone()
                if row is None:
                    continue
                average_sum += float(weight) * int(row[0])
                connectivity_sum += float(weight) * int(row[1])
            value = average_sum / float(source[0]) if float(source[0]) else 0.0
            state.execute(
                "INSERT INTO neighbor_degree(node,value,connectivity_sum,connectivity_norm) "
                "VALUES (?,?,?,?)",
                (node, value, connectivity_sum, float(source[1])),
            )
        state.commit()
        return state

    def _rich_club_state(self) -> sqlite3.Connection:
        state = self._undirected_projection("grouped-rich-club")
        state.execute("CREATE TABLE degree(node TEXT PRIMARY KEY,value INTEGER) WITHOUT ROWID")
        state.executemany(
            "INSERT INTO degree(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        for left, right in state.execute(
            "SELECT source,target FROM projected_edges WHERE source<>target"
        ):
            state.execute("UPDATE degree SET value=value+1 WHERE node IN (?,?)", (str(left), str(right)))
        state.execute("CREATE TABLE coefficients(k INTEGER PRIMARY KEY,value REAL)")
        maximum = int(state.execute("SELECT COALESCE(MAX(value),0) FROM degree").fetchone()[0])
        for threshold in range(maximum):
            rich_nodes = int(state.execute(
                "SELECT COUNT(*) FROM degree WHERE value>?", (threshold,)
            ).fetchone()[0])
            rich_edges = int(state.execute(
                "SELECT COUNT(*) FROM projected_edges AS edges "
                "JOIN degree AS left_degree ON left_degree.node=edges.source "
                "JOIN degree AS right_degree ON right_degree.node=edges.target "
                "WHERE edges.source<>edges.target AND left_degree.value>? AND right_degree.value>?",
                (threshold, threshold),
            ).fetchone()[0])
            denominator = rich_nodes * (rich_nodes - 1)
            value = 2.0 * rich_edges / denominator if denominator else 0.0
            state.execute("INSERT INTO coefficients(k,value) VALUES (?,?)", (threshold, value))
        state.commit()
        return state

    def _flow_hierarchy_disk(self) -> float:
        if not self.graph.edge_count:
            return 0.0
        components = self._component_state("grouped-flow-hierarchy-scc")
        cyclic_weight = 0.0
        total_weight = 0.0
        for source, target, weight, _relation in self.graph.iter_edges():
            total_weight += float(weight)
            left = components.execute(
                "SELECT component FROM components WHERE node=?", (source,)
            ).fetchone()
            right = components.execute(
                "SELECT component FROM components WHERE node=?", (target,)
            ).fetchone()
            if left is not None and right is not None and int(left[0]) == int(right[0]):
                cyclic_weight += float(weight)
        components.close()
        return 1.0 - cyclic_weight / total_weight if total_weight else 0.0

    def _estrada_hutchinson(self, *, probes: int = 8, terms: int = 12) -> float:
        """Bounded-memory stochastic trace estimate of ``trace(exp(A))``."""
        state = self._undirected_projection("grouped-estrada-projection")
        state.executescript(
            """
            CREATE TABLE estrada_vector(
                node TEXT PRIMARY KEY,z REAL,current REAL,next REAL
            ) WITHOUT ROWID;
            """
        )
        rng = random.Random(42)
        estimates: list[float] = []
        for _probe in range(probes):
            state.execute("DELETE FROM estrada_vector")
            state.executemany(
                "INSERT INTO estrada_vector(node,z,current,next) VALUES (?,?,?,0)",
                ((node, (1.0 if rng.getrandbits(1) else -1.0), 0.0)
                 for node in self.graph.iter_node_ids()),
            )
            state.execute("UPDATE estrada_vector SET current=z")
            estimate = float(self.graph.node_count)
            factorial = 1.0
            for order in range(1, terms + 1):
                state.execute("UPDATE estrada_vector SET next=0")
                for left, right in state.execute(
                    "SELECT source,target FROM projected_edges WHERE source<>target"
                ):
                    left, right = str(left), str(right)
                    left_value = float(state.execute(
                        "SELECT current FROM estrada_vector WHERE node=?", (left,)
                    ).fetchone()[0])
                    right_value = float(state.execute(
                        "SELECT current FROM estrada_vector WHERE node=?", (right,)
                    ).fetchone()[0])
                    state.execute("UPDATE estrada_vector SET next=next+? WHERE node=?", (right_value, left))
                    state.execute("UPDATE estrada_vector SET next=next+? WHERE node=?", (left_value, right))
                factorial *= order
                moment = float(state.execute(
                    "SELECT COALESCE(SUM(z*next),0) FROM estrada_vector"
                ).fetchone()[0])
                estimate += moment / factorial
                state.execute("UPDATE estrada_vector SET current=next")
            estimates.append(estimate)
        state.close()
        return sum(estimates) / len(estimates) if estimates else 0.0

    def _girth_disk(self) -> int | None:
        best: int | None = None
        for source in self.graph.iter_node_ids():
            state = self._connection("grouped-girth-source")
            state.executescript(
                """
                DROP TABLE IF EXISTS girth_seen;
                DROP TABLE IF EXISTS girth_queue;
                CREATE TABLE girth_seen(
                    node TEXT PRIMARY KEY,depth INTEGER,parent TEXT
                ) WITHOUT ROWID;
                CREATE TABLE girth_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT);
                """
            )
            state.execute("INSERT INTO girth_seen(node,depth,parent) VALUES (?,0,NULL)", (source,))
            state.execute("INSERT INTO girth_queue(node) VALUES (?)", (source,))
            while True:
                queued = state.execute("SELECT seq,node FROM girth_queue ORDER BY seq LIMIT 1").fetchone()
                if queued is None:
                    break
                state.execute("DELETE FROM girth_queue WHERE seq=?", (int(queued[0]),))
                node = str(queued[1])
                depth, parent = state.execute(
                    "SELECT depth,parent FROM girth_seen WHERE node=?", (node,)
                ).fetchone()
                depth = int(depth)
                if best is not None and 2 * depth + 1 >= best:
                    continue
                for neighbor, _weight in self.graph.iter_neighbors(node, undirected=True):
                    if neighbor == node:
                        best = 1
                        break
                    seen = state.execute(
                        "SELECT depth,parent FROM girth_seen WHERE node=?", (neighbor,)
                    ).fetchone()
                    if seen is None:
                        state.execute(
                            "INSERT INTO girth_seen(node,depth,parent) VALUES (?,?,?)",
                            (neighbor, depth + 1, node),
                        )
                        state.execute("INSERT INTO girth_queue(node) VALUES (?)", (neighbor,))
                    elif str(parent or "") != neighbor:
                        candidate = depth + int(seen[0]) + 1
                        best = candidate if best is None else min(best, candidate)
                if best == 1:
                    break
            state.close()
            if best in {1, 3}:
                break
        return best

    def distance_measures(self, p: dict[str, Any]) -> dict[str, Any]:
        metric = str(p.get("metric", "")).strip().lower()
        limit = _bounded_limit(p)
        known = {
            "center", "periphery", "eccentricity", "wiener_index",
            "global_efficiency", "local_efficiency", "average_neighbor_degree",
            "average_degree_connectivity", "rich_club_coefficient", "reciprocity",
            "flow_hierarchy", "barycenter", "estrada_index", "girth",
        }
        if metric not in known:
            return self._group_result({"success": False, "error": f"未知指标 '{metric}'"})
        if metric == "flow_hierarchy" and not self.graph.directed:
            return self._group_result({"success": False, "error": "flow_hierarchy 要求有向图"})

        if metric in {"center", "periphery", "eccentricity", "wiener_index", "barycenter"}:
            if self.graph.node_count == 0 and metric in {"center", "periphery"}:
                return self._group_result({
                    "success": False,
                    "code": "GRAPH_ALGORITHM_FAILED",
                    "error": "G has no nodes.",
                })
            weighted = metric == "barycenter"
            profiles, components, _component_id, size = self._distance_profiles(
                weighted=weighted, stem=f"grouped-{metric}"
            )
            if metric == "wiener_index":
                value = float(profiles.execute("SELECT COALESCE(SUM(total),0)/2.0 FROM profiles").fetchone()[0])
                profiles.close(); components.close()
                return self._group_result({"success": True, "metric": metric, "value": round(value, 6)})
            if metric == "eccentricity":
                def profile_rows() -> Iterator[dict[str, Any]]:
                    for node, value in profiles.execute(
                        "SELECT node,eccentricity FROM profiles ORDER BY eccentricity DESC,node"
                    ):
                        yield {"key": self._display(str(node)), "value": round(float(value), 6)}
                files, preview, count = self._write_rows("eccentricity", profile_rows())
                profiles.close(); components.close()
                return self._group_result({
                    "success": True, "metric": metric, "count": count,
                    "values": preview[:limit], "truncated": count > limit,
                    "scope": "largest_connected_component",
                }, preview, files)
            column = "total" if metric == "barycenter" else "eccentricity"
            order = "ASC" if metric in {"center", "barycenter"} else "DESC"
            extremum = profiles.execute(
                f"SELECT {column} FROM profiles ORDER BY {column} {order} LIMIT 1"
            ).fetchone()
            value = float(extremum[0]) if extremum else 0.0
            nodes = [self._display(str(row[0])) for row in profiles.execute(
                f"SELECT node FROM profiles WHERE {column}=? ORDER BY node LIMIT ?", (value, limit)
            )]
            count = int(profiles.execute(
                f"SELECT COUNT(*) FROM profiles WHERE {column}=?", (value,)
            ).fetchone()[0])
            profiles.close(); components.close()
            return self._group_result({
                "success": True, "metric": metric, "nodes": nodes, "count": count,
                "truncated": count > len(nodes), "scope": "largest_connected_component",
                "component_size": size,
            }, nodes)

        if metric == "global_efficiency":
            value = self._global_efficiency_disk()
            return self._group_result({"success": True, "metric": metric, "value": round(value, 6)})
        if metric == "local_efficiency":
            if self.graph.node_count == 0:
                return self._group_result({
                    "success": False,
                    "code": "GRAPH_ALGORITHM_FAILED",
                    "error": "division by zero",
                })
            value = self._local_efficiency_disk()
            return self._group_result({"success": True, "metric": metric, "value": round(value, 6)})
        if metric in {"average_neighbor_degree", "average_degree_connectivity"}:
            state = self._neighbor_degree_state()
            if metric == "average_neighbor_degree":
                def rows() -> Iterator[dict[str, Any]]:
                    for node, value in state.execute(
                        "SELECT node,value FROM neighbor_degree ORDER BY value DESC,node"
                    ):
                        yield {"key": self._display(str(node)), "value": round(float(value), 6)}
            else:
                def rows() -> Iterator[dict[str, Any]]:
                    for degree, value in state.execute(
                        "SELECT degree.total_value,"
                        "CASE WHEN SUM(neighbor_degree.connectivity_norm)=0 THEN "
                        "SUM(neighbor_degree.connectivity_sum) ELSE "
                        "SUM(neighbor_degree.connectivity_sum)/SUM(neighbor_degree.connectivity_norm) END "
                        "FROM degree JOIN neighbor_degree USING(node) "
                        "GROUP BY degree.total_value ORDER BY degree.total_value"
                    ):
                        yield {"key": int(degree), "value": round(float(value), 6)}
            files, preview, count = self._write_rows(metric, rows())
            state.close()
            return self._group_result({
                "success": True, "metric": metric, "count": count,
                "values": preview[:limit], "truncated": count > limit,
            }, preview, files)
        if metric == "rich_club_coefficient":
            if self.graph.has_self_loops:
                return self._group_result({
                    "success": False,
                    "code": "GRAPH_ALGORITHM_FAILED",
                    "error": "rich_club_coefficient 不支持自环",
                })
            state = self._rich_club_state()
            def rows() -> Iterator[dict[str, Any]]:
                for threshold, value in state.execute("SELECT k,value FROM coefficients ORDER BY k"):
                    yield {"key": int(threshold), "value": round(float(value), 6)}
            files, preview, count = self._write_rows(metric, rows())
            state.close()
            return self._group_result({
                "success": True, "metric": metric, "count": count,
                "values": preview[:limit], "truncated": count > limit,
            }, preview, files)
        if metric == "reciprocity":
            if not self.graph.directed and self.graph.edge_count:
                return self._group_result({
                    "success": False,
                    "code": "GRAPH_ALGORITHM_FAILED",
                    "error": "reciprocity not defined for undirected graphs",
                })
            if not self.graph.edge_count:
                value = 0.0
            elif not self.graph.directed:
                value = 1.0
            else:
                reciprocal = 0
                for source, target, _weight, _relation in self.graph.iter_edges():
                    if source != target and self.graph.edge_weight(target, source) is not None:
                        reciprocal += 1
                value = reciprocal / self.graph.edge_count
            return self._group_result({"success": True, "metric": metric, "value": round(value, 6)})
        if metric == "flow_hierarchy":
            value = self._flow_hierarchy_disk()
            return self._group_result({"success": True, "metric": metric, "value": round(value, 6)})
        if metric == "estrada_index":
            value = self._estrada_hutchinson()
            return self._group_result({
                "success": True, "metric": metric, "value": round(value, 6),
                "exact": False, "execution_mode": "disk-hutchinson-taylor",
                "probes": 8, "series_terms": 12,
            })
        girth = self._girth_disk()
        return self._group_result({
            "success": True, "metric": metric, "value": girth,
            "acyclic": girth is None,
        })

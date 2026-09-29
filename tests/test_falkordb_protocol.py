"""Live FalkorDB protocol admission against a disposable authenticated graph.

The administrator prepares and inspects state. ``query`` is the only path used
for candidate model queries; it always sends GRAPH.RO_QUERY.
"""

import json
import os
import asyncio
import base64
import hashlib
import ipaddress
import io
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest


WIRE_FIXTURE = Path(__file__).parent / "fixtures" / "falkordb_compact_path.resp2.json"
WIRE_QUERY = ("MATCH p=(a:WireA)-[r:WIRE_R]->(b:WireB) "
              "RETURN a,r,p,2.5 AS f,true AS b,9007199254740993 AS exact")


def _resp_command(*items):
    args = [str(item).encode("utf-8") for item in items]
    return (f"*{len(args)}\r\n".encode() +
            b"".join(f"${len(arg)}\r\n".encode() + arg + b"\r\n" for arg in args))


def _read_resp2(reader):
    """Small fixed-query capture parser; return both actual wire bytes and values."""
    chunks = []

    def take(length):
        assert 0 <= length <= 65536
        data = reader.read(length)
        assert len(data) == length
        chunks.append(data)
        assert sum(map(len, chunks)) <= 65536
        return data

    def parse(depth):
        assert depth <= 16
        line = reader.readline(4096)
        assert line.endswith(b"\r\n") and len(line) < 4096
        chunks.append(line)
        assert sum(map(len, chunks)) <= 65536
        marker, value = line[:1], line[1:-2]
        if marker == b"+":
            return value
        if marker == b":":
            return int(value)
        if marker == b"$":
            length = int(value)
            if length == -1:
                return None
            data = take(length + 2)
            assert data.endswith(b"\r\n")
            return data[:-2]
        if marker == b"*":
            count = int(value)
            if count == -1:
                return None
            assert 0 <= count <= 128
            return [parse(depth + 1) for _ in range(count)]
        raise AssertionError("Unexpected RESP2 marker in fixed graph reply")

    decoded = parse(0)
    return b"".join(chunks), decoded


def _wire_ro_query(uri, password, graph):
    with socket.create_connection((uri.hostname, uri.port), timeout=5) as sock:
        sock.settimeout(5)
        with sock.makefile("rb") as reader:
            sock.sendall(_resp_command("AUTH", password))
            assert reader.readline(1024) == b"+OK\r\n"
            # Capture begins only after the AUTH response has been consumed.
            sock.sendall(_resp_command("GRAPH.RO_QUERY", graph, WIRE_QUERY, "--compact"))
            return _read_resp2(reader)


def _capture(name, value):
    def serializable(item):
        if isinstance(item, bytes):
            return item.decode("utf-8", "backslashreplace")
        if isinstance(item, dict):
            return {serializable(key): serializable(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [serializable(val) for val in item]
        return item

    target = os.environ.get("IGRAPH_TEST_FALKORDB_EVIDENCE_DIR")
    if target:
        path = Path(target)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps(serializable(value), ensure_ascii=False, indent=2))


@pytest.fixture(scope="module")
def live(request):
    keys = ("IGRAPH_TEST_FALKORDB_URI", "IGRAPH_TEST_FALKORDB_PASSWORD", "IGRAPH_TEST_FALKORDB_GRAPH")
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        reason = "Live FalkorDB prerequisites missing: " + ", ".join(missing)
        if request.config.getoption("--require-live"):
            pytest.fail(reason, pytrace=False)
        pytest.skip(reason)

    import redis

    uri = urlsplit(os.environ[keys[0]])
    assert uri.scheme == "redis" and uri.hostname and uri.port and not uri.username and not uri.password
    ipaddress.ip_address(uri.hostname)  # Admission fixture never delegates a second DNS resolution.
    password = os.environ[keys[1]]
    graph = os.environ[keys[2]] + "_" + uuid4().hex[:10]
    kwargs = dict(host=uri.hostname, port=uri.port, password=password, decode_responses=False,
                  socket_connect_timeout=2, socket_timeout=10, health_check_interval=0,
                  single_connection_client=True)
    admin = redis.Redis(**kwargs)
    candidate = redis.Redis(**kwargs)
    assert admin.ping() and candidate.ping()
    admin.execute_command("GRAPH.QUERY", graph, "CREATE (:Probe {name:'seed', score:1})")
    _capture("versions", {"redis_py": redis.__version__, "server_info": admin.info("server").get("redis_version"),
                          "module_list": admin.execute_command("MODULE", "LIST"), "graph": graph})
    try:
        yield admin, candidate, graph
    finally:
        try:
            admin.execute_command("GRAPH.DELETE", graph)
        except redis.ResponseError:
            pass
        admin.close()
        candidate.close()


def _query(candidate, graph, cypher, *options):
    return candidate.execute_command("GRAPH.RO_QUERY", graph, cypher, *options)


def _count(admin, graph):
    return int(admin.execute_command("GRAPH.RO_QUERY", graph, "MATCH (n) RETURN count(n)")[1][0][0])


def test_read_query_protocol_with_writable_account(live):
    import redis

    admin, candidate, graph = live
    before = _count(admin, graph)
    read = _query(candidate, graph, "MATCH (n:Probe) RETURN count(n)")
    assert int(read[1][0][0]) == before == 1

    rejected = {}
    cases = {
        "create": "CREATE (:Probe {name:'illegal'})",
        "set": "MATCH (n:Probe) SET n.score = 2 RETURN n.score",
        "delete": "MATCH (n:Probe) DELETE n",
        "index": "CREATE INDEX FOR (n:Probe) ON (n.name)",
    }
    for name, cypher in cases.items():
        with pytest.raises(redis.ResponseError) as exc:
            _query(candidate, graph, cypher)
        rejected[name] = str(exc.value)
        assert _count(admin, graph) == before
    score = admin.execute_command("GRAPH.RO_QUERY", graph, "MATCH (n:Probe) RETURN n.score")
    assert int(score[1][0][0]) == 1
    _capture("read_only", {"read": read, "rejected": rejected, "score": score})


def test_parameters_unicode_empty_scalar_and_graph_values(live):
    admin, candidate, graph = live
    value = "李 O'Reilly \\ 雪"
    admin.execute_command("GRAPH.QUERY", graph, "CREATE (:Person {name:'李 O\\'Reilly \\\\ 雪'})-[:KNOWS {weight:2}]->(:Person {name:'二'})")
    parameter = "CYPHER target='李 O\\'Reilly \\\\ 雪' MATCH (n:Person {name:$target}) RETURN n.name"
    matched = _query(candidate, graph, parameter)
    assert matched[1][0][0].decode() == value
    empty = _query(candidate, graph, "MATCH (n:Missing) RETURN n")
    assert not empty[1]
    scalar = _query(candidate, graph, "RETURN 42 AS i, 2.5 AS f, true AS b, null AS z")
    assert len(scalar[1]) == 1
    precision = _query(candidate, graph, "RETURN 9007199254740993 AS exact_integer")
    assert precision[1][0][0] == 9007199254740993
    values = _query(candidate, graph, "MATCH p=(a:Person)-[r:KNOWS]->(b:Person) RETURN a,r,p")
    assert len(values[1]) == 1
    edge_fields = dict(values[1][0][1])
    assert edge_fields[b"src_node"] != edge_fields[b"dest_node"]
    reverse = _query(candidate, graph, "MATCH p=(b:Person)<-[r:KNOWS]-(a:Person) RETURN p")
    assert reverse[1][0][0] != values[1][0][2]
    _capture("values", {"matched": matched, "empty": empty, "scalar": scalar,
                        "precision": precision, "graph_values": values, "reverse_path": reverse})


def test_server_caps_result_rows_and_memory(live):
    import redis

    admin, candidate, graph = live
    cap = admin.execute_command("GRAPH.CONFIG", "GET", "RESULTSET_SIZE")
    mem = admin.execute_command("GRAPH.CONFIG", "GET", "QUERY_MEM_CAPACITY")
    assert cap == [b"RESULTSET_SIZE", 128]
    assert mem == [b"QUERY_MEM_CAPACITY", 8388608]
    rows = _query(candidate, graph, "UNWIND range(1, 100000) AS x RETURN x")
    assert len(rows[1]) == 128
    with pytest.raises(redis.ResponseError, match="mem consumption exceeded capacity") as exc:
        _query(candidate, graph, "UNWIND range(1, 300000) AS x RETURN collect(x)")
    assert _count(admin, graph) >= 1
    nested = _query(candidate, graph, "UNWIND range(1, 100000) AS x RETURN collect(x)")
    assert len(nested[1]) == 1 and len(nested[1][0][0]) > 500000
    _capture("budgets", {"resultset_size": cap, "query_mem_capacity": mem,
                          "requested_rows": 100000, "received_rows": len(rows[1]),
                          "memory_error": str(exc.value), "nested_value_bytes": len(nested[1][0][0]),
                          "raw_response": rows})


def test_single_connection_uses_pinned_ip(live):
    _, candidate, graph = live
    uri = urlsplit(os.environ["IGRAPH_TEST_FALKORDB_URI"])
    before = candidate.client_id()
    assert _query(candidate, graph, "RETURN 1 AS one")[1][0][0] == 1
    after = candidate.client_id()
    assert before == after
    assert candidate.connection_pool.connection_kwargs["host"] == uri.hostname
    _capture("connection", {"pinned_ip": uri.hostname, "same_client_id": before == after,
                            "client_id": before})


def test_single_bulk_value_exceeds_small_result_budget(live):
    admin, candidate, graph = live
    payload = "x" * (1024 * 1024)
    admin.execute_command("GRAPH.QUERY", graph, "CREATE (:Bulk {payload:'" + payload + "'})")
    response = _query(candidate, graph, "MATCH (n:Bulk) RETURN n.payload")
    received = response[1][0][0]
    assert len(response[1]) == 1 and len(received) == len(payload)
    _capture("large_bulk", {"row_count": len(response[1]), "bulk_bytes": len(received),
                            "first_bytes": received[:8], "last_bytes": received[-8:]})


def test_compact_response_has_typed_scalars_entities_and_path(live):
    admin, candidate, graph = live
    admin.execute_command("GRAPH.QUERY", graph,
                          "CREATE (:CompactA {name:'甲', flag:true})-[:COMPACT_R {weight:2.5}]->(:CompactB {name:'乙'})")
    scalars = _query(candidate, graph,
                     "RETURN 42 AS i, 2.5 AS f, true AS b, null AS z, 9007199254740993 AS exact_integer",
                     "--compact")
    assert [cell[0] for cell in scalars[1][0]] == [3, 5, 4, 1, 3]
    assert scalars[1][0][-1][1] == 9007199254740993
    values = _query(candidate, graph,
                    "MATCH p=(a:CompactA)-[r:COMPACT_R]->(b:CompactB) RETURN a,r,p",
                    "--compact")
    node, edge, path = values[1][0]
    assert [node[0], edge[0], path[0]] == [8, 7, 9]
    assert edge[1][2:4] == [node[1][0], path[1][0][1][1][1][0]]
    assert path[1][0][0] == 6 and path[1][1][0] == 6
    assert path[1][0][1][0][0] == 8 and path[1][1][1][0][0] == 7
    mappings = {kind: _query(candidate, graph, query, "--compact")
                for kind, query in (("labels", "CALL db.labels()"),
                                    ("relationship_types", "CALL db.relationshipTypes()"),
                                    ("property_keys", "CALL db.propertyKeys()"))}
    _capture("compact", {"scalars": scalars, "values": values, "mappings": mappings})


def test_tracked_resp2_wire_fixture_matches_live_compact_reply(live):
    admin, candidate, graph = live
    wire_graph = graph + "_wire"
    admin.execute_command("GRAPH.QUERY", wire_graph,
                          "CREATE (:WireA {name:'甲'})-[:WIRE_R {weight:2.5}]->(:WireB {name:'乙'})")
    try:
        uri = urlsplit(os.environ["IGRAPH_TEST_FALKORDB_URI"])
        password = os.environ["IGRAPH_TEST_FALKORDB_PASSWORD"]
        live_reply = _query(candidate, wire_graph, WIRE_QUERY, "--compact")
        wire, decoded = _wire_ro_query(uri, password, wire_graph)
        assert decoded[:2] == live_reply[:2]
        assert wire.startswith(b"*3\r\n")
        assert password.encode() not in wire
        if os.environ.get("IGRAPH_TEST_FALKORDB_RECORD_WIRE") == "1":
            WIRE_FIXTURE.write_text(json.dumps({
                "format": "RESP2 GRAPH.RO_QUERY --compact response after authentication",
                "encoding": "base64",
                "sha256": hashlib.sha256(wire).hexdigest(),
                "response_base64": base64.b64encode(wire).decode("ascii"),
            }, indent=2) + "\n")
        saved = json.loads(WIRE_FIXTURE.read_text())
        assert saved["format"] == "RESP2 GRAPH.RO_QUERY --compact response after authentication"
        assert saved["encoding"] == "base64"
        fixture = base64.b64decode(saved["response_base64"], validate=True)
        assert hashlib.sha256(fixture).hexdigest() == saved["sha256"]
        assert password.encode() not in fixture
        assert b"AUTH" not in fixture
        captured, expected = _read_resp2(io.BytesIO(fixture))
        assert captured == fixture
        assert expected[:2] == live_reply[:2]
        assert expected[1][0][2][0] == 9  # Path is typed in the wire reply.
    finally:
        admin.execute_command("GRAPH.DELETE", wire_graph)


def test_server_timeout(live):
    import redis

    admin, candidate, graph = live
    limits = {name: admin.execute_command("GRAPH.CONFIG", "GET", name)
              for name in ("TIMEOUT_MAX", "TIMEOUT_DEFAULT")}
    started = time.monotonic()
    with pytest.raises(redis.ResponseError) as exc:
        _query(candidate, graph, "UNWIND range(1, 10000) AS x UNWIND range(1, 10000) AS y RETURN sum(x+y)", "TIMEOUT", "1")
    elapsed = time.monotonic() - started
    assert "timed out" in str(exc.value).lower()
    assert elapsed < 10
    assert _count(admin, graph) >= 1
    _capture("timeout", {"limits": limits, "error": str(exc.value), "elapsed_seconds": elapsed})


def test_disconnected_client_has_finite_server_task(live):
    import redis.asyncio as async_redis

    admin, _, graph = live
    uri = urlsplit(os.environ["IGRAPH_TEST_FALKORDB_URI"])

    def running():
        info = admin.execute_command("GRAPH.INFO")
        assert info[0] == b"# Running queries"
        return len(info[1])

    async def exercise():
        client = async_redis.Redis(host=uri.hostname, port=uri.port,
                                   password=os.environ["IGRAPH_TEST_FALKORDB_PASSWORD"],
                                   single_connection_client=True, socket_timeout=5,
                                   health_check_interval=0)
        client_id = await client.client_id()
        query = "UNWIND range(1, 10000) AS x UNWIND range(1, 10000) AS y RETURN sum(x+y)"
        task = asyncio.create_task(client.execute_command("GRAPH.RO_QUERY", graph, query, "TIMEOUT", "1000"))
        try:
            active = 0
            for _ in range(20):
                await asyncio.sleep(0.01)
                active = running()
                if active:
                    break
            assert active > 0, "The server never exposed an in-flight graph query"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.aclose()
        disconnected = all(f"id={client_id}".encode() not in line
                           for line in admin.execute_command("CLIENT", "LIST").splitlines())
        observed = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            observed.append(running())
            if not observed[-1]:
                break
            await asyncio.sleep(0.05)
        return {"client_disconnected": disconnected, "running_before_cancel": active,
                "running_after_cancel": observed}

    evidence = asyncio.run(exercise())
    assert evidence["client_disconnected"]
    assert evidence["running_after_cancel"][-1] == 0
    _capture("disconnect", evidence)

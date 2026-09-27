#!/usr/bin/env python3
"""Destructive, bounded feasibility probe ONLY for the owned Task 7 Compose fixture.

Exit 0 = all admission gates pass; 2 = complete evidence with failed/unknown gates;
1 = harness/setup failure. No production adapter or generic query validator.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time
import uuid

from neo4j import AsyncGraphDatabase, GraphDatabase, Query, READ_ACCESS
from neo4j.graph import Node, Path as GraphPath, Relationship

URI = "bolt://127.0.0.1:21189"
CONTAINER = "igraph-memgraph-probe-memgraph-1"
SLOW = "UNWIND range(1,1000) AS x UNWIND range(1,1000) AS y UNWIND range(1,1000) AS z RETURN count(*) AS n"


def encode(v):
    if isinstance(v, GraphPath):
        return {
            "type": "path",
            "nodes": [encode(x) for x in v.nodes],
            "edges": [encode(x) for x in v.relationships],
        }
    if isinstance(v, Relationship):
        return {
            "type": "edge",
            "id": v.element_id,
            "label": v.type,
            "source": v.start_node.element_id,
            "target": v.end_node.element_id,
            "properties": dict(v),
        }
    if isinstance(v, Node):
        return {
            "type": "node",
            "id": v.element_id,
            "labels": sorted(v.labels),
            "properties": dict(v),
        }
    if isinstance(v, dict):
        return {k: encode(x) for k, x in v.items()}
    if isinstance(v, (tuple, list)):
        return [encode(x) for x in v]
    if hasattr(v, "iso_format"):
        return {"type": type(v).__name__, "value": v.iso_format()}
    return v


class Probe:
    def __init__(self, password):
        self.driver = GraphDatabase.driver(
            URI,
            auth=("qa_probe", password),
            connection_timeout=3,
            max_connection_pool_size=8,
            max_transaction_retry_time=0,
        )
        self.events = []
        self.gates = {}

    def query(self, query, params=None, *, access=None):
        start = time.monotonic()
        try:
            with self.driver.session(
                default_access_mode=access or "WRITE", fetch_size=16
            ) as session:
                result = session.run(query, params or {})
                rows = [encode(dict(row)) for row in result]
                summary = result.consume()
                return {
                    "ok": True,
                    "rows": rows,
                    "query_type": summary.query_type,
                    "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
                }
        except Exception as e:
            return {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e)[:1200],
                "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
            }

    def must(self, query, params=None):
        result = self.query(query, params)
        if not result["ok"]:
            raise RuntimeError(result)
        return result

    def snapshot(self):
        queries = [
            "MATCH (n:__IgraphTask7) RETURN properties(n) AS p ORDER BY p.id",
            "MATCH (a:__IgraphTask7)-[r]->(b:__IgraphTask7) RETURN a.id AS source,b.id AS target,type(r) AS type,properties(r) AS p ORDER BY source,target",
            "SHOW INDEX INFO",
            "SHOW USERS",
            "CALL qa_probe.counter() YIELD count RETURN count",
        ]
        return {q: self.query(q) for q in queries}

    def fixture(self):
        self.must("MATCH (n:__IgraphTask7) DETACH DELETE n")
        self.must(
            "CREATE (a:__IgraphTask7 {id:'p1', number:9007199254740993, active:true}), (b:__IgraphTask7 {id:'p2'}) CREATE (a)-[:PROBE_LINK {weight:2.5}]->(b)"
        )

    def candidate(self, query, params=None):
        """Experimental hypothesis only: inspect EXPLAIN summary before execution.

        This intentionally tests the metadata hypothesis, not an assumed guarantee.
        Only the fixed owned-fixture cases below reach this method.
        """
        plan = self.query("EXPLAIN " + query, params)
        if not plan["ok"] or plan.get("query_type") != "r":
            return {
                "rejected": True,
                "reason": "no pre-execution read classification",
                "plan": plan,
            }
        return {"rejected": False, "plan": plan, "execution": self.query(query, params)}

    @staticmethod
    def state(snapshot):
        # Query timing is not database state.
        return {
            query: {key: value for key, value in result.items() if key != "elapsed_ms"}
            for query, result in snapshot.items()
        }

    @classmethod
    def same_state(cls, before, after):
        if (
            not before
            or before.keys() != after.keys()
            or not all(
                result.get("ok") is True and "rows" in result
                for snapshot in (before, after)
                for result in snapshot.values()
            )
        ):
            return None
        return cls.state(before) == cls.state(after)

    @staticmethod
    def controls_status(events):
        statuses = [event["status"] for event in events if event["direct"]["ok"]]
        if "fail" in statuses:
            return "fail"
        if not statuses or "unverified" in statuses:
            return "unverified"
        return "pass"

    def negative(self, name, query, cleanup=None, params=None):
        self.fixture()
        if cleanup:
            self.query(cleanup)
        before = self.snapshot()
        direct = self.query(query, params)
        after_positive = self.snapshot()
        if cleanup:
            self.query(cleanup)
        self.fixture()
        candidate_before = self.snapshot()
        candidate = self.candidate(query, params)
        candidate_after = self.snapshot()
        positive_unchanged = self.same_state(before, after_positive)
        positive_changed = (
            None if positive_unchanged is None else not positive_unchanged
        )
        unchanged = self.same_state(candidate_before, candidate_after)
        if not direct["ok"] or positive_changed is None or unchanged is None:
            status = "unverified"
        elif positive_changed and candidate["rejected"] and unchanged:
            status = "pass"
        else:
            status = "fail"
        self.events.append(
            {
                "case": name,
                "query": query,
                "direct": direct,
                "positive_before": before,
                "positive_after": after_positive,
                "positive_changed_state": positive_changed,
                "candidate": candidate,
                "before": candidate_before,
                "after": candidate_after,
                "state_unchanged": unchanged,
                "status": status,
            }
        )
        if cleanup:
            self.query(cleanup)
        self.fixture()

    @staticmethod
    def deadline_status(timeout, server_stopped):
        return (
            "pass"
            if not timeout["ok"]
            and "time" in timeout.get("error", "").lower()
            and timeout["elapsed_ms"] < 5000
            and server_stopped.get("ok") is True
            and "rows" in server_stopped
            and not any(SLOW in str(row) for row in server_stopped["rows"])
            else "unverified"
        )

    def run(self, password):
        version = self.must("SHOW VERSION")
        self.fixture()
        reads = {}
        samples = [
            ("scalar", "RETURN $value AS v", {"value": "中文 '; CREATE (:Evil) //"}),
            (
                "params",
                "MATCH (n:__IgraphTask7 {id:$id}) RETURN n.id AS id",
                {"id": "p1"},
            ),
            (
                "types",
                "MATCH p=(a:__IgraphTask7)-[r:PROBE_LINK]->(b) RETURN a,r,p,9007199254740993 AS big,true AS flag,null AS empty",
                {},
            ),
            ("empty", "MATCH (n:__IgraphTask7 {id:'missing'}) RETURN n", {}),
            ("list", "RETURN $values AS v", {"values": [1, 2, 3]}),
        ]
        for name, q, p in samples:
            reads[name] = self.must(q, p)
        assert reads["scalar"]["rows"][0]["v"] == samples[0][2]["value"]
        assert reads["params"]["rows"] == [{"id": "p1"}]
        assert reads["types"]["rows"][0]["big"] == 9007199254740993
        assert reads["types"]["rows"][0]["p"]["type"] == "path"
        assert reads["empty"]["rows"] == []
        schema = {
            q: self.query(q)
            for q in [
                "SHOW NODE_LABELS INFO",
                "SHOW EDGE_TYPES INFO",
                "SHOW INDEX INFO",
                "CALL schema.node_type_properties() YIELD *",
                "CALL schema.rel_type_properties() YIELD *",
            ]
        }
        candidate_read = self.candidate("RETURN 1 AS ok")
        self.gates["candidate_read"] = (
            "pass" if not candidate_read["rejected"] else "fail"
        )
        cases = [
            ("create", "CREATE (:__IgraphTask7 {id:'canary'})", None),
            (
                "set",
                "MATCH (n:__IgraphTask7 {id:'p1'}) SET n.changed=true RETURN n",
                None,
            ),
            ("merge", "MERGE (:__IgraphTask7 {id:'canary'})", None),
            ("delete", "MATCH (n:__IgraphTask7 {id:'p1'}) DETACH DELETE n", None),
            (
                "ddl",
                "CREATE INDEX ON :__IgraphTask7(id)",
                "DROP INDEX ON :__IgraphTask7(id)",
            ),
            (
                "admin",
                f"CREATE USER qa_canary IDENTIFIED BY '{password}'",
                "DROP USER qa_canary",
            ),
            ("procedure", "CALL qa_probe.touch() YIELD count RETURN count", None),
            ("comment", "/* before */ CREATE (:__IgraphTask7 {id:'canary'})", None),
            ("multi", "RETURN 1; CREATE (:__IgraphTask7 {id:'canary'})", None),
        ]
        for name, q, cleanup in cases:
            self.negative(
                name, q, cleanup, {"password": password} if name == "admin" else None
            )
        self.gates["reject_all_controls"] = self.controls_status(self.events)
        self.gates["usable_pre_execution_safety"] = "fail"
        route_only = self.query(
            "CREATE (:__IgraphTask7 {id:'read-route'})", access=READ_ACCESS
        )
        self.fixture()
        # One connection configuration, independent request-owned sessions; sharing a
        # Session object across threads is outside the driver's supported contract.
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.query, "RETURN $token AS token", {"token": token})
                for token in ["a", "b"]
            ]
            concurrent = [f.result(timeout=5) for f in futures]
        assert [r["rows"][0]["token"] for r in concurrent] == ["a", "b"]
        self.gates["concurrency"] = "pass"
        timeout = self.query(SLOW)
        server_stopped = self.query("SHOW TRANSACTIONS")
        self.gates["server_deadline"] = self.deadline_status(timeout, server_stopped)
        short_deadline = self.query(Query(SLOW, timeout=0.1))
        cancel = self.cancel_probe()
        disconnect = asyncio.run(self.disconnect_probe(password))
        self.gates["active_cancel"] = cancel["status"]
        large = self.must("RETURN $s AS cell", {"s": "x" * 65536})
        self.gates["pre_materialization_bytes"] = "fail"
        self.gates["tls_and_rebinding"] = "unverified"
        self.gates["native_read_params_types"] = "pass"
        self.gates["schema"] = (
            "pass" if all(x["ok"] for x in schema.values()) else "unverified"
        )
        return {
            "product": "Memgraph",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client": importlib.metadata.version("neo4j"),
            "uri": URI,
            "version": version,
            "reads": reads,
            "schema": schema,
            "candidate_read": candidate_read,
            "negative_controls": self.events,
            "read_access_write_control": route_only,
            "concurrency": concurrent,
            "server_timeout": timeout,
            "transactions_after_timeout": server_stopped,
            "cancel": cancel,
            "short_request_deadline": short_deadline,
            "client_disconnect": disconnect,
            "bounded_result": {
                "one_cell_bytes": len(large["rows"][0]["cell"]),
                "budget": 4096,
                "driver_materialized_over_budget": True,
            },
            "gates": self.gates,
            "admitted": all(v == "pass" for v in self.gates.values()),
        }

    async def disconnect_probe(self, password):
        driver = AsyncGraphDatabase.driver(
            URI, auth=("qa_probe", password), max_transaction_retry_time=0
        )
        query = SLOW + " /* owned-client-disconnect */"

        async def worker():
            async with driver.session() as session:
                try:
                    result = await session.run(query)
                    await result.consume()
                except asyncio.CancelledError:
                    session.cancel()
                    raise

        task = asyncio.create_task(worker())
        observed = None
        initial_polls = []
        def valid_observation(result):
            return (result.get("ok") is True and isinstance(result.get("rows"), list)
                    and all(isinstance(row, dict) and isinstance(row.get("query"), list)
                            and all(isinstance(text, str) for text in row["query"])
                            for row in result["rows"]))
        for _ in range(30):
            observation = await asyncio.to_thread(self.query, "SHOW TRANSACTIONS")
            valid = valid_observation(observation)
            initial_polls.append({"observation": observation, "valid": valid})
            if not valid:
                break
            observed = next((row for row in observation["rows"] if query in str(row)), None)
            if observed or task.done():
                break
            await asyncio.sleep(0.01)
        start = time.monotonic()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await driver.close()
        polls = []
        while time.monotonic() - start < 3:
            r = await asyncio.to_thread(self.query, "SHOW TRANSACTIONS")
            valid = valid_observation(r)
            running = any(query in str(row) for row in r["rows"]) if valid else None
            polls.append(
                {
                    "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
                    "running": running,
                    "observation": r,
                    "valid": valid,
                }
            )
            if running is not True:
                break
            await asyncio.sleep(0.05)
        stopped = bool(observed and polls and polls[-1]["running"] is False)
        return {
            "observed": observed,
            "initial_polls": initial_polls,
            "polls": polls,
            "status": "pass" if stopped else "unverified",
            "server_stopped": stopped,
        }

    def cancel_probe(self):
        owned_query = SLOW + " /* cancel-" + uuid.uuid4().hex + " */"
        with ThreadPoolExecutor(max_workers=1) as executor:
            started = time.monotonic()
            future = executor.submit(self.query, owned_query)
            selected = None
            for _ in range(25):
                rows = self.query("SHOW TRANSACTIONS").get("rows", [])
                selected = next((r for r in rows if owned_query in str(r)), None)
                if selected or future.done():
                    break
                time.sleep(0.02)
            kill = None
            if selected:
                # Column names are recorded; only an ID observed with our exact
                # owned query text can be targeted. Never cancel all transactions.
                transaction_id = selected.get("transaction_id")
                if transaction_id is not None:
                    assert str(transaction_id).isdecimal()
                    kill = self.query(
                        "TERMINATE TRANSACTIONS '" + str(transaction_id) + "'"
                    )
            result = future.result(timeout=5)
            after = self.query("SHOW TRANSACTIONS")
            ended = after["ok"] and not any(
                owned_query in str(r) for r in after.get("rows", [])
            )
            passed = bool(
                kill
                and kill["ok"]
                and ended
                and not result["ok"]
                and "abort by another user" in result.get("error", "").lower()
            )
            return {
                "status": "pass" if passed else "unverified",
                "owned_query": owned_query,
                "observed": selected,
                "kill": kill,
                "worker": result,
                "after": after,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    info = json.loads(subprocess.check_output(["docker", "inspect", CONTAINER]))[0]
    assert (
        info["Config"]["Labels"]["com.docker.compose.project"]
        == "igraph-memgraph-probe"
    )
    assert all(
        m.get("Name", "").startswith("igraph-memgraph-probe_")
        for m in info["Mounts"]
        if m["Type"] == "volume"
    )
    password = json.loads(args.credentials.read_text())["memgraph"]
    probe = Probe(password)
    assert len(password) == 48 and all(c in "0123456789abcdef" for c in password)
    if args.bootstrap:
        users = probe.query("SHOW USERS")
        if users["ok"] and not users["rows"]:
            probe.must(f"CREATE USER qa_probe IDENTIFIED BY '{password}'")
            probe.driver.close()
            probe = Probe(password)
    assert probe.must("SHOW USERS")[
        "rows"
    ], "Authenticated QA account required; run --bootstrap once"
    wrong = GraphDatabase.driver(URI, auth=("qa_probe", "intentionally-wrong-password"))
    try:
        wrong.verify_connectivity()
    except Exception as e:
        auth = {
            "rejected": True,
            "error_type": type(e).__name__,
            "code": getattr(e, "code", None),
            "error": str(e)[:500],
        }
        assert "auth" in str(e).lower() or "credential" in str(e).lower(), auth
    else:
        raise AssertionError("Authentication is not enforced")
    finally:
        wrong.close()
    try:
        result = probe.run(password)
        result["wrong_password"] = auth
        result["container"] = {k: info[k] for k in ["Image", "Platform"]}
        result["container"]["limits"] = {
            k: info["HostConfig"][k] for k in ["Memory", "NanoCpus", "PidsLimit"]
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Never persist credentials even if a driver error echoes query parameters.
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2).replace(
                password, "[REDACTED]"
            )
            + "\n"
        )
        print(
            json.dumps(
                {
                    "admitted": result["admitted"],
                    "gates": result["gates"],
                    "output": str(args.output),
                }
            )
        )
        return 0 if result["admitted"] else 2
    finally:
        probe.driver.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Owned NebulaGraph Task 7 fixture probe, not a production adapter.

Exit 2 records failed/unknown admission gates. Only the dedicated Compose instance
and fixed qa_task7 space may be mutated. No model-provided query is accepted.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time
import threading

from nebula3.Config import Config
from nebula3.gclient.net import ConnectionPool
from nebula3.gclient.net.Connection import Connection

SPACE = "qa_task7"
SLOW = "UNWIND range(1,1000) AS x UNWIND range(1,1000) AS y RETURN count(*) AS n"


def thrift(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [thrift(x) for x in v]
    if isinstance(v, dict):
        return {str(thrift(k)): thrift(x) for k, x in v.items()}
    if hasattr(v, "__dict__"):
        return {"_type": type(v).__name__, **{k: thrift(x) for k, x in vars(v).items()}}
    raise TypeError(type(v).__name__)


class Probe:
    def __init__(self, password):
        self.password = password
        config = Config()
        config.max_connection_pool_size = 8
        config.timeout = 4000
        self.pool = ConnectionPool()
        assert self.pool.init([("127.0.0.1", 21190)], config)
        self.events = []

    def query(self, q, params=None, *, space=True, session=None):
        start = time.monotonic()
        owned = session is None
        try:
            if owned:
                session = self.pool.get_session(
                    "root", self.password, retry_connect=False
                )
                if space:
                    use = session.execute("USE " + SPACE)
                    if not use.is_succeeded():
                        raise RuntimeError(use.error_msg())
            result = session.execute_py(q, params)
            rows = [
                [
                    {"type": v._value.getType(), "value": thrift(v.cast_primitive())}
                    for v in result.row_values(i)
                ]
                for i in range(result.row_size())
            ]
            return {
                "ok": True,
                "columns": result.keys(),
                "rows": rows,
                "space": result.space_name(),
                "plan": thrift(result.plan_desc()),
                "session_id": session._session_id,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
            }
        except Exception as e:
            return {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e)[:1500],
                "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
            }
        finally:
            if owned and session is not None:
                session.release()

    def must(self, q, params=None, **kwargs):
        result = self.query(q, params, **kwargs)
        if not result["ok"]:
            raise RuntimeError(result)
        return result

    def retry(self, q):
        for _ in range(30):
            r = self.query(q)
            if r["ok"]:
                return r
            time.sleep(0.5)
        raise RuntimeError(r)

    def setup(self):
        hosts = self.must("SHOW HOSTS STORAGE", space=False)
        if not hosts["rows"]:
            self.must('ADD HOSTS "storaged":9779', space=False)
        # Wait for registration and heartbeat before allocating a partition.
        for _ in range(30):
            r = self.query(
                "CREATE SPACE IF NOT EXISTS "
                + SPACE
                + " (partition_num=1,replica_factor=1,vid_type=FIXED_STRING(32))",
                space=False,
            )
            if r["ok"]:
                break
            time.sleep(0.5)
        assert r["ok"], r
        self.retry("CREATE TAG IF NOT EXISTS Probe(id string, number int, active bool)")
        self.retry("CREATE EDGE IF NOT EXISTS PROBE_LINK(weight double)")
        self.fixture()

    def fixture(self):
        self.retry("CREATE TAG IF NOT EXISTS Probe(id string, number int, active bool)")
        self.retry(
            'INSERT VERTEX Probe(id,number,active) VALUES "p1":("p1",9007199254740993,true), "p2":("p2",2,false)'
        )
        self.retry('INSERT EDGE PROBE_LINK(weight) VALUES "p1"->"p2":(2.5)')
        self.must('DELETE VERTEX "canary" WITH EDGE')

    def snapshot(self):
        return {
            q: self.query(q)
            for q in [
                'FETCH PROP ON Probe "p1","p2","canary" YIELD vertex AS v',
                "SHOW TAGS",
                "SHOW EDGES",
                "SHOW USERS",
                "SHOW JOBS",
            ]
        }

    @staticmethod
    def state(snapshot):
        # Session IDs/timing/job progress are observations, not fixture state.
        return {
            q: {
                "ok": r["ok"],
                "rows": [
                    [v for i, v in enumerate(row) if q != "SHOW JOBS" or i == 0]
                    for row in r.get("rows", [])
                ],
            }
            for q, r in snapshot.items()
        }

    def candidate(self, q, params=None):
        plan = self.query("EXPLAIN { " + q + "; }", params)
        # 3.8 plan_desc supplies operator descriptions, not a query effect class.
        # A full operator/grammar validator is outside this feasibility task.
        return {
            "rejected": True,
            "reason": "No verified pre-execution semantic classifier; operator-plan presence alone is not admission",
            "plan": plan,
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

    def negative(self, name, q, cleanup=None):
        self.fixture()
        if cleanup:
            self.query(cleanup)
        before = self.snapshot()
        direct = self.query(q)
        positive_after = self.snapshot()
        if cleanup:
            self.query(cleanup)
        self.fixture()
        candidate_before = self.snapshot()
        candidate = self.candidate(q)
        candidate_after = self.snapshot()
        positive_unchanged = self.same_state(before, positive_after)
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
                "query": q,
                "direct": direct,
                "positive_before": before,
                "positive_after": positive_after,
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

    def cancel_probe(self):
        worker_session = self.pool.get_session(
            "root", self.password, retry_connect=False
        )
        observer_session = self.pool.get_session(
            "root", self.password, retry_connect=False
        )
        worker_session.execute("USE " + SPACE)
        observer_session.execute("USE " + SPACE)
        started = threading.Event()

        def worker():
            started.set()
            return self.query(SLOW, session=worker_session)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker)
            assert started.wait(2), "Worker did not start"
            selected = None
            observed = []
            for _ in range(30):
                r = self.query("SHOW LOCAL QUERIES", session=observer_session)
                observed.append(r)
                for row in r.get("rows", []):
                    record = dict(zip(r["columns"], [v["value"] for v in row]))
                    if (
                        SLOW in str(record)
                        and record.get("SessionID") == worker_session._session_id
                    ):
                        selected = record
                        break
                if selected or future.done():
                    break
                time.sleep(0.005)
            kill = None
            if selected:
                sid = selected.get("SessionID")
                pid = selected.get("ExecutionPlanID")
                if isinstance(sid, int) and isinstance(pid, int):
                    kill = self.query(
                        f"KILL QUERY(SESSION={sid},PLAN={pid})",
                        session=observer_session,
                    )
            result = future.result(timeout=6)
            worker_session.release()
            observer_session.release()
            after = self.query("SHOW LOCAL QUERIES")
            stopped = after["ok"] and not any(
                SLOW in str(r) for r in after.get("rows", [])
            )
            passed = bool(
                kill
                and kill["ok"]
                and not result["ok"]
                and "kill" in result.get("error", "").lower()
                and stopped
            )
            return {
                "status": "pass" if passed else "unverified",
                "observed": observed,
                "selected": selected,
                "kill": kill,
                "worker": result,
                "after": after,
                "work_bound": "1000000 row product from two 1000-element lists; no larger retry",
            }

    def run(self):
        self.setup()
        reads = {
            q: self.query(q, p)
            for q, p in [
                (
                    "RETURN $value AS v",
                    {
                        "value": "中文 '; INSERT VERTEX Probe(id) VALUES 'evil':('x'); //"
                    },
                ),
                ("MATCH (v:Probe) WHERE id(v) IN $ids RETURN v", {"ids": ["p1"]}),
                ("FETCH PROP ON Probe $id YIELD vertex AS v", {"id": "p1"}),
                (
                    'MATCH p=(a:Probe)-[r:PROBE_LINK]->(b:Probe) WHERE id(a)=="p1" RETURN a,r,p,9007199254740993 AS big,true AS flag,NULL AS empty',
                    None,
                ),
                ('FETCH PROP ON Probe "missing" YIELD vertex AS v', None),
            ]
        }
        assert (
            reads["RETURN $value AS v"]["rows"][0][0]["value"]
            == "中文 '; INSERT VERTEX Probe(id) VALUES 'evil':('x'); //"
        )
        assert (
            reads["MATCH (v:Probe) WHERE id(v) IN $ids RETURN v"]["rows"][0][0][
                "value"
            ]["vid"]
            == "p1"
        )
        typed = next(r for q, r in reads.items() if q.startswith("MATCH p="))
        assert typed["ok"] and [v["type"] for v in typed["rows"][0]] == [
            9,
            10,
            11,
            3,
            2,
            1,
        ]
        assert typed["rows"][0][3]["value"] == 9007199254740993
        schema = {
            q: self.must(q)
            for q in [
                "SHOW TAGS",
                "DESCRIBE TAG Probe",
                "SHOW EDGES",
                "DESCRIBE EDGE PROBE_LINK",
                "SHOW TAG INDEXES",
                "SHOW EDGE INDEXES",
            ]
        }
        for name, q, cleanup in [
            ("insert", 'INSERT VERTEX Probe(id) VALUES "canary":("x")', None),
            ("update", 'UPDATE VERTEX ON Probe "p1" SET id="mutated"', None),
            ("delete", 'DELETE VERTEX "p1" WITH EDGE', None),
            ("ddl_create", "CREATE TAG Canary(x string)", "DROP TAG Canary"),
            ("ddl_drop", "DROP TAG Probe", None),
            (
                "admin",
                f'CREATE USER qa_canary WITH PASSWORD "{self.password}"',
                "DROP USER qa_canary",
            ),
            ("job", "SUBMIT JOB STATS", None),
            (
                "comment",
                '/* before */ INSERT VERTEX Probe(id) VALUES "canary":("x")',
                None,
            ),
            ("multi", 'RETURN 1; INSERT VERTEX Probe(id) VALUES "canary":("x")', None),
            ("procedure", "CALL example()", None),
            ("cypher_write", 'CREATE (:Probe {id:"canary"})', None),
        ]:
            self.negative(name, q, cleanup)
        candidate_read = self.candidate("RETURN 1 AS ok")
        with ThreadPoolExecutor(max_workers=2) as executor:
            fs = [
                executor.submit(self.query, "RETURN $token AS token", {"token": x})
                for x in ["a", "b"]
            ]
            concurrent = [f.result(timeout=6) for f in fs]
        assert [r["rows"][0][0]["value"] for r in concurrent] == ["a", "b"]
        cancel = self.cancel_probe()
        large = self.must("RETURN $value AS v", {"value": "x" * 65536})
        configs = {
            q: self.query(q)
            for q in ["SHOW HOSTS GRAPH", "SHOW HOSTS STORAGE", "SHOW CONFIGS GRAPH"]
        }
        gates = {
            "native_read_params_types": "pass",
            "schema": "pass",
            "candidate_read": "fail",
            "reject_all_controls": self.controls_status(self.events),
            "usable_pre_execution_safety": "fail",
            "concurrency": "pass",
            "active_cancel": cancel["status"],
            "server_deadline": "unverified",
            "pre_materialization_bytes": "fail",
            "tls_and_rebinding": "unverified",
        }
        return {
            "product": "NebulaGraph",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client": importlib.metadata.version("nebula3-python"),
            "endpoint": "127.0.0.1:21190",
            "space": SPACE,
            "reads": reads,
            "schema": schema,
            "negative_controls": self.events,
            "candidate_read": candidate_read,
            "concurrency": concurrent,
            "cancel": cancel,
            "runtime": configs,
            "bounded_result": {
                "one_cell_bytes": len(large["rows"][0][0]["value"]),
                "budget": 4096,
                "driver_materialized_over_budget": True,
            },
            "gates": gates,
            "admitted": False,
        }


def authentication_probe(password):
    """Own the socket: SDK 3.8.3 get_session leaks the lease on auth failure."""
    connection = Connection()
    try:
        connection.open("127.0.0.1", 21190, 4000)
        auth = connection.authenticate("root", password)
        connection.signout(auth.get_session_id())
        return {"ok": True}
    except Exception as error:
        return {"ok": False, "error_type": type(error).__name__, "error": str(error)}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    containers = {}
    for name in ["metad", "storaged", "graphd"]:
        info = json.loads(
            subprocess.check_output(
                ["docker", "inspect", "igraph-nebula-probe-" + name + "-1"]
            )
        )[0]
        assert (
            info["Config"]["Labels"]["com.docker.compose.project"]
            == "igraph-nebula-probe"
        )
        assert all(
            m.get("Name", "").startswith("igraph-nebula-probe_")
            for m in info["Mounts"]
            if m["Type"] == "volume"
        )
        containers[name] = {
            "image": info["Image"],
            "limits": {
                k: info["HostConfig"][k] for k in ["Memory", "NanoCpus", "PidsLimit"]
            },
        }
    password = json.loads(args.credentials.read_text())["nebula"]
    assert len(password) == 24 and all(c in "0123456789abcdef" for c in password)
    probe = Probe(password)
    if args.bootstrap and not authentication_probe(password)["ok"]:
        session = probe.pool.get_session("root", "nebula", retry_connect=False)
        result = session.execute(f'CHANGE PASSWORD root FROM "nebula" TO "{password}"')
        session.release()
        assert result.is_succeeded(), "Root bootstrap failed"
        time.sleep(2)
        probe.pool.close()
        probe = Probe(password)
    auth = authentication_probe("intentionally-wrong-password")
    assert (
        not auth["ok"] and auth["error_type"] == "AuthFailedException"
    ), "Authentication is not enforced"
    try:
        result = probe.run()
        result["containers"] = containers
        result["wrong_password"] = auth
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2).replace(
                password, "[REDACTED]"
            )
            + "\n"
        )
        print(
            json.dumps(
                {
                    "admitted": False,
                    "gates": result["gates"],
                    "output": str(args.output),
                }
            )
        )
        return 2
    finally:
        probe.pool.close()


if __name__ == "__main__":
    raise SystemExit(main())

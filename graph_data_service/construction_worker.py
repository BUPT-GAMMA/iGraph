"""Fixed construction entry point; accepts only a service-created request file."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time


def main():
    from execution_lifecycle import atomic_json
    import psutil
    identity_path = os.environ["IGRAPH_WORKER_IDENTITY_FILE"]
    pid = os.getpid()
    if os.getpgrp() != pid:
        raise RuntimeError("WORKER_REQUIRES_PRIVATE_PROCESS_GROUP")
    atomic_json(Path(identity_path), {"pid": pid, "started_at": psutil.Process(pid).create_time(), "process_group": pid})
    first = sys.stdin.readline()
    if not first:
        return
    payload = json.loads(first)
    sys.stdout = sys.stderr
    stopping = threading.Event()

    def stop(*_args):
        stopping.set()

    def control():
        for line in sys.stdin:
            try:
                if json.loads(line).get("control") == "cancel":
                    stopping.set()
            except ValueError:
                continue
        # Parent death cannot strand an uncooperative worker or its children.
        stopping.set()
        time.sleep(2)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(1)
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=control, daemon=True).start()
    from graph_engine.construction.agent import TabularConstructionSubAgent, ConstructionFailed
    from graph_engine.construction.budget import deadline_scope, check_deadline
    from graph_data_service.construction import _atomic_json
    request_path = Path(payload["request_path"])
    request = json.loads(request_path.read_text())
    build_id = payload["build_id"]
    if request["build_id"] != build_id or request_path.parent.name != build_id:
        raise ValueError("WORKER_REQUEST_IDENTITY_MISMATCH")
    result = {"build_id": build_id}
    try:
        with deadline_scope(payload["deadline"]):
            check_deadline()
            if stopping.is_set():
                raise InterruptedError("WORKER_CANCELLED")
            graph = TabularConstructionSubAgent(data_root=request["data_root"])(Path(request["source"]), request["options"])
            check_deadline()
            if stopping.is_set():
                raise InterruptedError("WORKER_CANCELLED")
            artifact = request_path.parent / "graph.json"
            _atomic_json(artifact, graph, before_replace=check_deadline)
            with artifact.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            check_deadline()
            result.update(filename="graph.json", sha256=digest)
    except ConstructionFailed as exc:
        result["failure_code"] = exc.code
    except TimeoutError:
        result["failure_code"] = "construction_time_budget_exhausted"
    except InterruptedError:
        result["failure_code"] = "construction_interrupted"
    sys.__stdout__.write(json.dumps({"type": "result", "value": result}) + "\n")
    sys.__stdout__.flush()


if __name__ == "__main__":
    main()

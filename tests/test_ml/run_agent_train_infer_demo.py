from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):
        def _load_file(path: Path) -> bool:
            if not path.exists() or not path.is_file():
                return False
            loaded = False
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                if key not in os.environ:
                    os.environ[key] = value
                loaded = True
            return loaded

        return _load_file(Path.cwd() / ".env")

load_dotenv()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"缺少环境变量: {name}（请在 .env 中配置）")
    return value.strip()


def require_int_env(name: str) -> int:
    raw = require_env(name)
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值: {raw}") from e


def _request(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 8.0,
) -> Tuple[int, Dict[str, Any]]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"raw": body}
            return int(resp.status), parsed
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return int(e.code), parsed


def _require_ok(name: str, status: int, body: Dict[str, Any]) -> None:
    if status >= 400:
        raise RuntimeError(f"{name} 失败: status={status}, body={body}")


def _wait_health(
    name: str,
    url: str,
    timeout: float,
    process: Optional[subprocess.Popen[Any]] = None,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"{name} 进程提前退出，exit_code={process.returncode}")
        try:
            status, body = _request("GET", url, timeout=2.0)
            if status < 400 and isinstance(body, dict) and body.get("status") == "ok":
                print(f"[AgentDemo] {name} healthy -> {body}")
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"等待 {name} /health 超时: {url}")


def _stop_process(name: str, process: Optional[subprocess.Popen[Any]]) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    print(f"[AgentDemo] stop {name}")


def _ensure_dataset(
    root_dir: Path,
    python_executable: str,
    graph_path: Path,
    groundtruth_path: Path,
) -> None:
    if graph_path.exists() and groundtruth_path.exists():
        return
    gen_script = root_dir / "tests" / "test_ml" / "generate_synthetic_gender_graph.py"
    cmd = [
        python_executable,
        str(gen_script),
        "--output",
        str(graph_path),
        "--groundtruth-output",
        str(groundtruth_path),
    ]
    print(f"[AgentDemo] 生成数据文件: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(root_dir), check=True)


def _pick_unlabeled_nodes(graph_path: Path, k: int) -> list[str]:
    with graph_path.open("r", encoding="utf-8") as f:
        doc = json.load(f)
    id2node = doc.get("id2node", {})
    node_label = doc.get("node_label", [])

    names: list[str] = []
    for i, lb in enumerate(node_label):
        if lb is None:
            name = id2node.get(str(i))
            if isinstance(name, str):
                names.append(name)
            if len(names) >= k:
                break

    if names:
        return names

    for i in range(min(k, len(node_label))):
        name = id2node.get(str(i))
        if isinstance(name, str):
            names.append(name)
    return names


def _check_graph_data_dependencies() -> None:
    required = [
        ("uvicorn", "pip install uvicorn"),
        ("fastapi", "pip install fastapi"),
    ]
    missing: list[str] = []
    for mod, hint in required:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(f"{mod} ({hint})")
    if missing:
        raise RuntimeError("缺少运行依赖: " + ", ".join(missing))


def _maybe_eval_against_groundtruth(
    infer_body: Dict[str, Any],
    groundtruth_path: Path,
) -> None:
    if not groundtruth_path.exists():
        return
    preds = infer_body.get("predictions")
    if not isinstance(preds, list):
        return

    with groundtruth_path.open("r", encoding="utf-8") as f:
        gt = json.load(f)
    id2node = gt.get("id2node", {})
    labels = gt.get("node_label_groundtruth", [])
    if not isinstance(id2node, dict) or not isinstance(labels, list):
        return

    node_to_gt: Dict[str, str] = {}
    for k, v in id2node.items():
        try:
            idx = int(k)
        except Exception:
            continue
        if 0 <= idx < len(labels):
            node_to_gt[str(v)] = str(labels[idx])

    total = 0
    correct = 0
    for item in preds:
        if not isinstance(item, dict):
            continue
        node = item.get("node")
        pred = item.get("pred_label")
        if not isinstance(node, str):
            continue
        gt_label = node_to_gt.get(node)
        if gt_label is None:
            continue
        total += 1
        if str(pred) == gt_label:
            correct += 1

    if total > 0:
        print(f"[AgentDemo] infer subset accuracy={correct/total:.4f} ({correct}/{total})")


def run(args: argparse.Namespace) -> None:
    root_dir = Path(__file__).resolve().parents[2]
    graph_path = Path(args.graph_file).resolve()
    groundtruth_path = Path(args.groundtruth_file).resolve()
    session_id = args.session_id or f"agent-demo-{uuid.uuid4().hex[:8]}"
    graph_data_host = require_env("GRAPH_DATA_HOST")
    graph_data_port = require_int_env("GRAPH_DATA_PORT")
    data_base = require_env("GRAPH_DATA_SERVER_URL").rstrip("/")

    _ensure_dataset(
        root_dir=root_dir,
        python_executable=args.python,
        graph_path=graph_path,
        groundtruth_path=groundtruth_path,
    )
    _check_graph_data_dependencies()

    print(f"[AgentDemo] session_id={session_id}")
    print(f"[AgentDemo] graph_file={graph_path}")
    print(f"[AgentDemo] graph_data_base={data_base}")

    data_proc: Optional[subprocess.Popen[Any]] = None
    try:
        data_env = os.environ.copy()
        data_env["GRAPH_DATA_HOST"] = graph_data_host
        data_env["GRAPH_DATA_PORT"] = str(graph_data_port)
        data_proc = subprocess.Popen(
            [args.python, "-m", "graph_data_service"],
            cwd=str(root_dir),
            env=data_env,
        )
        _wait_health("graph_data_service", f"{data_base}/health", args.health_timeout, data_proc)

        status, body = _request("POST", f"{data_base}/sessions/{session_id}", timeout=args.timeout)
        _require_ok("create_session", status, body)

        register_payload = {
            "file_token": f"local-{uuid.uuid4().hex[:8]}",
            "original_filename": graph_path.name,
            "saved_path": str(graph_path),
        }
        status, body = _request(
            "POST",
            f"{data_base}/sessions/{session_id}/uploads/register",
            payload=register_payload,
            timeout=args.timeout,
        )
        _require_ok("register_upload", status, body)

        status, body = _request(
            "POST",
            f"{data_base}/sessions/{session_id}/graph/load_upload",
            payload={"filename": graph_path.name, "append": False},
            timeout=args.timeout,
        )
        _require_ok("load_upload", status, body)
        if not body.get("success", False):
            raise RuntimeError(f"load_upload 返回失败: {body}")
        print(
            "[AgentDemo] graph loaded -> "
            f"nodes={body.get('num_nodes')} edges={body.get('num_edges')}"
        )

        train_payload: Dict[str, Any] = {
            "task": "node_classification",
            "model": args.model,
            "data": {"feature_source": "graph"},
            "train_config": {
                "split": {"train": 0.8, "val": 0.2},
                "epochs": args.epochs,
                "lr": args.lr,
                "weight_decay": args.weight_decay,
                "seed": args.seed,
                "early_stop_patience": args.early_stop_patience,
                "early_stop_min_delta": args.early_stop_min_delta,
            },
            "algo_config": {
                "hidden_dim": args.hidden_dim,
                "embedding_dim": args.embedding_dim,
                "num_layers": args.num_layers,
                "dropout": args.dropout,
                "device": args.device,
            },
        }
        status, body = _request(
            "POST",
            f"{data_base}/sessions/{session_id}/tools/train",
            payload={"arguments": train_payload},
            timeout=args.timeout,
        )
        _require_ok("train", status, body)
        if not body.get("success", False):
            raise RuntimeError(f"train 返回失败: {body}")
        model_id = str(body.get("model_id", "")).strip()
        if not model_id:
            raise RuntimeError(f"train 缺少 model_id: {body}")
        print(f"[AgentDemo] train success -> model_id={model_id}")
        print(f"[AgentDemo] metrics={body.get('metrics')}")

        infer_nodes = _pick_unlabeled_nodes(graph_path, args.infer_count)
        infer_payload = {
            "model_id": model_id,
            "query": {"nodes": infer_nodes},
            "infer_config": {"return_prob": True},
        }
        status, body = _request(
            "POST",
            f"{data_base}/sessions/{session_id}/tools/infer",
            payload={"arguments": infer_payload},
            timeout=args.timeout,
        )
        _require_ok("infer", status, body)
        if not body.get("success", False):
            raise RuntimeError(f"infer 返回失败: {body}")
        predictions = body.get("predictions") or []
        print(f"[AgentDemo] infer success -> count={body.get('count')}")
        if predictions:
            print(f"[AgentDemo] first_prediction={predictions[0]}")
        _maybe_eval_against_groundtruth(body, groundtruth_path)

        if args.cleanup_session:
            status, body = _request(
                "DELETE",
                f"{data_base}/sessions/{session_id}",
                timeout=args.timeout,
            )
            _require_ok("delete_session", status, body)
            print("[AgentDemo] session cleaned")
    finally:
        if not args.keep_services:
            _stop_process("graph_data_service", data_proc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="启动 graph_data_service，并模拟 agent 的 train + infer 流程。"
    )
    parser.add_argument("--python", default=sys.executable, help="Python 可执行文件")
    parser.add_argument("--health-timeout", type=float, default=30.0, help="服务健康检查最大等待秒数")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--graph-file",
        default=str(Path(__file__).resolve().parent / "synthetic_gender_community_100.json"),
    )
    parser.add_argument(
        "--groundtruth-file",
        default=str(Path(__file__).resolve().parent / "synthetic_gender_community_100_groundtruth.json"),
    )
    parser.add_argument("--model", default="GCN", help="可选 GCN/GAT/SAGE")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=0.01)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--early-stop-patience",
        type=int,
        default=0,
        help="连续 N 个 epoch 无提升则提前停止（<=0 关闭）",
    )
    parser.add_argument(
        "--early-stop-min-delta",
        type=float,
        default=0.0,
        help="最小提升阈值（默认 0.0）",
    )
    parser.add_argument("--hidden-dim", type=int, default=128)
    parser.add_argument("--embedding-dim", type=int, default=128)
    parser.add_argument("--num-layers", type=int, default=3)
    parser.add_argument("--dropout", type=float, default=0.5)
    parser.add_argument("--device", default="auto", help="cpu/cuda/auto")
    parser.add_argument("--infer-count", type=int, default=10, help="infer 时抽取的未知标签节点数量")
    parser.add_argument("--cleanup-session", action="store_true", help="流程结束后删除 GraphData 会话")
    parser.add_argument("--keep-services", action="store_true", help="流程结束后保留 graph_data_service 进程")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        run(args)
        return 0
    except Exception as e:
        print(f"[AgentDemo] FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

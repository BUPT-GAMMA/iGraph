"""Safe loader and contract validation for HIV/PCBA graph classification data."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Sequence

import torch
from torch_geometric.data import Data
from torch_geometric.data.data import DataEdgeAttr, DataTensorAttr
from torch_geometric.data.storage import GlobalStorage

from ..schemas import GraphClassificationDatasetProfile

if TYPE_CHECKING:
    from graph_data_service.session import GraphSession


ProgressCallback = Callable[[dict[str, Any]], None]

_SAFE_GLOBAL_TYPES = (Data, DataEdgeAttr, DataTensorAttr, GlobalStorage)
_SAFE_GLOBAL_NAMES = frozenset(
    f"{global_type.__module__}.{global_type.__qualname__}"
    for global_type in _SAFE_GLOBAL_TYPES
)
_INTEGER_DTYPES = frozenset(
    dtype
    for dtype in (
        getattr(torch, "uint8", None),
        getattr(torch, "uint16", None),
        getattr(torch, "uint32", None),
        getattr(torch, "uint64", None),
        getattr(torch, "int8", None),
        getattr(torch, "int16", None),
        getattr(torch, "int32", None),
        getattr(torch, "int64", None),
    )
    if dtype is not None
)


@dataclass
class GraphClassificationDatasetBundle:
    dataset_id: str
    name: str
    source_filename: str
    source_path: str
    fingerprint: str
    graphs: Sequence[Data]
    split_indices: Dict[str, List[int]]
    node_texts: List[str]
    edge_texts: List[str]
    label_texts: List[str]
    num_tasks: int
    metric: str
    node_vocab_size: int
    edge_vocab_size: int
    texts: Sequence[Sequence[str]] = field(default_factory=tuple)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> Dict[str, Any]:
        """Return the small, JSON-safe summary used by tools and artifacts."""
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "fingerprint": self.fingerprint,
            "num_graphs": len(self.graphs),
            "num_tasks": self.num_tasks,
            "metric": self.metric,
            "split_sizes": {
                name: len(indices) for name, indices in self.split_indices.items()
            },
            "node_vocab_size": self.node_vocab_size,
            "edge_vocab_size": self.edge_vocab_size,
            "has_missing_labels": bool(self.metadata.get("has_missing_labels", False)),
            "num_edgeless_graphs": int(self.metadata.get("num_edgeless_graphs", 0)),
        }


def load_graph_classification_dataset(
    session: "GraphSession",
    filename: str,
    dataset_profile: GraphClassificationDatasetProfile | str = "auto",
    *,
    progress_callback: ProgressCallback | None = None,
) -> GraphClassificationDatasetBundle:
    """
    Load a registered multi-graph ``.pt`` upload with a strict safe-global policy.

    The caller supplies only the user-visible filename. The actual path is resolved
    exclusively from the current ``GraphSession`` upload registry.
    """
    clean_filename = str(filename or "").strip()
    if not clean_filename:
        raise ValueError("filename 不能为空")
    if Path(clean_filename).suffix.lower() != ".pt":
        raise ValueError("图分类数据集只支持已注册的 .pt 上传文件")

    record = session.resolve_upload_by_name(clean_filename)
    if record is None:
        raise ValueError(f"当前会话中未找到已注册上传文件: {clean_filename}")

    source_filename = str(record.original_filename or clean_filename).strip()
    if Path(source_filename).suffix.lower() != ".pt":
        raise ValueError("图分类数据集只支持已注册的 .pt 上传文件")

    source_path = Path(str(record.saved_path or "")).resolve(strict=False)
    if not source_path.is_file():
        raise ValueError(f"已注册上传文件不存在: {source_filename}")
    if source_path.suffix.lower() != ".pt":
        raise ValueError("已注册上传记录的实际文件不是 .pt")

    profile = str(dataset_profile or "auto").strip().lower()
    if profile not in {"auto", "hiv", "pcba"}:
        raise ValueError("dataset_profile 必须是 auto、hiv 或 pcba")

    _require_safe_torch_capabilities()
    stat_before = source_path.stat()
    _emit_progress(progress_callback, "fingerprint", 0, 1, "正在计算数据集指纹")
    fingerprint = _sha256_file(source_path)
    _emit_progress(progress_callback, "security_scan", 0, 1, "正在检查 checkpoint 全局类型")
    unsafe_globals = _scan_checkpoint_globals(source_path)
    unexpected_globals = sorted(set(unsafe_globals) - _SAFE_GLOBAL_NAMES)
    if unexpected_globals:
        raise ValueError(
            "图分类 .pt 包含未获准的全局类型，已拒绝加载: "
            + ", ".join(unexpected_globals)
        )

    _emit_progress(progress_callback, "deserialize", 0, 1, "正在安全反序列化数据集")
    try:
        with torch.serialization.safe_globals(list(_SAFE_GLOBAL_TYPES)):
            payload = torch.load(
                source_path,
                map_location="cpu",
                weights_only=True,
            )
    except Exception as exc:
        raise ValueError(f"图分类 .pt 安全反序列化失败: {exc}") from exc

    stat_after = source_path.stat()
    if (
        stat_before.st_size != stat_after.st_size
        or stat_before.st_mtime_ns != stat_after.st_mtime_ns
    ):
        raise ValueError("数据集文件在加载过程中发生变化，请重新上传后再试")

    return _validate_and_normalize_payload(
        payload,
        source_filename=source_filename,
        source_path=source_path,
        fingerprint=fingerprint,
        requested_profile=profile,
        unsafe_globals=unsafe_globals,
        source_size_bytes=stat_after.st_size,
        source_mtime_ns=stat_after.st_mtime_ns,
        progress_callback=progress_callback,
    )


def _require_safe_torch_capabilities() -> None:
    scanner = getattr(torch.serialization, "get_unsafe_globals_in_checkpoint", None)
    safe_globals = getattr(torch.serialization, "safe_globals", None)
    try:
        load_parameters = inspect.signature(torch.load).parameters
    except (TypeError, ValueError):
        load_parameters = {}
    missing = []
    if not callable(scanner):
        missing.append("torch.serialization.get_unsafe_globals_in_checkpoint")
    if not callable(safe_globals):
        missing.append("torch.serialization.safe_globals")
    if "weights_only" not in load_parameters:
        missing.append("torch.load(weights_only=...)")
    if missing:
        raise ValueError(
            "当前 PyTorch 版本缺少安全加载能力，请升级 PyTorch；缺少: "
            + ", ".join(missing)
        )


def _scan_checkpoint_globals(path: Path) -> List[str]:
    scanner = torch.serialization.get_unsafe_globals_in_checkpoint
    try:
        return [str(name) for name in scanner(path)]
    except Exception as exc:
        raise ValueError(f"无法静态检查图分类 .pt 的全局类型: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_and_normalize_payload(
    payload: Any,
    *,
    source_filename: str,
    source_path: Path,
    fingerprint: str,
    requested_profile: str,
    unsafe_globals: Sequence[str],
    source_size_bytes: int,
    source_mtime_ns: int,
    progress_callback: ProgressCallback | None,
) -> GraphClassificationDatasetBundle:
    if not isinstance(payload, dict):
        raise ValueError("图分类 .pt 根对象必须是 dict")
    missing_root_fields = [
        field_name
        for field_name in ("graphs", "texts", "split")
        if field_name not in payload
    ]
    if missing_root_fields:
        raise ValueError(
            "图分类 .pt 缺少根字段: " + ", ".join(missing_root_fields)
        )

    graphs = payload["graphs"]
    if not isinstance(graphs, (list, tuple)) or not graphs:
        raise ValueError("graphs 必须是非空的 PyG Data 序列")

    num_tasks: int | None = None
    max_node_category = -1
    max_edge_category = -1
    has_missing_labels = False
    num_edgeless_graphs = 0
    total_graphs = len(graphs)
    _emit_progress(progress_callback, "validate_graphs", 0, total_graphs, "正在校验图样本")

    for graph_index, graph in enumerate(graphs):
        if not isinstance(graph, Data):
            raise ValueError(f"graphs[{graph_index}] 必须是 torch_geometric.data.Data")
        _require_graph_tensor_fields(graph, graph_index)

        x = graph.x
        xe = graph.xe
        edge_index = graph.edge_index
        y = graph.y

        _require_integer_tensor(x, f"graphs[{graph_index}].x")
        _require_integer_tensor(xe, f"graphs[{graph_index}].xe")
        _require_integer_tensor(edge_index, f"graphs[{graph_index}].edge_index")
        if x.ndim != 1:
            raise ValueError(f"graphs[{graph_index}].x 必须是一维节点类别 ID")
        if xe.ndim != 1:
            raise ValueError(f"graphs[{graph_index}].xe 必须是一维边类别 ID")
        if edge_index.ndim != 2 or edge_index.shape[0] != 2:
            raise ValueError(
                f"graphs[{graph_index}].edge_index 必须是 shape [2, E]"
            )
        if x.numel() == 0:
            raise ValueError(f"graphs[{graph_index}] 不允许无节点")
        if int(graph.num_nodes) != int(x.shape[0]):
            raise ValueError(
                f"graphs[{graph_index}].x 长度与 num_nodes 不一致"
            )
        if int(x.min().item()) < 0:
            raise ValueError(f"graphs[{graph_index}].x 包含负类别 ID")
        max_x_id = int(x.max().item())
        if max_x_id > torch.iinfo(torch.int64).max:
            raise ValueError(f"graphs[{graph_index}].x 类别 ID 超出 int64 范围")

        num_edges = int(edge_index.shape[1])
        if int(xe.numel()) != num_edges:
            raise ValueError(
                f"graphs[{graph_index}].xe 长度与 edge_index 边数不一致"
            )
        if num_edges == 0:
            num_edgeless_graphs += 1
        else:
            if int(xe.min().item()) < 0:
                raise ValueError(f"graphs[{graph_index}].xe 包含负类别 ID")
            max_xe_id = int(xe.max().item())
            if max_xe_id > torch.iinfo(torch.int64).max:
                raise ValueError(f"graphs[{graph_index}].xe 类别 ID 超出 int64 范围")
            min_node_id = int(edge_index.min().item())
            max_node_id = int(edge_index.max().item())
            if min_node_id < 0 or max_node_id >= int(x.shape[0]):
                raise ValueError(
                    f"graphs[{graph_index}].edge_index 引用了越界节点"
                )
            max_edge_category = max(max_edge_category, max_xe_id)

        if not isinstance(y, torch.Tensor) or y.ndim == 0 or y.numel() == 0:
            raise ValueError(f"graphs[{graph_index}].y 必须是非空标签 Tensor")
        if y.ndim not in {1, 2} or (y.ndim == 2 and y.shape[0] != 1):
            raise ValueError(
                f"graphs[{graph_index}].y 必须是 [num_tasks] 或 [1, num_tasks]"
            )
        if y.dtype == torch.bool or torch.is_complex(y):
            raise ValueError(f"graphs[{graph_index}].y 必须是数值标签 Tensor")
        y_float = y.detach().cpu().to(dtype=torch.float32).reshape(-1)
        if torch.isinf(y_float).any().item():
            raise ValueError(f"graphs[{graph_index}].y 不能包含无穷值")
        invalid_labels = (
            ~torch.isnan(y_float)
            & (y_float != 0)
            & (y_float != 1)
        )
        if invalid_labels.any().item():
            raise ValueError(
                f"graphs[{graph_index}].y 只能包含 0、1 或 NaN"
            )
        if torch.isnan(y_float).any().item():
            has_missing_labels = True

        graph_num_tasks = int(y_float.numel())
        if num_tasks is None:
            num_tasks = graph_num_tasks
        elif graph_num_tasks != num_tasks:
            raise ValueError(
                f"graphs[{graph_index}].y 任务数不一致: "
                f"期望 {num_tasks}，实际 {graph_num_tasks}"
            )

        graph.x = x.detach().cpu().to(dtype=torch.long).contiguous()
        graph.xe = xe.detach().cpu().to(dtype=torch.long).contiguous()
        graph.edge_index = edge_index.detach().cpu().to(dtype=torch.long).contiguous()
        graph.y = y_float.reshape(1, graph_num_tasks).contiguous()
        max_node_category = max(max_node_category, max_x_id)

        if (graph_index + 1) % 5000 == 0 or graph_index + 1 == total_graphs:
            _emit_progress(
                progress_callback,
                "validate_graphs",
                graph_index + 1,
                total_graphs,
                "正在校验图样本",
            )

    assert num_tasks is not None
    texts = _validate_texts(payload["texts"])
    node_texts = list(texts[0])
    edge_texts = list(texts[1])
    label_texts = list(texts[2])
    if max_node_category >= len(node_texts):
        raise ValueError(
            "texts[0] 未覆盖 x 的最大节点类别 ID: "
            f"max_id={max_node_category}, vocab_size={len(node_texts)}"
        )
    if max_edge_category >= len(edge_texts):
        raise ValueError(
            "texts[1] 未覆盖 xe 的最大边类别 ID: "
            f"max_id={max_edge_category}, vocab_size={len(edge_texts)}"
        )

    split_indices, prompt_split_summary = _validate_split(
        payload["split"],
        total_graphs,
    )
    dataset_name, metric = _resolve_dataset_profile(
        requested_profile=requested_profile,
        source_filename=source_filename,
        num_tasks=num_tasks,
    )

    metadata = {
        "prompt_split_summary": prompt_split_summary,
        "extra_root_fields": sorted(
            str(key) for key in payload.keys() if key not in {"graphs", "texts", "split"}
        ),
        "unsafe_globals": sorted(set(unsafe_globals)),
        "source_size_bytes": int(source_size_bytes),
        "source_mtime_ns": int(source_mtime_ns),
        "has_missing_labels": has_missing_labels,
        "num_edgeless_graphs": num_edgeless_graphs,
        "uses_edge_features": False,
    }
    bundle = GraphClassificationDatasetBundle(
        dataset_id=f"gds_{fingerprint[:8]}",
        name=dataset_name,
        source_filename=source_filename,
        source_path=str(source_path),
        fingerprint=fingerprint,
        graphs=graphs,
        split_indices=split_indices,
        node_texts=node_texts,
        edge_texts=edge_texts,
        label_texts=label_texts,
        num_tasks=num_tasks,
        metric=metric,
        node_vocab_size=len(node_texts),
        edge_vocab_size=len(edge_texts),
        texts=texts,
        metadata=metadata,
    )
    _emit_progress(progress_callback, "complete", 1, 1, "图分类数据集校验完成")
    return bundle


def _require_graph_tensor_fields(graph: Data, graph_index: int) -> None:
    for field_name in ("x", "xe", "edge_index", "y"):
        value = getattr(graph, field_name, None)
        if not isinstance(value, torch.Tensor):
            raise ValueError(
                f"graphs[{graph_index}] 缺少 Tensor 字段: {field_name}"
            )


def _require_integer_tensor(value: torch.Tensor, field_name: str) -> None:
    if value.dtype not in _INTEGER_DTYPES:
        raise ValueError(f"{field_name} 必须是整型 Tensor")


def _validate_texts(raw_texts: Any) -> List[List[str]]:
    if not isinstance(raw_texts, (list, tuple)) or len(raw_texts) < 3:
        raise ValueError("texts 必须至少包含节点、边和标签文本三个列表")
    texts: List[List[str]] = []
    for text_index, values in enumerate(raw_texts):
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"texts[{text_index}] 必须是字符串列表")
        if not all(isinstance(value, str) for value in values):
            raise ValueError(f"texts[{text_index}] 只能包含字符串")
        texts.append(list(values))
    return texts


def _validate_split(raw_split: Any, num_graphs: int) -> tuple[Dict[str, List[int]], Dict[str, Any]]:
    if not isinstance(raw_split, (list, tuple)) or len(raw_split) < 2:
        raise ValueError("split 必须包含 benchmark 划分和 prompt/few-shot 元数据")
    benchmark_split = raw_split[0]
    prompt_split = raw_split[1]
    if not isinstance(benchmark_split, dict):
        raise ValueError("split[0] 必须是包含 train/valid/test 的 dict")
    if not isinstance(prompt_split, dict):
        raise ValueError("split[1] 必须是 prompt/few-shot 元数据 dict")

    if "val" in benchmark_split and "valid" in benchmark_split:
        raise ValueError("split[0] 不能同时包含 val 和 valid")
    val_key = "val" if "val" in benchmark_split else "valid"
    required_keys = ("train", val_key, "test")
    missing_keys = [key for key in required_keys if key not in benchmark_split]
    if missing_keys:
        raise ValueError("split[0] 缺少划分: " + ", ".join(missing_keys))

    normalized: Dict[str, List[int]] = {}
    occupied: set[int] = set()
    for output_key, source_key in (("train", "train"), ("val", val_key), ("test", "test")):
        raw_indices = benchmark_split[source_key]
        if not isinstance(raw_indices, (list, tuple)):
            raise ValueError(f"split[0].{source_key} 必须是整数列表")
        indices: List[int] = []
        local_seen: set[int] = set()
        for position, raw_index in enumerate(raw_indices):
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise ValueError(
                    f"split[0].{source_key}[{position}] 必须是整数"
                )
            index = int(raw_index)
            if index < 0 or index >= num_graphs:
                raise ValueError(
                    f"split[0].{source_key} 包含越界图索引: {index}"
                )
            if index in local_seen:
                raise ValueError(
                    f"split[0].{source_key} 包含重复图索引: {index}"
                )
            if index in occupied:
                raise ValueError(f"train/val/test 划分存在重叠图索引: {index}")
            local_seen.add(index)
            occupied.add(index)
            indices.append(index)
        normalized[output_key] = indices

    if len(occupied) != num_graphs:
        missing_count = num_graphs - len(occupied)
        raise ValueError(
            f"train/val/test 未覆盖全部图，缺少 {missing_count} 张图"
        )
    return normalized, _summarize_mapping(prompt_split)


def _summarize_mapping(mapping: Dict[Any, Any]) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for raw_key, value in mapping.items():
        key = str(raw_key)
        item: Dict[str, Any] = {"type": type(value).__name__}
        if isinstance(value, (dict, list, tuple, set)):
            item["size"] = len(value)
        if isinstance(value, dict):
            item["keys"] = sorted(str(child_key) for child_key in value.keys())
        values[key] = item
    return {"keys": sorted(values), "values": values}


def _resolve_dataset_profile(
    *,
    requested_profile: str,
    source_filename: str,
    num_tasks: int,
) -> tuple[str, str]:
    filename = Path(source_filename).name.lower()
    filename_profile: str | None = None
    if "pcba" in filename:
        filename_profile = "pcba"
    elif "hiv" in filename:
        filename_profile = "hiv"

    inferred_profile: str | None = None
    if num_tasks == 1:
        inferred_profile = "hiv"
    elif num_tasks == 128:
        inferred_profile = "pcba"

    if requested_profile == "auto":
        profile = filename_profile or inferred_profile
        if profile is None:
            raise ValueError(
                f"无法自动识别图分类数据集：filename={source_filename}, num_tasks={num_tasks}"
            )
    else:
        profile = requested_profile
        if filename_profile is not None and filename_profile != profile:
            raise ValueError(
                f"文件名识别为 {filename_profile}，与 dataset_profile={profile} 冲突"
            )

    expected_tasks = 1 if profile == "hiv" else 128
    if num_tasks != expected_tasks:
        raise ValueError(
            f"{profile} 数据集要求 num_tasks={expected_tasks}，实际为 {num_tasks}"
        )
    return profile, "roc_auc" if profile == "hiv" else "ap"


def _emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    current: int,
    total: int,
    message: str,
) -> None:
    if callback is None:
        return
    callback(
        {
            "stage": stage,
            "current": int(current),
            "total": int(total),
            "message": message,
        }
    )

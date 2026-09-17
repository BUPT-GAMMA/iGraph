"""
GraphData: 通用图数据容器（只读）
支持同质图与异质图，可转换至 NetworkX。
"""

import numpy as np
from typing import Optional, Dict, List, Any, Tuple, Union, Sequence

from graph_data_service.adapters import graph_to_networkx
from graph_data_service.property_graph import property_weights
from graph_data_service.graph_structure import (
    GraphStructureInfo,
    compute_node_degrees,
    inspect_graph_structure,
    logical_edge_pairs,
    normalize_storage_type,
    resolve_semantic_type,
    structure_metadata_dict,
)
from graph_data_service.io import load_graph_data, save_graph_data


class GraphData:
    """
    通用图数据容器（只读，不支持增删改）。
    支持同质图与异质图。

    内部存储约定：
        [基础]
        edge_index  : np.ndarray, shape [2, E], dtype int64
                      edge_index[0] = 源节点, edge_index[1] = 目标节点
        edge_weight : np.ndarray, shape [E], dtype float32（构图属性图为 float64）, 边权重 (可选)
        node_feat   : np.ndarray, shape [N, *], dtype float32, 节点特征 (可选)
        edge_feat   : np.ndarray, shape [E, *], dtype float32, 边特征 (可选)
        graph_desc  : str, 图的文本描述 (可选)

        [图学习]
        node_label  : np.ndarray, shape [N] (int64/object) 或 [N, C] (float32, one-hot)
        node_type   : np.ndarray, shape [N], dtype object (str 类型名)
        edge_label  : np.ndarray, shape [E] (int64/object) 或 [E, C] (float32, one-hot)
        edge_type   : np.ndarray, shape [E], dtype object (str 类型名)
        graph_label : str | int | np.ndarray (one-hot 向量)

    节点名称双向映射：
        _id2node : Dict[int, str]   node_id  -> node_name
        _node2id : Dict[str, int]   node_name -> node_id

    边快速查找表：
        _edge_lookup : Dict[Tuple[int, int], Dict[str, Any]]
                       key   = (src_id, dst_id)
                       value = {"edge_idx": int, "weight": float | None,
                                "feat": list | None, "name": str | None,
                                "label": Any | None, "type": str | None}
        _edge_name_lookup : Dict[str, Tuple[int, int]]
                            key = edge_name, value = (src_id, dst_id)

    类型快速查找表：
        _node_type_lookup : Dict[str, List[int]]   type_name -> [node_id, ...]
        _edge_type_lookup : Dict[str, List[int]]   type_name -> [edge_idx, ...]

    无向图约定：
        to_undirected() 返回新对象，并将 (u, v) 和 (v, u) 同时写入 edge_index，
        因此 neighbors() / get_incident_edges() 等方法均只需遍历 src == node_id。
    """

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    def __init__(
        self,
        num_nodes: int = 0,
        edge_index: Optional[np.ndarray] = None,
        *,
        id2node: Optional[Dict[int, str]] = None,
        node2id: Optional[Dict[str, int]] = None,
        edge_names: Optional[List[str]] = None,
        edge_weight: Optional[np.ndarray] = None,
        node_feat: Optional[np.ndarray] = None,
        edge_feat: Optional[np.ndarray] = None,
        graph_desc: Optional[str] = None,
        directed: bool = True,
        semantic_type: Optional[str] = None,
        storage_type: Optional[str] = None,
        graph_metadata: Optional[Dict[str, Any]] = None,
        transform_log: Optional[List[str]] = None,
        name: str = "",
        # ── 图学习 ──────────────────────────────────────────────────
        node_label: Optional[Union[List, np.ndarray]] = None,
        node_type: Optional[Union[List[str], np.ndarray]] = None,
        edge_label: Optional[Union[List, np.ndarray]] = None,
        edge_type: Optional[Union[List[str], np.ndarray]] = None,
        graph_label: Optional[Union[str, int, List, np.ndarray]] = None,
        # ── 文本图 (TAG) ─────────────────────────────────────────────
        node_text: Optional[List[str]] = None,
        edge_text: Optional[List[str]] = None,
        label_names: Optional[List[str]] = None,
        train_mask: Optional[np.ndarray] = None,
        val_mask: Optional[np.ndarray] = None,
        test_mask: Optional[np.ndarray] = None,
    ):
        self._num_nodes: int = int(num_nodes)
        self._graph_metadata: Dict[str, Any] = dict(graph_metadata or {})
        self._transform_log: List[str] = list(transform_log or [])
        self.name: str = name
        resolved_semantic, semantic_source = resolve_semantic_type(
            explicit_semantic_type=semantic_type,
            directed=directed,
            metadata=self._graph_metadata,
            dataset_name=name,
        )
        self._semantic_type: str = resolved_semantic
        self._semantic_source: str = semantic_source
        if resolved_semantic == "directed":
            self._directed = True
        elif resolved_semantic == "undirected":
            self._directed = False
        else:
            self._directed = bool(directed)
        self._storage_type: Optional[str] = normalize_storage_type(storage_type)
        self._structure_info: Optional[GraphStructureInfo] = None
        self._degree_cache: Optional[dict[str, Any]] = None

        # --------------------------------------------------------------
        # 边索引
        # --------------------------------------------------------------
        if edge_index is None:
            self._edge_index = np.empty((2, 0), dtype=np.int64)
        else:
            self._edge_index = np.asarray(edge_index, dtype=np.int64)
            if not (self._edge_index.ndim == 2 and self._edge_index.shape[0] == 2):
                raise ValueError("edge_index 必须是 shape [2, E] 的数组")

        # --------------------------------------------------------------
        # 节点双向 Map
        # --------------------------------------------------------------
        if (id2node is None) != (node2id is None):
            raise ValueError("id2node 与 node2id 必须同时提供或同时为 None")
        if id2node is not None:
            self._validate_node_maps(id2node, node2id)
            self._id2node: Dict[int, str] = dict(id2node)
            self._node2id: Dict[str, int] = dict(node2id)
        else:
            self._id2node: Dict[int, str] = {}
            self._node2id: Dict[str, int] = {}

        # --------------------------------------------------------------
        # 边名称
        # --------------------------------------------------------------
        self._edge_names: Optional[np.ndarray] = (
            np.asarray(edge_names, dtype=str) if edge_names is not None else None
        )

        # --------------------------------------------------------------
        # 特征 / 权重 / 描述
        # --------------------------------------------------------------
        self._node_feat: Optional[np.ndarray] = (
            np.asarray(node_feat, dtype=np.float32) if node_feat is not None else None
        )
        self._edge_feat: Optional[np.ndarray] = (
            np.asarray(edge_feat, dtype=np.float32) if edge_feat is not None else None
        )
        # Hydrate older property snapshots too; weights must agree with their
        # retained JSON properties and retain JSON double precision.
        constructed_weights = property_weights(self._graph_metadata, self._edge_names)
        if constructed_weights is not None:
            edge_weight = constructed_weights
        self._edge_weight: Optional[np.ndarray] = (
            np.asarray(edge_weight, dtype=np.float64 if constructed_weights is not None else np.float32).ravel()
            if edge_weight is not None
            else None
        )
        self._graph_desc: Optional[str] = graph_desc

        # --------------------------------------------------------------
        # 图学习字段
        # --------------------------------------------------------------
        self._node_label: Optional[np.ndarray] = (
            self._as_label_array(node_label) if node_label is not None else None
        )
        self._node_type: Optional[np.ndarray] = (
            np.asarray(node_type, dtype=object) if node_type is not None else None
        )
        self._edge_label: Optional[np.ndarray] = (
            self._as_label_array(edge_label) if edge_label is not None else None
        )
        self._edge_type: Optional[np.ndarray] = (
            np.asarray(edge_type, dtype=object) if edge_type is not None else None
        )
        self._structure_info = inspect_graph_structure(
            self._edge_index,
            num_nodes=self._num_nodes,
            semantic_type=self._semantic_type,
            storage_type=self._storage_type,
            edge_type=self._relationship_keys(),
            semantic_source=self._semantic_source,
        )
        if self._semantic_type == "unknown":
            resolved_semantic, semantic_source = resolve_semantic_type(
                explicit_semantic_type=semantic_type,
                directed=directed,
                metadata=self._graph_metadata,
                dataset_name=name,
                structure_is_symmetric=self._structure_info.is_symmetric,
            )
            if resolved_semantic != "unknown":
                self._semantic_type = resolved_semantic
                self._semantic_source = semantic_source
                self._directed = resolved_semantic == "directed"
                self._structure_info = inspect_graph_structure(
                    self._edge_index,
                    num_nodes=self._num_nodes,
                    semantic_type=self._semantic_type,
                    storage_type=self._storage_type,
                    edge_type=self._relationship_keys(),
                    semantic_source=self._semantic_source,
                )
        if self._storage_type is None:
            self._storage_type = self._structure_info.storage_type
        self._degree_cache = None
        # graph_label: list/ndarray → float32 one-hot；str/int → 原样存储
        if graph_label is None:
            self._graph_label: Optional[Union[str, int, np.ndarray]] = None
        elif isinstance(graph_label, (list, np.ndarray)):
            self._graph_label = np.asarray(graph_label, dtype=np.float32)
        else:
            self._graph_label = graph_label  # str or int

        # --------------------------------------------------------------
        # 文本图 (TAG) 字段
        # --------------------------------------------------------------
        self._node_text: Optional[List[str]] = list(node_text) if node_text is not None else None
        self._edge_text: Optional[List[str]] = list(edge_text) if edge_text is not None else None
        self._label_names: Optional[List[str]] = list(label_names) if label_names is not None else None
        self._train_mask: Optional[np.ndarray] = (
            np.asarray(train_mask, dtype=bool) if train_mask is not None else None
        )
        self._val_mask: Optional[np.ndarray] = (
            np.asarray(val_mask, dtype=bool) if val_mask is not None else None
        )
        self._test_mask: Optional[np.ndarray] = (
            np.asarray(test_mask, dtype=bool) if test_mask is not None else None
        )

        # --------------------------------------------------------------
        # 查找表（初始化最后统一构建）
        # --------------------------------------------------------------
        self._edge_lookup: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._edge_name_lookup: Dict[str, Tuple[int, int]] = {}
        self._node_type_lookup: Dict[str, List[int]] = {}
        self._edge_type_lookup: Dict[str, List[int]] = {}
        self._build_edge_lookup()
        self._build_type_lookups()

        self._validate()

    # ------------------------------------------------------------------
    # 内部：校验
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_node_maps(
        id2node: Dict[int, str],
        node2id: Dict[str, int],
    ) -> None:
        """校验 id2node 与 node2id 互为双射。"""
        if len(id2node) != len(node2id):
            raise ValueError(
                f"id2node 长度({len(id2node)}) 与 node2id 长度({len(node2id)}) 不一致"
            )
        for nid, nname in id2node.items():
            if node2id.get(nname) != nid:
                raise ValueError(
                    f"映射不一致：id2node[{nid}]='{nname}', "
                    f"但 node2id['{nname}']={node2id.get(nname)}"
                )

    def _validate(self) -> None:
        """全量校验各字段的一致性。"""
        E = self.num_edges
        N = self._num_nodes

        if self._edge_index.size > 0:
            min_id = int(self._edge_index.min())
            max_id = int(self._edge_index.max())
            if min_id < 0:
                raise ValueError("edge_index contains negative node ids")
            if max_id >= N:
                raise ValueError(
                    f"edge_index 包含节点 ID {max_id}，超出节点总数 {N}"
                )
        if self._id2node and N > 0:
            if max(self._id2node.keys()) >= N:
                raise ValueError("id2node 中存在超出节点范围的 id")

        for field, arr, size, label in [
            ("edge_names",  self._edge_names,  E, "边数"),
            ("node_feat",   self._node_feat,   N, "节点数"),
            ("edge_feat",   self._edge_feat,   E, "边数"),
            ("edge_weight", self._edge_weight, E, "边数"),
            ("node_label",  self._node_label,  N, "节点数"),
            ("node_type",   self._node_type,   N, "节点数"),
            ("edge_label",  self._edge_label,  E, "边数"),
            ("edge_type",   self._edge_type,   E, "边数"),
            ("train_mask",  self._train_mask,  N, "节点数"),
            ("val_mask",    self._val_mask,    N, "节点数"),
            ("test_mask",   self._test_mask,   N, "节点数"),
        ]:
            if arr is not None and arr.shape[0] != size:
                raise ValueError(
                    f"{field} 第 0 维({arr.shape[0]}) 与{label}({size}) 不匹配"
                )

        if self._node_text is not None and len(self._node_text) != N:
            raise ValueError(
                f"node_text 长度({len(self._node_text)}) 与节点数({N}) 不匹配"
            )
        if self._edge_text is not None and len(self._edge_text) != E:
            raise ValueError(
                f"edge_text 长度({len(self._edge_text)}) 与边数({E}) 不匹配"
            )

    # ------------------------------------------------------------------
    # 内部：标签数组规范化
    # ------------------------------------------------------------------

    @staticmethod
    def _as_label_array(data: Union[List, np.ndarray]) -> np.ndarray:
        """
        将用户传入的 label 数据规范化为 numpy 数组：
            - 字符串列表  → object dtype, shape [N]
            - 整数列表    → int64 dtype, shape [N]
            - 二维数组    → float32 dtype, shape [N, C]（one-hot / soft label）
            - 其他浮点    → float32, shape [N]
        """
        arr = np.asarray(data)
        if arr.ndim == 2:
            return arr.astype(np.float32)
        if arr.dtype.kind in ("U", "S", "O"):
            return arr.astype(object)
        if arr.dtype.kind in ("i", "u"):
            return arr.astype(np.int64)
        return arr.astype(np.float32)

    # ------------------------------------------------------------------
    # 内部：构建查找表
    # ------------------------------------------------------------------

    def _build_edge_lookup(self) -> None:
        """
        根据 edge_index / edge_weight / edge_feat / edge_names / edge_label / edge_type
        全量构建 O(1) 查找表：

            _edge_lookup      : (src, dst) -> 属性字典
            _edge_name_lookup : edge_name  -> (src, dst)
            _edge_entries_by_name : edge_name -> 属性字典

        无向图对 (src, dst) 和 (dst, src) 分别存储独立副本。
        按端点查询保留最后一条边；按边名称查询保留各条关系。
        """
        self._edge_lookup = {}
        self._edge_name_lookup = {}
        self._edge_entries_by_name = {}

        if self._edge_index.size == 0:
            return

        src_arr, dst_arr = self._edge_index

        for idx in range(self.num_edges):
            src = int(src_arr[idx])
            dst = int(dst_arr[idx])
            ename = (
                str(self._edge_names[idx]) if self._edge_names is not None else None
            )
            elabel = self._label_entry(self._edge_label, idx)
            etype = (
                str(self._edge_type[idx]) if self._edge_type is not None else None
            )

            entry: Dict[str, Any] = {
                "edge_idx": idx,
                "weight": (
                    float(self._edge_weight[idx])
                    if self._edge_weight is not None
                    else None
                ),
                "feat": (
                    self._edge_feat[idx].tolist()
                    if self._edge_feat is not None
                    else None
                ),
                "name":  ename,
                "label": elabel,
                "type":  etype,
            }

            self._edge_lookup[(src, dst)] = entry
            if not self._directed and src != dst:
                self._edge_lookup[(dst, src)] = dict(entry)

            if ename is not None:
                self._edge_name_lookup[ename] = (src, dst)
                self._edge_entries_by_name[ename] = entry

    def _build_type_lookups(self) -> None:
        """构建节点类型和边类型的快速查找表。"""
        self._node_type_lookup = {}
        if self._node_type is not None:
            for i, t in enumerate(self._node_type):
                self._node_type_lookup.setdefault(str(t), []).append(i)

        self._edge_type_lookup = {}
        if self._edge_type is not None:
            for i, t in enumerate(self._edge_type):
                self._edge_type_lookup.setdefault(str(t), []).append(i)

    @staticmethod
    def _label_entry(label_arr: Optional[np.ndarray], idx: int) -> Any:
        """从 label 数组取出第 idx 条记录，转为 Python 原生类型。"""
        if label_arr is None:
            return None
        val = label_arr[idx]
        if label_arr.ndim == 2:
            return val.tolist()
        if label_arr.dtype.kind in ("i", "u"):
            return int(val)
        if label_arr.dtype == object:
            return str(val)
        return float(val)

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------

    @property
    def num_nodes(self) -> int:
        return self._num_nodes

    @property
    def num_edges(self) -> int:
        return self._edge_index.shape[1]

    @property
    def num_edge_entries(self) -> int:
        return self.num_edges

    @property
    def directed(self) -> bool:
        return self._directed

    @property
    def semantic_type(self) -> str:
        return self._semantic_type

    @property
    def storage_type(self) -> str:
        return self._storage_type or "mixed"

    @property
    def is_symmetric(self) -> bool:
        return bool(self._structure_info.is_symmetric) if self._structure_info is not None else False

    @property
    def has_self_loops(self) -> bool:
        return bool(self._structure_info.has_self_loops) if self._structure_info is not None else False

    @property
    def has_duplicate_entries(self) -> bool:
        return bool(self._structure_info.has_duplicate_entries) if self._structure_info is not None else False

    @property
    def num_unique_directed_edges(self) -> int:
        return int(self._structure_info.num_unique_directed_edges) if self._structure_info is not None else self.num_edges

    @property
    def num_unique_undirected_edges(self) -> int:
        return int(self._structure_info.num_unique_undirected_edges) if self._structure_info is not None else self.num_edges

    @property
    def num_unique_edges(self) -> int:
        if self._structure_info is None:
            return self.num_edges
        return int(self._structure_info.num_unique_edges)

    @property
    def structure_info(self) -> Dict[str, Any]:
        if self._structure_info is None:
            return {}
        return structure_metadata_dict(self._structure_info)

    @property
    def graph_metadata(self) -> Dict[str, Any]:
        payload = dict(self._graph_metadata)
        payload.update(self.structure_info)
        return payload

    @property
    def transform_log(self) -> List[str]:
        return list(self._transform_log)

    @property
    def edge_index(self) -> np.ndarray:
        return self._edge_index

    @property
    def raw_edge_index(self) -> np.ndarray:
        return self._edge_index

    @property
    def edge_weight(self) -> Optional[np.ndarray]:
        return self._edge_weight

    @property
    def node_feat(self) -> Optional[np.ndarray]:
        return self._node_feat

    @property
    def edge_feat(self) -> Optional[np.ndarray]:
        return self._edge_feat

    @property
    def graph_desc(self) -> Optional[str]:
        return self._graph_desc

    @property
    def id2node(self) -> Dict[int, str]:
        return dict(self._id2node)

    @property
    def node2id(self) -> Dict[str, int]:
        return dict(self._node2id)

    @property
    def edge_names(self) -> Optional[np.ndarray]:
        return self._edge_names

    @property
    def node_names(self) -> Optional[List[Optional[str]]]:
        if not self._id2node:
            return None
        return [self._id2node.get(i) for i in range(self._num_nodes)]

    @property
    def node_feat_dim(self) -> Optional[int]:
        if self._node_feat is None or self._node_feat.ndim < 2:
            return None
        return self._node_feat.shape[1]

    @property
    def edge_feat_dim(self) -> Optional[int]:
        if self._edge_feat is None or self._edge_feat.ndim < 2:
            return None
        return self._edge_feat.shape[1]

    # ── 异质图属性 ─────────────────────────────────────────────────

    @property
    def node_label(self) -> Optional[np.ndarray]:
        return self._node_label

    @property
    def node_type(self) -> Optional[np.ndarray]:
        return self._node_type

    @property
    def edge_label(self) -> Optional[np.ndarray]:
        return self._edge_label

    @property
    def edge_type(self) -> Optional[np.ndarray]:
        return self._edge_type

    @property
    def graph_label(self) -> Optional[Union[str, int, np.ndarray]]:
        return self._graph_label

    @property
    def node_types(self) -> List[str]:
        """所有节点类型名称列表（去重有序）。"""
        return list(self._node_type_lookup.keys())

    @property
    def edge_types(self) -> List[str]:
        """所有边类型名称列表（去重有序）。"""
        return list(self._edge_type_lookup.keys())

    # 快捷布尔属性
    @property
    def has_node_names(self) -> bool:
        return bool(self._id2node)

    @property
    def has_edge_names(self) -> bool:
        return self._edge_names is not None

    @property
    def has_node_feat(self) -> bool:
        return self._node_feat is not None

    @property
    def has_edge_feat(self) -> bool:
        return self._edge_feat is not None

    @property
    def has_edge_weight(self) -> bool:
        return self._edge_weight is not None

    @property
    def has_node_label(self) -> bool:
        return self._node_label is not None

    @property
    def has_node_type(self) -> bool:
        return self._node_type is not None

    @property
    def has_edge_label(self) -> bool:
        return self._edge_label is not None

    @property
    def has_edge_type(self) -> bool:
        return self._edge_type is not None

    @property
    def has_graph_label(self) -> bool:
        return self._graph_label is not None

    # ── 文本图 (TAG) 属性 ──────────────────────────────────────────

    @property
    def node_text(self) -> Optional[List[str]]:
        return self._node_text

    @property
    def edge_text(self) -> Optional[List[str]]:
        return self._edge_text

    @property
    def label_names(self) -> Optional[List[str]]:
        return self._label_names

    @property
    def train_mask(self) -> Optional[np.ndarray]:
        return self._train_mask

    @property
    def val_mask(self) -> Optional[np.ndarray]:
        return self._val_mask

    @property
    def test_mask(self) -> Optional[np.ndarray]:
        return self._test_mask

    @property
    def has_node_text(self) -> bool:
        return self._node_text is not None

    @property
    def has_edge_text(self) -> bool:
        return self._edge_text is not None

    @property
    def has_label_names(self) -> bool:
        return self._label_names is not None

    @property
    def has_train_mask(self) -> bool:
        return self._train_mask is not None

    @property
    def has_split_masks(self) -> bool:
        return (
            self._train_mask is not None
            or self._val_mask is not None
            or self._test_mask is not None
        )

    @property
    def is_heterogeneous(self) -> bool:
        """若存在节点类型或边类型则视为异质图。"""
        return self._node_type is not None or self._edge_type is not None

    # ------------------------------------------------------------------
    # 节点名称查询
    # ------------------------------------------------------------------

    def id_to_name(self, node_id: int) -> str:
        if node_id not in self._id2node:
            raise KeyError(f"节点 id={node_id} 没有对应名称")
        return self._id2node[node_id]

    def name_to_id(self, node_name: str) -> int:
        if node_name not in self._node2id:
            raise KeyError(f"节点名称 '{node_name}' 不存在")
        return self._node2id[node_name]

    def has_node_name(self, node_name: str) -> bool:
        return node_name in self._node2id

    def has_node_id(self, node_id: int) -> bool:
        return node_id in self._id2node

    def ensure_node_names(self) -> None:
        """若无节点名称，用整数 ID 的字符串形式填充。仅在数据加载入口调用。"""
        if self._id2node:
            return
        self._id2node = {i: str(i) for i in range(self._num_nodes)}
        self._node2id = {str(i): i for i in range(self._num_nodes)}

    # ------------------------------------------------------------------
    # 节点特征查询
    # ------------------------------------------------------------------

    def get_node_feat_by_id(self, node_id: int) -> np.ndarray:
        if self._node_feat is None:
            raise RuntimeError("当前图不含节点特征")
        if not (0 <= node_id < self._num_nodes):
            raise IndexError(f"节点 id={node_id} 超出范围 [0, {self._num_nodes})")
        return self._node_feat[node_id]

    def get_node_feat_by_name(self, node_name: str) -> np.ndarray:
        return self.get_node_feat_by_id(self.name_to_id(node_name))

    def get_node_feats_by_ids(self, node_ids: List[int]) -> np.ndarray:
        if self._node_feat is None:
            raise RuntimeError("当前图不含节点特征")
        ids = np.asarray(node_ids, dtype=np.int64)
        if ids.size > 0 and (ids.min() < 0 or ids.max() >= self._num_nodes):
            raise IndexError("node_ids 中存在越界的节点 id")
        return self._node_feat[ids]

    def get_node_feats_by_names(self, node_names: List[str]) -> np.ndarray:
        return self.get_node_feats_by_ids([self.name_to_id(n) for n in node_names])

    # ------------------------------------------------------------------
    # 节点文本查询（TAG）
    # ------------------------------------------------------------------

    def get_node_text_by_id(self, node_id: int) -> str:
        if self._node_text is None:
            raise RuntimeError("当前图不含节点文本")
        if not (0 <= node_id < self._num_nodes):
            raise IndexError(f"节点 id={node_id} 超出范围 [0, {self._num_nodes})")
        return self._node_text[node_id]

    def get_node_text_by_name(self, node_name: str) -> str:
        return self.get_node_text_by_id(self.name_to_id(node_name))

    def get_edge_text(self, src: int, dst: int) -> Optional[str]:
        if self._edge_text is None:
            return None
        entry = self._edge_lookup.get((src, dst))
        if entry is None:
            return None
        return self._edge_text[entry["edge_idx"]]

    # ------------------------------------------------------------------
    # 节点标签查询
    # ------------------------------------------------------------------

    def get_node_label_by_id(self, node_id: int) -> Any:
        """返回节点 node_id 的标签（str / int / list）。"""
        if self._node_label is None:
            raise RuntimeError("当前图不含节点标签")
        if not (0 <= node_id < self._num_nodes):
            raise IndexError(f"节点 id={node_id} 超出范围")
        return self._label_entry(self._node_label, node_id)

    def get_node_label_by_name(self, node_name: str) -> Any:
        """通过节点名称返回节点标签。"""
        return self.get_node_label_by_id(self.name_to_id(node_name))

    # ------------------------------------------------------------------
    # 节点类型查询
    # ------------------------------------------------------------------

    def get_node_type_by_id(self, node_id: int) -> Optional[str]:
        """返回节点 node_id 的类型字符串，无类型时返回 None。"""
        if self._node_type is None:
            return None
        if not (0 <= node_id < self._num_nodes):
            raise IndexError(f"节点 id={node_id} 超出范围")
        return str(self._node_type[node_id])

    def get_node_type_by_name(self, node_name: str) -> Optional[str]:
        """通过节点名称返回节点类型。"""
        return self.get_node_type_by_id(self.name_to_id(node_name))

    def get_nodes_by_type(self, type_name: str) -> List[int]:
        """返回指定类型的所有节点 ID 列表。"""
        return list(self._node_type_lookup.get(type_name, []))

    def get_nodes_by_type_as_names(self, type_name: str) -> List[str]:
        """返回指定类型的所有节点名称列表（无名称节点以 str(id) 表示）。"""
        return [
            self._id2node.get(i, str(i))
            for i in self.get_nodes_by_type(type_name)
        ]

    # ------------------------------------------------------------------
    # 边查询
    # ------------------------------------------------------------------

    def get_edge(self, src: int, dst: int) -> Optional[Dict[str, Any]]:
        """O(1) 查询边 (src, dst) 的属性字典，不存在时返回 None。"""
        return self._edge_lookup.get((src, dst))

    def get_edge_by_node_names(
        self, src_name: str, dst_name: str
    ) -> Optional[Dict[str, Any]]:
        return self.get_edge(self.name_to_id(src_name), self.name_to_id(dst_name))

    def get_edge_by_edge_name(self, edge_name: str) -> Optional[Dict[str, Any]]:
        return self._edge_entries_by_name.get(edge_name)

    def has_edge(self, src: int, dst: int) -> bool:
        return (src, dst) in self._edge_lookup

    def has_edge_by_node_names(self, src_name: str, dst_name: str) -> bool:
        try:
            return self.has_edge(self.name_to_id(src_name), self.name_to_id(dst_name))
        except KeyError:
            return False

    def has_edge_by_edge_name(self, edge_name: str) -> bool:
        return edge_name in self._edge_name_lookup

    # ── 边权重 ──────────────────────────────────────────────────────

    def get_edge_weight(self, src: int, dst: int) -> Optional[float]:
        entry = self._edge_lookup.get((src, dst))
        return entry["weight"] if entry is not None else None

    def get_edge_weight_by_node_names(
        self, src_name: str, dst_name: str
    ) -> Optional[float]:
        return self.get_edge_weight(
            self.name_to_id(src_name), self.name_to_id(dst_name)
        )

    def get_edge_weight_by_edge_name(self, edge_name: str) -> Optional[float]:
        entry = self.get_edge_by_edge_name(edge_name)
        return entry["weight"] if entry is not None else None

    # ── 边特征 ──────────────────────────────────────────────────────

    def get_edge_feat(self, src: int, dst: int) -> Optional[np.ndarray]:
        if self._edge_feat is None:
            return None
        entry = self._edge_lookup.get((src, dst))
        return self._edge_feat[entry["edge_idx"]] if entry is not None else None

    def get_edge_feat_by_node_names(
        self, src_name: str, dst_name: str
    ) -> Optional[np.ndarray]:
        return self.get_edge_feat(
            self.name_to_id(src_name), self.name_to_id(dst_name)
        )

    def get_edge_feat_by_edge_name(self, edge_name: str) -> Optional[np.ndarray]:
        entry = self.get_edge_by_edge_name(edge_name)
        if entry is None or self._edge_feat is None:
            return None
        return self._edge_feat[entry["edge_idx"]]

    # ── 边名称 ──────────────────────────────────────────────────────

    def get_edge_name(self, src: int, dst: int) -> Optional[str]:
        entry = self._edge_lookup.get((src, dst))
        return entry["name"] if entry is not None else None

    def get_edge_name_by_node_names(
        self, src_name: str, dst_name: str
    ) -> Optional[str]:
        return self.get_edge_name(
            self.name_to_id(src_name), self.name_to_id(dst_name)
        )

    def get_edge_endpoints(self, edge_name: str) -> Optional[Tuple[int, int]]:
        return self._edge_name_lookup.get(edge_name)

    def get_edge_endpoints_by_name(
        self, edge_name: str
    ) -> Optional[Tuple[str, str]]:
        pair = self._edge_name_lookup.get(edge_name)
        if pair is None:
            return None
        return (
            self._id2node.get(pair[0], str(pair[0])),
            self._id2node.get(pair[1], str(pair[1])),
        )

    # ── 边标签 ──────────────────────────────────────────────────────

    def get_edge_label(self, src: int, dst: int) -> Any:
        """返回边 (src, dst) 的标签，不存在或无标签时返回 None。"""
        entry = self._edge_lookup.get((src, dst))
        return entry["label"] if entry is not None else None

    def get_edge_label_by_node_names(self, src_name: str, dst_name: str) -> Any:
        return self.get_edge_label(
            self.name_to_id(src_name), self.name_to_id(dst_name)
        )

    def get_edge_label_by_edge_name(self, edge_name: str) -> Any:
        entry = self.get_edge_by_edge_name(edge_name)
        return entry["label"] if entry is not None else None

    # ── 边类型 ──────────────────────────────────────────────────────

    def get_edge_type(self, src: int, dst: int) -> Optional[str]:
        """返回边 (src, dst) 的类型字符串，不存在或无类型时返回 None。"""
        entry = self._edge_lookup.get((src, dst))
        return entry["type"] if entry is not None else None

    def get_edge_type_by_node_names(
        self, src_name: str, dst_name: str
    ) -> Optional[str]:
        return self.get_edge_type(
            self.name_to_id(src_name), self.name_to_id(dst_name)
        )

    def get_edge_type_by_edge_name(self, edge_name: str) -> Optional[str]:
        entry = self.get_edge_by_edge_name(edge_name)
        return entry["type"] if entry is not None else None

    def get_edges_by_type(self, type_name: str) -> List[Tuple[int, int]]:
        """返回指定类型的所有边 (src_id, dst_id) 列表。"""
        src_arr, dst_arr = self._edge_index
        return [
            (int(src_arr[i]), int(dst_arr[i]))
            for i in self._edge_type_lookup.get(type_name, [])
        ]

    def get_edges_by_type_as_names(
        self, type_name: str
    ) -> List[Tuple[str, str]]:
        """返回指定类型的所有边端点名称列表。"""
        return [
            (self._id2node.get(s, str(s)), self._id2node.get(d, str(d)))
            for s, d in self.get_edges_by_type(type_name)
        ]

    # ── 枚举所有边 ──────────────────────────────────────────────────

    def all_edges(self) -> List[Tuple[int, int]]:
        src, dst = self._edge_index
        return [(int(src[i]), int(dst[i])) for i in range(self.num_edges)]

    def _relationship_keys(self):
        """Constructed graphs identify relationships by ID, ordinary graphs by type."""
        if self._graph_metadata.get("construction_graph") is not None and self._edge_names is not None:
            return self._edge_names
        return self._edge_type

    def logical_edges(self) -> List[Tuple[int, int]]:
        return logical_edge_pairs(
            self._edge_index,
            self._semantic_type,
            edge_type=self._relationship_keys(),
        )

    def all_edges_by_name(self) -> List[Tuple[str, str]]:
        return [
            (
                self._id2node.get(s, str(s)),
                self._id2node.get(d, str(d)),
            )
            for s, d in self.all_edges()
        ]

    def logical_edges_by_name(self) -> List[Tuple[str, str]]:
        return [
            (
                self._id2node.get(s, str(s)),
                self._id2node.get(d, str(d)),
            )
            for s, d in self.logical_edges()
        ]

    # ------------------------------------------------------------------
    # 图拓扑查询
    # ------------------------------------------------------------------

    def _degree_data(self) -> dict[str, Any]:
        if self._degree_cache is None:
            self._degree_cache = compute_node_degrees(
                self._edge_index,
                self._num_nodes,
                self._semantic_type,
                edge_type=self._relationship_keys(),
            )
        return self._degree_cache

    def neighbors(self, node_id: int) -> List[int]:
        src, dst = self._edge_index
        neighbors: List[int] = []
        seen: set[int] = set()
        if self._semantic_type == "undirected":
            for s, d in zip(src.tolist(), dst.tolist()):
                s_i = int(s)
                d_i = int(d)
                if s_i == node_id and d_i not in seen:
                    seen.add(d_i)
                    neighbors.append(d_i)
                elif d_i == node_id and s_i not in seen:
                    seen.add(s_i)
                    neighbors.append(s_i)
            return neighbors
        mask = src == node_id
        for dst_id in dst[mask].tolist():
            d_i = int(dst_id)
            if d_i not in seen:
                seen.add(d_i)
                neighbors.append(d_i)
        return neighbors

    def neighbors_by_name(self, node_name: str) -> List[str]:
        return [
            self._id2node.get(i, str(i))
            for i in self.neighbors(self.name_to_id(node_name))
        ]

    def predecessors(self, node_id: int) -> List[int]:
        if self._semantic_type == "undirected":
            return self.neighbors(node_id)
        src, dst = self._edge_index
        preds: List[int] = []
        seen: set[int] = set()
        for src_id in src[dst == node_id].tolist():
            s_i = int(src_id)
            if s_i not in seen:
                seen.add(s_i)
                preds.append(s_i)
        return preds

    def predecessors_by_name(self, node_name: str) -> List[str]:
        return [
            self._id2node.get(i, str(i))
            for i in self.predecessors(self.name_to_id(node_name))
        ]

    def out_degree(self, node_id: int) -> int:
        degrees = self._degree_data()
        return int(degrees["out_degree"][node_id])

    def in_degree(self, node_id: int) -> int:
        degrees = self._degree_data()
        return int(degrees["in_degree"][node_id])

    def degree(self, node_id: int) -> int:
        degrees = self._degree_data()
        return int(degrees["degree"][node_id])

    def total_degree(self, node_id: int) -> int:
        degrees = self._degree_data()
        return int(degrees["total_degree"][node_id])

    def num_neighbors(self, node_id: int) -> int:
        degrees = self._degree_data()
        return int(degrees["num_neighbors"][node_id])

    def out_degree_by_name(self, node_name: str) -> int:
        return self.out_degree(self.name_to_id(node_name))

    def in_degree_by_name(self, node_name: str) -> int:
        return self.in_degree(self.name_to_id(node_name))

    def degree_by_name(self, node_name: str) -> int:
        return self.degree(self.name_to_id(node_name))

    def total_degree_by_name(self, node_name: str) -> int:
        return self.total_degree(self.name_to_id(node_name))

    def num_neighbors_by_name(self, node_name: str) -> int:
        return self.num_neighbors(self.name_to_id(node_name))

    def get_incident_edges(self, node_id: int) -> List[Dict[str, Any]]:
        src_arr, dst_arr = self._edge_index
        results = []
        seen = set()
        relationship_keys = self._relationship_keys()
        constructed = self._graph_metadata.get("construction_graph") is not None and self._edge_names is not None
        mask = (src_arr == node_id) | (dst_arr == node_id)
        for i in np.where(mask)[0]:
            source, target = int(src_arr[i]), int(dst_arr[i])
            if self._semantic_type == "undirected":
                relation = relationship_keys[i] if relationship_keys is not None else None
                key = tuple(sorted((source, target))) + (relation,)
                if key in seen:
                    continue
                seen.add(key)
            entry = (self.get_edge_by_edge_name(str(self._edge_names[i])) if constructed
                     else self._edge_lookup.get((source, target)))
            if entry is not None:
                results.append({"src": source, "dst": target, **entry})
        return results

    def get_incident_edges_by_name(self, node_name: str) -> List[Dict[str, Any]]:
        raw = self.get_incident_edges(self.name_to_id(node_name))
        for r in raw:
            r["src_name"] = self._id2node.get(r["src"], str(r["src"]))
            r["dst_name"] = self._id2node.get(r["dst"], str(r["dst"]))
        return raw

    # ------------------------------------------------------------------
    # 子图 / 图变换（返回新对象，不修改原图）
    # ------------------------------------------------------------------

    def subgraph(self, node_ids: List[int]) -> "GraphData":
        """
        按节点 id 列表提取子图，节点 id 重新从 0 开始映射。
        所有逐节点/逐边字段（特征、标签、类型等）随之裁剪。
        """
        node_ids_arr = np.asarray(node_ids, dtype=np.int64)
        old_to_new: Dict[int, int] = {
            int(old): new for new, old in enumerate(node_ids_arr)
        }

        src, dst = self._edge_index
        mask = np.isin(src, node_ids_arr) & np.isin(dst, node_ids_arr)

        new_src = np.array([old_to_new[int(s)] for s in src[mask]], dtype=np.int64)
        new_dst = np.array([old_to_new[int(d)] for d in dst[mask]], dtype=np.int64)
        new_edge_index = (
            np.stack([new_src, new_dst], axis=0)
            if new_src.size > 0
            else np.empty((2, 0), dtype=np.int64)
        )

        new_id2node: Dict[int, str] = {}
        new_node2id: Dict[str, int] = {}
        for old_id, new_id in old_to_new.items():
            if old_id in self._id2node:
                nname = self._id2node[old_id]
                new_id2node[new_id] = nname
                new_node2id[nname] = new_id

        def _node_slice(arr: Optional[np.ndarray]) -> Optional[np.ndarray]:
            return arr[node_ids_arr] if arr is not None else None

        def _edge_slice(arr: Optional[np.ndarray]) -> Optional[np.ndarray]:
            return arr[mask] if arr is not None else None

        edge_indices = np.where(mask)[0]

        return GraphData(
            num_nodes=len(node_ids_arr),
            edge_index=new_edge_index,
            id2node=new_id2node or None,
            node2id=new_node2id or None,
            edge_names=(
                self._edge_names[mask].tolist()
                if self._edge_names is not None
                else None
            ),
            edge_weight=_edge_slice(self._edge_weight),
            node_feat=_node_slice(self._node_feat),
            edge_feat=_edge_slice(self._edge_feat),
            graph_desc=self._graph_desc,
            directed=self._directed,
            name=self.name + "_sub",
            node_label=_node_slice(self._node_label),
            node_type=(
                self._node_type[node_ids_arr].tolist()
                if self._node_type is not None
                else None
            ),
            edge_label=_edge_slice(self._edge_label),
            edge_type=(
                self._edge_type[mask].tolist()
                if self._edge_type is not None
                else None
            ),
            graph_label=self._graph_label,
            semantic_type=self._semantic_type,
            storage_type=self._storage_type,
            graph_metadata=dict(self._graph_metadata),
            transform_log=list(self._transform_log),
            node_text=(
                [self._node_text[i] for i in node_ids_arr]
                if self._node_text is not None
                else None
            ),
            edge_text=(
                [self._edge_text[i] for i in edge_indices]
                if self._edge_text is not None
                else None
            ),
            label_names=self._label_names,
            train_mask=_node_slice(self._train_mask),
            val_mask=_node_slice(self._val_mask),
            test_mask=_node_slice(self._test_mask),
        )

    def subgraph_by_names(self, node_names: List[str]) -> "GraphData":
        return self.subgraph([self.name_to_id(n) for n in node_names])

    def to_undirected(self) -> "GraphData":
        """Return a processed copy with bidirectional undirected storage.

        The raw graph is left untouched. Exact duplicate directed records are
        removed, while reverse records are preserved or added for undirected
        message passing. Edge attributes are copied with the edge record they
        belong to so edge_index and edge-level fields stay aligned.
        """
        def _freeze(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, np.generic):
                return value.item()
            if isinstance(value, np.ndarray):
                return tuple(np.asarray(value, dtype=object).reshape(-1).tolist())
            if isinstance(value, (list, tuple)):
                return tuple(_freeze(item) for item in value)
            return value

        constructed = self._graph_metadata.get("construction_graph") is not None and self._edge_names is not None

        def _storage_key(record, *, reverse=False) -> tuple[Any, ...]:
            src, dst = int(record["src"]), int(record["dst"])
            if reverse:
                src, dst = dst, src
            identity = record["name"] if constructed else _freeze(record["type"])
            return src, dst, identity

        def _exact_signature(record: dict[str, Any]) -> tuple[Any, ...]:
            if constructed:
                return _storage_key(record)
            return (
                int(record["src"]),
                int(record["dst"]),
                _freeze(record["type"]),
                _freeze(record["label"]),
                _freeze(record["weight"]),
                _freeze(record["feat"]),
                _freeze(record["text"]),
            )

        def _record_at(idx: int) -> dict[str, Any]:
            return {
                "src": int(self._edge_index[0, idx]),
                "dst": int(self._edge_index[1, idx]),
                "weight": float(self._edge_weight[idx]) if self._edge_weight is not None else None,
                "feat": self._edge_feat[idx].tolist() if self._edge_feat is not None else None,
                "name": str(self._edge_names[idx]) if self._edge_names is not None else None,
                "label": self._label_entry(self._edge_label, idx),
                "type": self._edge_type[idx] if self._edge_type is not None else None,
                "text": self._edge_text[idx] if self._edge_text is not None else None,
            }

        edge_records: list[dict[str, Any]] = []
        seen_exact: set[tuple[Any, ...]] = set()
        removed_duplicate_entries = 0
        for idx in range(self.num_edges):
            record = _record_at(idx)
            signature = _exact_signature(record)
            if signature in seen_exact:
                removed_duplicate_entries += 1
                continue
            seen_exact.add(signature)
            edge_records.append(record)

        present_storage_keys = {
            _storage_key(record)
            for record in edge_records
        }
        added_reverse_edges = 0
        for record in list(edge_records):
            if record["src"] == record["dst"]:
                continue
            reverse_key = _storage_key(record, reverse=True)
            if reverse_key in present_storage_keys:
                continue
            reverse = dict(record)
            reverse["src"], reverse["dst"] = record["dst"], record["src"]
            present_storage_keys.add(reverse_key)
            edge_records.append(reverse)
            added_reverse_edges += 1

        new_edge_index = (
            np.asarray(
                [[r["src"] for r in edge_records], [r["dst"] for r in edge_records]],
                dtype=np.int64,
            )
            if edge_records
            else np.empty((2, 0), dtype=np.int64)
        )
        new_edge_names = [r["name"] for r in edge_records] if self._edge_names is not None else None
        new_edge_weight = [r["weight"] for r in edge_records] if self._edge_weight is not None else None
        new_edge_feat = [r["feat"] for r in edge_records] if self._edge_feat is not None else None
        new_edge_label = [r["label"] for r in edge_records] if self._edge_label is not None else None
        new_edge_type = [r["type"] for r in edge_records] if self._edge_type is not None else None
        new_edge_text = [r["text"] for r in edge_records] if self._edge_text is not None else None

        transform_log = list(self._transform_log)
        if removed_duplicate_entries:
            transform_log.append("Removed duplicate directed edge entries")
        if added_reverse_edges:
            transform_log.append("Added reverse edges for undirected message passing")
        else:
            transform_log.append("Validated bidirectional storage for undirected message passing")

        return GraphData(
            num_nodes=self._num_nodes,
            edge_index=new_edge_index,
            id2node=dict(self._id2node) if self._id2node else None,
            node2id=dict(self._node2id) if self._node2id else None,
            edge_names=new_edge_names,
            edge_weight=new_edge_weight,
            node_feat=self._node_feat.copy() if self._node_feat is not None else None,
            edge_feat=new_edge_feat,
            graph_desc=self._graph_desc,
            directed=False,
            semantic_type="undirected",
            storage_type="bidirectional",
            graph_metadata=dict(self._graph_metadata),
            transform_log=transform_log,
            name=self.name + "_undirected",
            node_label=self._node_label.copy() if self._node_label is not None else None,
            node_type=self._node_type.tolist() if self._node_type is not None else None,
            edge_label=new_edge_label,
            edge_type=new_edge_type,
            graph_label=(
                self._graph_label.copy()
                if isinstance(self._graph_label, np.ndarray)
                else self._graph_label
            ),
            node_text=list(self._node_text) if self._node_text is not None else None,
            edge_text=new_edge_text,
            label_names=self._label_names,
            train_mask=self._train_mask.copy() if self._train_mask is not None else None,
            val_mask=self._val_mask.copy() if self._val_mask is not None else None,
            test_mask=self._test_mask.copy() if self._test_mask is not None else None,
        )

    def to_networkx(self, feat_as_attr: bool = True, use_node_names: bool = True):
        return graph_to_networkx(
            self,
            feat_as_attr=feat_as_attr,
            use_node_names=use_node_names,
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        save_graph_data(self, path)

    @classmethod
    def load(cls, path: str) -> "GraphData":
        return load_graph_data(path, cls)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def summary(self) -> str:
        nf = str(self._node_feat.shape) if self._node_feat is not None else "none"
        ef = str(self._edge_feat.shape) if self._edge_feat is not None else "none"
        nl = str(self._node_label.shape) if self._node_label is not None else "none"
        el = str(self._edge_label.shape) if self._edge_label is not None else "none"
        ntypes = ", ".join(self.node_types) if self._node_type is not None else "none"
        etypes = ", ".join(self.edge_types) if self._edge_type is not None else "none"
        gl = (
            self._graph_label.tolist()
            if isinstance(self._graph_label, np.ndarray)
            else self._graph_label
        )
        mask_info = (
            f"train={int(self._train_mask.sum())}"
            if self._train_mask is not None
            else "none"
        )
        lines = [
            f"GraphData(name='{self.name}')",
            f"  num_nodes              : {self.num_nodes}",
            f"  num_edge_entries       : {self.num_edge_entries}",
            f"  num_unique_edges       : {self.num_unique_edges}",
            f"  num_unique_directed    : {self.num_unique_directed_edges}",
            f"  num_unique_undirected  : {self.num_unique_undirected_edges}",
            f"  semantic_type          : {self.semantic_type}",
            f"  storage_type           : {self.storage_type}",
            f"  is_symmetric           : {self.is_symmetric}",
            f"  has_self_loops         : {self.has_self_loops}",
            f"  has_duplicate_entries  : {self.has_duplicate_entries}",
            f"  named_nodes            : {len(self._id2node)} / {self.num_nodes}",
            f"  edge_names             : {'yes' if self._edge_names is not None else 'no'}",
            f"  node_feat              : {nf}",
            f"  edge_feat              : {ef}",
            f"  edge_weight            : {'yes' if self._edge_weight is not None else 'no'}",
            f"  graph_desc              : {self._graph_desc or 'none'}",
            f"  node_label              : {nl}",
            f"  node_type               : {ntypes}",
            f"  edge_label              : {el}",
            f"  edge_type               : {etypes}",
            f"  graph_label             : {gl if gl is not None else 'none'}",
            f"  node_text               : {'yes(' + str(len(self._node_text)) + ')' if self._node_text is not None else 'no'}",
            f"  edge_text               : {'yes(' + str(len(self._edge_text)) + ')' if self._edge_text is not None else 'no'}",
            f"  label_names             : {', '.join(self._label_names) if self._label_names else 'none'}",
            f"  split_masks             : {mask_info}",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"GraphData(name='{self.name}', nodes={self.num_nodes}, "
            f"edge_entries={self.num_edge_entries}, unique_edges={self.num_unique_edges}, "
            f"semantic_type='{self.semantic_type}', storage_type='{self.storage_type}', "
            f"symmetric={self.is_symmetric}, named_nodes={len(self._id2node)}, "
            f"node_feat={'yes' if self._node_feat is not None else 'no'}, "
            f"edge_feat={'yes' if self._edge_feat is not None else 'no'}, "
            f"edge_weight={'yes' if self._edge_weight is not None else 'no'}, "
            f"node_label={'yes' if self._node_label is not None else 'no'}, "
            f"node_type={'yes' if self._node_type is not None else 'no'}, "
            f"edge_label={'yes' if self._edge_label is not None else 'no'}, "
            f"edge_type={'yes' if self._edge_type is not None else 'no'}, "
            f"graph_label={'yes' if self._graph_label is not None else 'no'}, "
            f"node_text={'yes' if self._node_text is not None else 'no'}, "
            f"label_names={'yes' if self._label_names is not None else 'no'}, "
            f"split_masks={'yes' if self.has_split_masks else 'no'})"
        )

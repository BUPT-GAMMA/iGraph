from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

from graph_data_service.graph_data import GraphData
from graph_data_service.graph_ml.datasets import load_graph_classification_dataset
from graph_data_service.session import GraphSession
from tests.fixtures.graph_classification import make_graph_classification_payload


class UnsafePayloadMarker:
    pass


class GraphClassificationDatasetLoaderTest(unittest.TestCase):
    def _save_and_load(
        self,
        payload,
        *,
        filename: str = "hiv.pt",
        profile: str = "auto",
        progress_callback=None,
    ):
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                path = Path(td) / filename
                torch.save(payload, path)
                session = GraphSession("s-graph-classification-loader", user_id="alice")
                session.register_upload("tok-dataset", filename, str(path))
                return load_graph_classification_dataset(
                    session,
                    filename,
                    profile,
                    progress_callback=progress_callback,
                )
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root

    def test_loads_hiv_contract_with_safe_torch_options(self) -> None:
        payload = make_graph_classification_payload("hiv")
        progress = []
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                path = Path(td) / "hiv.pt"
                torch.save(payload, path)
                expected_fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
                session = GraphSession("s-safe-hiv", user_id="alice")
                session.set_graph(GraphData(num_nodes=1))
                graph_version_before = session.graph_version
                session.register_upload("tok-hiv", "hiv.pt", str(path))

                real_torch_load = torch.load
                load_calls = []

                def recording_load(
                    file_obj,
                    map_location=None,
                    *,
                    weights_only=None,
                    **kwargs,
                ):
                    load_calls.append(
                        {
                            "map_location": map_location,
                            "weights_only": weights_only,
                        }
                    )
                    return real_torch_load(
                        file_obj,
                        map_location=map_location,
                        weights_only=weights_only,
                        **kwargs,
                    )

                with patch.object(torch, "load", recording_load):
                    bundle = load_graph_classification_dataset(
                        session,
                        "hiv.pt",
                        progress_callback=progress.append,
                    )

                self.assertEqual(
                    load_calls,
                    [{"map_location": "cpu", "weights_only": True}],
                )
                self.assertEqual(session.graph_version, graph_version_before)
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root

        self.assertEqual(bundle.name, "hiv")
        self.assertEqual(bundle.metric, "roc_auc")
        self.assertEqual(bundle.num_tasks, 1)
        self.assertEqual(bundle.fingerprint, expected_fingerprint)
        self.assertEqual(bundle.metadata["missing_label_rate"], 0.0)
        self.assertEqual(bundle.dataset_id, f"gds_{expected_fingerprint[:8]}")
        self.assertEqual(bundle.split_indices, {"train": [0, 1, 2, 3], "val": [4], "test": [5]})
        self.assertEqual(bundle.node_vocab_size, 4)
        self.assertEqual(bundle.edge_vocab_size, 3)
        self.assertEqual(bundle.graphs[0].y.shape, (1, 1))
        self.assertEqual(bundle.graphs[0].x.dtype, torch.long)
        self.assertEqual(bundle.graphs[0].xe.dtype, torch.long)
        self.assertEqual(bundle.graphs[0].edge_index.dtype, torch.long)
        self.assertEqual(bundle.graphs[0].y.dtype, torch.float32)
        self.assertEqual(bundle.metadata["num_edgeless_graphs"], 1)
        self.assertFalse(bundle.metadata["uses_edge_features"])
        self.assertEqual(
            bundle.metadata["prompt_split_summary"]["keys"],
            ["e2e_graph", "lr_graph"],
        )
        self.assertEqual(progress[-1]["stage"], "complete")

    def test_loads_pcba_contract_and_preserves_nan_labels(self) -> None:
        bundle = self._save_and_load(
            make_graph_classification_payload("pcba"),
            filename="pcba.pt",
        )

        self.assertEqual(bundle.name, "pcba")
        self.assertEqual(bundle.metric, "ap")
        self.assertEqual(bundle.num_tasks, 128)
        self.assertEqual(bundle.graphs[0].y.shape, (1, 128))
        self.assertTrue(torch.isnan(bundle.graphs[0].y).any().item())
        self.assertTrue(bundle.metadata["has_missing_labels"])
        self.assertEqual(bundle.metadata["num_edgeless_graphs"], 1)

    def test_rejects_unapproved_checkpoint_global_before_deserialization(self) -> None:
        payload = make_graph_classification_payload("hiv")
        payload["unsafe"] = UnsafePayloadMarker()

        load_calls = []
        real_torch_load = torch.load

        def recording_load(
            file_obj,
            map_location=None,
            *,
            weights_only=None,
            **kwargs,
        ):
            load_calls.append(file_obj)
            return real_torch_load(
                file_obj,
                map_location=map_location,
                weights_only=weights_only,
                **kwargs,
            )

        with patch.object(torch, "load", recording_load):
            with self.assertRaisesRegex(ValueError, "未获准的全局类型"):
                self._save_and_load(payload)
        self.assertEqual(load_calls, [])

    def test_requires_registered_pt_upload(self) -> None:
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                session = GraphSession("s-missing-upload", user_id="alice")
                with self.assertRaisesRegex(ValueError, "未找到已注册上传文件"):
                    load_graph_classification_dataset(session, "hiv.pt")
                with self.assertRaisesRegex(ValueError, "只支持已注册的 .pt"):
                    load_graph_classification_dataset(session, "hiv.json")
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root

    def test_rejects_wrong_root_and_missing_root_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "根对象必须是 dict"):
            self._save_and_load([1, 2, 3])

        payload = make_graph_classification_payload("hiv")
        payload.pop("texts")
        with self.assertRaisesRegex(ValueError, "缺少根字段: texts"):
            self._save_and_load(payload)

    def test_rejects_invalid_graph_tensor_contracts(self) -> None:
        cases = []

        payload = make_graph_classification_payload("hiv")
        del payload["graphs"][0].xe
        cases.append((payload, "缺少 Tensor 字段: xe"))

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][0].x = payload["graphs"][0].x.float()
        cases.append((payload, "x 必须是整型 Tensor"))

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][1].edge_index = torch.tensor([[0, 1, 2]], dtype=torch.long)
        cases.append((payload, "edge_index 必须是 shape"))

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][1].edge_index = torch.tensor([[0], [99]], dtype=torch.long)
        payload["graphs"][1].xe = torch.tensor([0], dtype=torch.long)
        cases.append((payload, "引用了越界节点"))

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][1].xe = torch.empty((0,), dtype=torch.long)
        cases.append((payload, "xe 长度与 edge_index 边数不一致"))

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][0].x = torch.empty((0,), dtype=torch.long)
        cases.append((payload, "不允许无节点"))

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][0].x[0] = -1
        cases.append((payload, "x 包含负类别 ID"))

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self._save_and_load(payload)

    def test_rejects_invalid_or_inconsistent_graph_labels(self) -> None:
        payload = make_graph_classification_payload("hiv")
        payload["graphs"][0].y = torch.tensor([2.0])
        with self.assertRaisesRegex(ValueError, "只能包含 0、1 或 NaN"):
            self._save_and_load(payload)

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][1].y = torch.tensor([0.0, 1.0])
        with self.assertRaisesRegex(ValueError, "任务数不一致"):
            self._save_and_load(payload)

        payload = make_graph_classification_payload("pcba")
        payload["graphs"][0].y = torch.zeros((2, 64), dtype=torch.float32)
        with self.assertRaisesRegex(ValueError, r"必须是 \[num_tasks\]"):
            self._save_and_load(payload, filename="pcba.pt")

        payload = make_graph_classification_payload("hiv")
        payload["graphs"][0].y = torch.tensor([torch.inf])
        with self.assertRaisesRegex(ValueError, "不能包含无穷值"):
            self._save_and_load(payload)

    def test_rejects_invalid_splits(self) -> None:
        cases = []

        payload = make_graph_classification_payload("hiv")
        payload["split"][0]["valid"] = [3]
        cases.append((payload, "存在重叠"))

        payload = make_graph_classification_payload("hiv")
        payload["split"][0]["train"].append(0)
        cases.append((payload, "包含重复"))

        payload = make_graph_classification_payload("hiv")
        payload["split"][0]["test"] = [99]
        cases.append((payload, "越界图索引"))

        payload = make_graph_classification_payload("hiv")
        payload["split"][0]["train"].pop()
        cases.append((payload, "未覆盖全部图"))

        payload = make_graph_classification_payload("hiv")
        payload["split"][0]["train"][0] = True
        cases.append((payload, "必须是整数"))

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self._save_and_load(payload)

    def test_rejects_vocab_and_profile_mismatches(self) -> None:
        payload = make_graph_classification_payload("hiv")
        payload["texts"][0] = payload["texts"][0][:2]
        with self.assertRaisesRegex(ValueError, r"texts\[0\] 未覆盖"):
            self._save_and_load(payload)

        with self.assertRaisesRegex(ValueError, "要求 num_tasks=128"):
            self._save_and_load(
                make_graph_classification_payload("hiv"),
                filename="pcba.pt",
            )

        with self.assertRaisesRegex(ValueError, "与 dataset_profile=pcba 冲突"):
            self._save_and_load(
                make_graph_classification_payload("hiv"),
                filename="hiv.pt",
                profile="pcba",
            )

    def test_reports_missing_safe_torch_capability_without_fallback(self) -> None:
        payload = make_graph_classification_payload("hiv")
        with patch.object(
            torch.serialization,
            "get_unsafe_globals_in_checkpoint",
            None,
        ):
            with self.assertRaisesRegex(ValueError, "缺少安全加载能力"):
                self._save_and_load(payload)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import torch

from graph_data_service.graph_data import GraphData
from graph_data_service.graph_ml.datasets import (
    GraphClassificationDatasetBundle,
    GraphMLDatasetStore,
    load_graph_classification_dataset,
)
from graph_data_service.graph_ml.module import GraphMLModule
from graph_data_service.session import GraphSession
from tests.fixtures.graph_classification import make_graph_classification_payload


class GraphMLDatasetStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_data_root = os.environ.get("IGRAPH_DATA_ROOT")
        self._temp_dir = tempfile.TemporaryDirectory()
        os.environ["IGRAPH_DATA_ROOT"] = self._temp_dir.name

    def tearDown(self) -> None:
        self._temp_dir.cleanup()
        if self._old_data_root is None:
            os.environ.pop("IGRAPH_DATA_ROOT", None)
        else:
            os.environ["IGRAPH_DATA_ROOT"] = self._old_data_root

    def _session(self, session_id: str, user_id: str = "alice") -> GraphSession:
        return GraphSession(session_id, user_id=user_id)

    def _register_bytes(
        self,
        session: GraphSession,
        filename: str,
        content: bytes | None = None,
    ) -> Path:
        user_part = session.user_id or "anonymous"
        path = Path(self._temp_dir.name) / f"{user_part}-{session.session_id}-{filename}"
        path.write_bytes(content if content is not None else filename.encode("utf-8"))
        session.register_upload(f"token-{filename}", filename, str(path))
        return path

    @staticmethod
    def _fake_bundle(
        path: Path,
        filename: str,
        dataset_id: str,
        profile: str = "hiv",
    ) -> GraphClassificationDatasetBundle:
        stat = path.stat()
        return GraphClassificationDatasetBundle(
            dataset_id=dataset_id,
            name=profile,
            source_filename=filename,
            source_path=str(path.resolve()),
            fingerprint=f"fingerprint-{dataset_id}",
            graphs=(),
            split_indices={"train": [], "val": [], "test": []},
            node_texts=[],
            edge_texts=[],
            label_texts=[],
            num_tasks=1 if profile == "hiv" else 128,
            metric="roc_auc" if profile == "hiv" else "ap",
            node_vocab_size=0,
            edge_vocab_size=0,
            metadata={
                "source_size_bytes": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
            },
        )

    def test_load_reuses_bundle_and_keeps_single_graph_state_independent(self) -> None:
        session = self._session("reuse")
        session.set_graph(GraphData(num_nodes=1))
        session.bind_dataset("single-dataset", "v1")
        graph_version_before = session.graph_version

        path = Path(self._temp_dir.name) / "hiv.pt"
        torch.save(make_graph_classification_payload("hiv"), path)
        session.register_upload("token-hiv", "hiv.pt", str(path))

        load_count = 0

        def counting_loader(*args, **kwargs):
            nonlocal load_count
            load_count += 1
            return load_graph_classification_dataset(*args, **kwargs)

        store = GraphMLDatasetStore(loader=counting_loader, max_resident_datasets=1)
        first = store.load("alice:reuse", session, "hiv.pt")
        second = store.load("alice:reuse", session, "hiv.pt")

        self.assertIs(first, second)
        self.assertEqual(load_count, 1)
        self.assertEqual(store.resident_count, 1)
        self.assertEqual(session.graph_version, graph_version_before)
        self.assertEqual(
            session.load_dataset_binding(),
            {"dataset_id": "single-dataset", "dataset_version": "v1"},
        )
        self.assertEqual(
            session.load_graph_ml_dataset_binding(),
            {
                "dataset_id": first.dataset_id,
                "dataset_profile": "hiv",
                "fingerprint": first.fingerprint,
                "source_filename": "hiv.pt",
            },
        )

        session.clear_graph()
        self.assertIs(store.resolve_for_session("alice:reuse"), first)
        self.assertEqual(
            session.load_graph_ml_dataset_binding()["dataset_id"],
            first.dataset_id,
        )

    def test_concurrent_same_file_uses_single_flight(self) -> None:
        session = self._session("concurrent")
        path = self._register_bytes(session, "hiv.pt")
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def blocking_loader(_session, filename, _profile, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=3))
            return self._fake_bundle(path, filename, "gds_concurrent")

        store = GraphMLDatasetStore(loader=blocking_loader, max_resident_datasets=1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                store.load, "alice:concurrent", session, "hiv.pt"
            )
            self.assertTrue(started.wait(timeout=3))
            second_future = executor.submit(
                store.load, "alice:concurrent", session, "hiv.pt"
            )
            release.set()
            first = first_future.result(timeout=3)
            second = second_future.result(timeout=3)

        self.assertEqual(calls, 1)
        self.assertIs(first, second)

    def test_failed_replacement_preserves_old_bundle_and_binding(self) -> None:
        session = self._session("replace-failure")
        first_path = self._register_bytes(session, "first.pt")
        self._register_bytes(session, "bad.pt")

        def loader(_session, filename, _profile, **_kwargs):
            if filename == "bad.pt":
                raise ValueError("invalid replacement")
            return self._fake_bundle(first_path, filename, "gds_first")

        store = GraphMLDatasetStore(loader=loader, max_resident_datasets=1)
        first = store.load("alice:replace-failure", session, "first.pt")

        with self.assertRaisesRegex(ValueError, "invalid replacement"):
            store.load("alice:replace-failure", session, "bad.pt")

        self.assertIs(store.resolve_for_session("alice:replace-failure"), first)
        self.assertEqual(
            session.load_graph_ml_dataset_binding()["dataset_id"],
            "gds_first",
        )

    def test_successful_replacement_drops_old_bundle(self) -> None:
        session = self._session("replace-success")
        paths = {
            name: self._register_bytes(session, name)
            for name in ("first.pt", "second.pt")
        }

        def loader(_session, filename, _profile, **_kwargs):
            stem = Path(filename).stem
            return self._fake_bundle(paths[filename], filename, f"gds_{stem}")

        store = GraphMLDatasetStore(loader=loader, max_resident_datasets=1)
        first = store.load("alice:replace-success", session, "first.pt")
        second = store.load("alice:replace-success", session, "second.pt")

        self.assertIsNot(first, second)
        self.assertIs(store.resolve_for_session("alice:replace-success"), second)
        with self.assertRaisesRegex(ValueError, "未绑定到当前会话"):
            store.resolve_for_session("alice:replace-success", first.dataset_id)
        self.assertEqual(store.resident_count, 1)

    def test_capacity_release_restart_and_cross_session_isolation(self) -> None:
        alice = self._session("shared-id", user_id="alice")
        bob = self._session("shared-id", user_id="bob")
        alice_path = self._register_bytes(alice, "hiv.pt", b"alice")
        bob_path = self._register_bytes(bob, "hiv.pt", b"bob")

        def loader(session, filename, _profile, **_kwargs):
            path = alice_path if session is alice else bob_path
            owner = "alice" if session is alice else "bob"
            return self._fake_bundle(path, filename, f"gds_{owner}")

        store = GraphMLDatasetStore(loader=loader, max_resident_datasets=1)
        alice_bundle = store.load("alice:shared-id", alice, "hiv.pt")

        with self.assertRaisesRegex(ValueError, "会话身份不匹配"):
            store.load("bob:shared-id", alice, "hiv.pt")
        with self.assertRaisesRegex(ValueError, "请重新调用"):
            store.resolve_for_session("bob:shared-id", alice_bundle.dataset_id)
        with self.assertRaisesRegex(ValueError, "达到上限"):
            store.load("bob:shared-id", bob, "hiv.pt")

        restarted_store = GraphMLDatasetStore(loader=loader, max_resident_datasets=1)
        with self.assertRaisesRegex(ValueError, "请重新调用"):
            restarted_store.resolve_for_session(
                "alice:shared-id", alice_bundle.dataset_id
            )
        self.assertEqual(
            alice.load_graph_ml_dataset_binding()["dataset_id"],
            alice_bundle.dataset_id,
        )

        self.assertTrue(store.release_session("alice:shared-id"))
        self.assertEqual(store.resident_count, 0)
        self.assertTrue(alice_path.exists())
        self.assertEqual(
            alice.load_graph_ml_dataset_binding()["dataset_id"],
            alice_bundle.dataset_id,
        )
        bob_bundle = store.load("bob:shared-id", bob, "hiv.pt")
        self.assertEqual(bob_bundle.dataset_id, "gds_bob")

    def test_module_shares_store_with_train_and_infer_runtime(self) -> None:
        session = self._session("module")
        path = self._register_bytes(session, "hiv.pt")

        def loader(_session, filename, _profile, **_kwargs):
            return self._fake_bundle(path, filename, "gds_module")

        store = GraphMLDatasetStore(loader=loader, max_resident_datasets=1)
        module = GraphMLModule(
            get_session=lambda _session_id: session,
            artifact_root=str(Path(self._temp_dir.name) / "artifacts"),
            dataset_store=store,
        )

        result = module.load_graph_classification_dataset(
            "alice:module",
            {"filename": "hiv.pt"},
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["dataset"]["dataset_id"], "gds_module")
        with self.assertRaisesRegex(ValueError, "训练器尚未接入"):
            module.train(
                "alice:module",
                {
                    "task": "graph_classification",
                    "model": "GCN",
                    "data": {"dataset_id": "gds_module"},
                },
            )

        module._ensure_runtime()
        self.assertIs(module._trainer._dataset_store, store)
        self.assertIs(module._inferencer._dataset_store, store)


if __name__ == "__main__":
    unittest.main()

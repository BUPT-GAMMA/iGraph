import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException
from backend.playground import ScenarioStore, Turn, approves_current_plan
from backend.projects import ProjectDirectory
from backend.project_datasets import ProjectDatasetDirectory


class PlaygroundStoreTest(unittest.TestCase):
    def test_free_form_build_and_deferral_intent(self):
        for message in ("这两道题就定了，请按这个方案现在把示例图建出来吧。", "可以了，按这个版本建图",
                        "没问题，直接生成示例图吧", "不用再调整问题了，请开始构图", "Looks good, go ahead and build it."):
            self.assertTrue(approves_current_plan(message), message)
        for message in ("先不要构图，再调整问题", "如果我说开始构图会发生什么？", "请先删除最后一个问题再生成图",
                        "Don't build the graph yet", "After I approve, build the graph"):
            self.assertFalse(approves_current_plan(message), message)

    def test_revision_ownership_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScenarioStore(Path(tmp) / "scenarios.sqlite")
            tid = str(uuid.uuid4())
            def turn(message, revision=0):
                return Turn(turn_id=uuid.uuid4(), revision=revision, message=message, connection_revision=1)
            with self.assertRaises(HTTPException):
                store.begin("alice", tid, turn("开始构图"))
            first = turn("展示校园社团之间的合作")
            doc, started = store.begin("alice", tid, first)
            self.assertTrue(started)
            self.assertFalse(store.begin("alice", tid, first)[1])
            with self.assertRaises(HTTPException):
                store.begin("alice", tid, turn("另一个问题"))
            with self.assertRaises(HTTPException):
                store.read("bob", tid)
            self.assertEqual(store.list("bob"), [])
            store.update("alice", doc, status="draft", confirmable=True, plan={"title": "校园社团"})
            negative, _ = store.begin("alice", tid, turn("先不要构图，调整问题", 1))
            self.assertFalse(negative["confirmed"])
            store.update("alice", negative, status="draft")
            confirmed, _ = store.begin("alice", tid, turn("确认好问题了，开始构图", 2))
            self.assertTrue(confirmed["confirmed"])
            self.assertEqual(store.read("alice", tid)["status"], "building")

    def test_unpublished_project_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            projects = ProjectDirectory(path / "projects.json")
            directory = ProjectDatasetDirectory(path / "datasets.json", path / "sources")
            pid, did = str(uuid.uuid4()), str(uuid.uuid4())
            projects.stage_scenario("alice", pid, "校园", "社团关系")
            self.assertEqual(projects.list("alice"), [])
            for _ in range(2):
                row = directory.create(project_id=pid, user_id="alice", dataset_id=did, name="示例",
                                       original_filename="playground-scenario.txt", content=b"source")
                self.assertEqual(row.dataset_id, did)
            self.assertEqual(len(directory.list(pid, "alice")), 1)
            projects.publish_scenario(pid, "alice", did)
            self.assertEqual(projects.list("alice")[0].default_dataset_id, did)
            self.assertEqual(projects.list("bob"), [])

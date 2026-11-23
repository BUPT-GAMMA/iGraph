import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException
from backend.playground import ScenarioStore, Turn, permits_build
from backend.playground_context import HISTORY_CHARS, planning_history
from backend.projects import ProjectDirectory
from backend.project_datasets import ProjectDatasetDirectory


class PlaygroundStoreTest(unittest.TestCase):
    def test_free_form_build_and_deferral_intent(self):
        for message in ("这两道题就定了，请按这个方案现在把示例图建出来吧。", "可以了，按这个版本建图",
                        "没问题，直接生成示例图吧", "不用再调整问题了，请开始构图", "Looks good, go ahead and build it.",
                        "就按这个方案开始吧", "Looks good, go ahead."):
            self.assertTrue(permits_build(message), message)
        for message in ("先不要构图，再调整问题", "如果我说开始构图会发生什么？", "请先删除最后一个问题再生成图",
                        "Don't build the graph yet", "After I approve, build the graph",
                        "请把项目名字定为社区供书保障演练，给我看最终方案，两道问题按我们商量的范围。",
                        "Show me the final proposal.", "请把第二题改为供货问题，然后开始构图。", "解释一下构图"):
            self.assertFalse(permits_build(message), message)

    def test_context_remains_scoped_and_an_interrupted_revision_cannot_build_the_old_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scenarios.sqlite"
            store = ScenarioStore(path)
            tid = str(uuid.uuid4())
            def turn(text, revision):
                return Turn(turn_id=uuid.uuid4(), revision=revision, message=text, connection_revision=1)
            doc, _ = store.begin("alice", tid, turn("按续签月份排优先级", 0))
            notes = [{"kind": "goal", "text": "Prioritize renewals", "quote": "按续签月份排优先级"}]
            store.update("alice", doc, status="draft", plan={"title": "园区"}, notes=notes, confirmable=True)
            self.assertEqual(ScenarioStore(path).read("alice", tid)["notes"], notes)
            self.assertNotIn("notes", store.begin("bob", tid, turn("一个新场景", 0))[0])
            edit, _ = store.begin("alice", tid, turn("只看下个月的合同，然后开始构图", 1))
            self.assertEqual(edit["status"], "thinking")
            store.update("alice", edit, deadline=0)
            self.assertFalse(store.read("alice", tid)["confirmable"])
            self.assertEqual(store.read("alice", tid)["notes"], notes)
            with self.assertRaises(HTTPException):
                store.begin("alice", tid, turn("开始构图", 2))

    def test_long_and_legacy_history_keep_original_request_and_displayed_question_numbers(self):
        messages = [{"role": "user", "text": "最初目标"}]
        for i in range(13):
            messages.extend([{"role": "user", "text": f"第{i}轮的补充"},
                             {"role": "assistant", "text": "请看方案", "questions": [
                                 {"zh": f"续签问题{i}", "en": f"Renewal question {i}"}]}])
        messages.append({"role": "user", "text": "最新消息单独传递"})
        history = planning_history(messages, "zh", bootstrap=False)
        self.assertEqual(history[0]["content"], "最初目标")
        self.assertIn("1. 续签问题12", history[-1]["content"])
        self.assertFalse(any("最新消息" in m["content"] for m in history))
        self.assertFalse(any(m["content"] == "第7轮的补充" for m in history))
        legacy = planning_history(messages, "en", bootstrap=True)
        self.assertTrue(any(m["content"] == "第7轮的补充" for m in legacy))
        self.assertIn("1. Renewal question 12", legacy[-1]["content"])
        self.assertLessEqual(len(legacy), 12)
        user_turns = [int(m["content"].split("轮")[0][1:]) for m in legacy if m["content"].startswith("第")]
        self.assertEqual(user_turns, sorted(user_turns))
        long = planning_history([{"role": "user", "text": str(i) * 4000} for i in range(10)], "en", bootstrap=True)
        self.assertLessEqual(sum(len(m["content"]) for m in long), HISTORY_CHARS)

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

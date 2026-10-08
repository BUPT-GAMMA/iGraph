"""Drafts cannot enter analysis until explicitly published."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import pytest
from graph_data_service.construction import ConstructionStore, ConstructionError
from graph_data_service.text_encoding import decode_txt
from graph_engine.construction.agent import EvolutionConfig, TabularConstructionSubAgent
from graph_engine.construction.llm import selected_model
from backend.projects import ProjectDirectory


def sample():
    return {"directed": True, "nodes": [{"id": "a", "type": "人物", "properties": {"name": "段誉"}}, {"id": "b", "type": "人物", "properties": {"name": "虚竹"}}], "edges": [{"id": "e", "source": "a", "target": "b", "type": "结义", "properties": {}}]}


@pytest.fixture
def store(tmp_path):
    source = tmp_path / "people.csv"
    source.write_text("name\n段誉\n", encoding="utf-8")
    value = ConstructionStore(tmp_path)
    value.register_algorithm("csv", lambda path, options: sample())
    value.build("u", "d", source, source.name)
    return value


def test_build_remains_draft_until_explicit_save(store):
    assert store.status("u", "d")["status"] == "draft"
    assert store.list_ready("u") == []
    assert store.graph("u", "d")["graph"] == sample()
    with pytest.raises(ConstructionError):
        store.graph("u", "d", saved_only=True)
    result = store.optimize("u", "d", 1, [], commit=True)
    assert result["status"] == "ready"
    assert store.status("u", "d")["status"] == "ready"
    assert store.graph("u", "d", saved_only=True)["graph"] == sample()
    assert len(store.list_ready("u")) == 1
    with pytest.raises(ConstructionError):
        store.optimize("u", "d", 1, [], commit=True)


def test_draft_can_be_optimized_before_save_without_mutation(store):
    operation = [{"op": "reverse_edges", "edge_ids": ["e"]}]
    preview = store.optimize("u", "d", 1, operation)
    assert store.status("u", "d")["status"] == "draft"
    assert store.graph("u", "d")["graph"] == sample()
    saved = store.optimize("u", "d", 1, operation, commit=True)
    assert saved["graph"] == preview["graph"]
    assert store.graph("u", "d", saved_only=True)["graph"]["edges"][0]["source"] == "b"


def test_simultaneous_save_has_one_revision_winner(store):
    def save():
        try:
            return store.optimize("u", "d", 1, [], commit=True)["revision"]
        except ConstructionError as exc:
            return exc.detail["code"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(), range(2)))
    assert sorted(map(str, results)) == ["2", "revision_conflict"]


def test_repeat_build_reuses_unpublished_draft(store):
    assert store.reuse_ready("u", "d")["status"] == "draft"
    assert store.build("u", "d")["revision"] == 1
    assert store.list_ready("u") == []


def test_chinese_encodings_preserve_complete_text():
    text = "段誉与虚竹结义。\n萧峰是丐帮帮主。"
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        assert decode_txt(text.encode(encoding)) == text
    assert decode_txt(text.encode("gb18030")[:-1], partial=True) == text[:-1]
    assert EvolutionConfig.from_options({"txt_mode": "entities"}).text_max_characters >= 1_235_236


def test_model_allowlist_rejects_arbitrary_provider_selection(monkeypatch):
    monkeypatch.setenv("GRAPH_CONSTRUCTION_MODELS", "deepseek-v4-pro,claude-opus-5-5")
    assert selected_model("deepseek-v4-pro") == "deepseek-v4-pro"
    with pytest.raises(ValueError):
        selected_model("https://external.invalid/model")


def test_case_projects_are_idempotent_and_isolated(tmp_path):
    directory = ProjectDirectory(tmp_path / "projects.json")
    first = directory.ensure_user_case("a", "finance", "金融示例", "说明")
    assert directory.ensure_user_case("a", "finance", "金融示例", "说明").project_id == first.project_id
    assert directory.ensure_user_case("b", "finance", "金融示例", "说明").project_id != first.project_id
    assert directory.ensure_user_case("a", "novel", "小说示例", "说明").project_id != first.project_id


def test_parallel_txt_calls_are_bounded_and_preserve_all_characters(tmp_path):
    source = tmp_path / "people.txt"
    source.write_text(("段誉与虚竹结义。" * 55 + "\n\n") * 4, encoding="gb18030")
    class LLM:
        model = "test"
        def __init__(self):
            self.lock = threading.Lock(); self.active = self.peak = self.calls = 0
        def complete_text(self, messages, **kwargs):
            with self.lock:
                self.active += 1; self.calls += 1; self.peak = max(self.peak, self.active)
            time.sleep(.01)
            with self.lock:
                self.active -= 1
            return {"content": "<entities>\n段誉 | 人物\n虚竹 | 人物\n</entities>\n<triples>\n段誉 | 人物 | 结义 | 虚竹 | 人物\n</triples>"}
    llm = LLM()
    result = TabularConstructionSubAgent(llm_factory=lambda config: llm).run(source, {
        "mode": "llm", "txt_mode": "entities", "entity_types": ["人物"], "experience_enabled": False,
        "text_chunk_tokens": 128, "text_overlap_tokens": 0, "text_parallelism": 3,
    })
    assert 1 < llm.peak <= 3
    assert llm.calls == result.report["semantic_extraction"]["chunk_count"]
    assert len(result.graph["nodes"]) == 2
    assert len(result.graph["edges"]) == 1


def test_unproven_model_entities_do_not_discard_grounded_people():
    from graph_engine.construction.agent import _parse_typed_entities
    response = "<entities>\n段誉 | 人物\n辛双清 | 人物\n双清 | 人物\n</entities>"
    assert _parse_typed_entities(response, ["人物"], "段誉见一位姓辛、道号双清的道姑。") == [{"name": "段誉", "type": "人物"}, {"name": "双清", "type": "人物"}]

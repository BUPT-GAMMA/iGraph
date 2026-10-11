"""User-facing questions for the bundled example graphs."""
import hashlib
import json
import logging
from pathlib import Path


USER_CASES = [
    {"id": "finance", "name": "金融股权示例", "filename": "finance-ownership.npz",
     "description": "三张金融表已构成股权图，可直接检查股东、控制链和集团关系。",
     "translations": {"en": {"name": "Financial ownership example",
         "description": "Three financial tables form a ready-to-use ownership graph for exploring shareholders, control chains, and corporate groups.",
         "questions": ["Check the size, ownership direction, and data quality of this financial ownership graph.",
             "Find the largest shareholder of SYN-ENT-000040, distinguishing direct ownership from cumulative economic interest.",
             "Analyze the strict control group of SYN-ENT-000001, listing its members, control chains, and original ownership evidence."]}},
     "questions": ["请检查这张金融股权图的数据规模、持股方向和数据质量问题。", "请计算 SYN-ENT-000040 的第一大股东，并区分直接持股和累计经济权益。", "请分析 SYN-ENT-000001 的严格控制集团，列出成员、控制链和原始持股证据。"]},
    {"id": "tianlongbabu", "name": "天龙八部人物关系示例", "filename": "tianlongbabu-characters.npz",
     "description": "完整小说经 deepseek-v4-pro 构成人物关系图，可直接探索人物联系和网络结构。",
     "translations": {"en": {"name": "Demi-Gods and Semi-Devils character example",
         "description": "The full novel was processed with deepseek-v4-pro into a character relationship graph, ready for exploring connections and network structure.",
         "questions": ["In Demi-Gods and Semi-Devils, what major social circles form around sects, families, and everyday interactions, and who is the most important figure in each?", "What kinship, marriage, or master–disciple relationships connect the seemingly distant Duan family of Dali, Wang family of Gusu, Xiaoyao Sect, and Western Xia royal family?", "Tracing these connections through Wang Yuyan’s (王语嫣) parentage, how do her parents and grandparents link her to the Duan family of Dali, the Xiaoyao Sect, and the Western Xia royal family?", "What easily overlooked relationships connect Wang Yuyan (王语嫣) and Xuzhu (虚竹)? Trace the connections through Xuzhu’s master Wuyazi (无崖子) and his wife Menggu (梦姑)."]}},
     "questions": ["《天龙八部》中，人物围绕门派、家族和日常往来，形成了哪些主要的江湖圈子？每个圈子中最重要的人物是谁？", "在这些圈子之间，大理段氏、姑苏王家、逍遥派和西夏皇室，看似相隔甚远，实际上通过哪些亲缘、婚姻或师承关系联系在一起？", "顺着这些联系追查王语嫣的身世，她的父母和祖辈，如何让她同时与大理段氏、逍遥派和西夏皇室产生牵连？", "王语嫣与虚竹之间，究竟藏着哪几层容易被忽略的关系？分别沿着虚竹的师父无崖子和妻子梦姑追溯，两人各有什么联系？"]},
]


def installed_source(server_root: Path, case: dict):
    """Only accept a complete, checked source published by the bundled installer."""
    directory = server_root / ".user-case-sources" / Path(case["filename"]).stem
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return None
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    filename = source["filename"]
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError("Invalid bundled source filename")
    checksum = source["sha256"]
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ValueError("Invalid bundled source checksum")
    path = directory / checksum / filename
    if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
        raise ValueError("Bundled source checksum mismatch")
    return path, source


# Only administrator-bundled snapshots can select a background document.
# Names and descriptions are user editable and must not determine graph identity.
_BACKGROUND_RESOURCES = Path(__file__).resolve().parents[1] / "graph_engine" / "resources"


def background_for_dataset(dataset):
    """Read the matching, packaged Markdown afresh for each turn/dataset change."""
    if dataset is None or dataset.status != "ready" or dataset.source_kind != "server":
        return None
    case = next((item for item in USER_CASES
                 if item["id"] == "tianlongbabu"
                 and dataset.metadata.get("source_relative_path") == item["filename"]), None)
    if case is None or dataset.file_type != "npz":
        return None
    resources = _BACKGROUND_RESOURCES / case["id"]
    try:
        manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
        if manifest["filename"] != case["filename"]:
            return None
        with Path(dataset.storage_path).open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        if checksum != manifest["sha256"]:
            return None
        content = (resources / "background.md").read_text(encoding="utf-8").strip()
        if not content:
            return None
    except (OSError, ValueError, KeyError):
        logging.getLogger(__name__).warning("Bundled graph background unavailable for %s", case["id"])
        return None
    return {"case_id": case["id"], "dataset_id": dataset.dataset_id,
            "title": case["name"], "content": content}

"""User-facing questions for the bundled example graphs."""
import hashlib
import json
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
         "questions": ["Find the nodes for Xiao Feng (萧峰), Qiao Feng (乔峰), Duan Yu (段誉), and Xuzhu (虚竹), then show their direct relationships. Distinguish aliases using graph evidence.",
             "Find the ten most connected characters, explain the calculation, and display the relevant subgraph.",
             "Run community detection on the character graph and summarize each community's representative characters and connections, using only graph evidence."]}},
     "questions": ["请先查找萧峰、乔峰、段誉和虚竹对应的节点，再展示他们的直接人物关系；别名需按图中证据区分。", "请找出人物关系图中连接最多的十个人物，说明计算方法并展示相关子图。", "请对人物关系图进行社区检测，概括各社区的代表人物与相互联系，只依据图中关系作答。"]},
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

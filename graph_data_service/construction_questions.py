"""Bilingual conversation starters selected from a committed property graph."""
from __future__ import annotations

import math
import re
from collections import Counter
from itertools import islice

from graph_data_service.construction_schema import schema_foreign_key

_NAME_KEYS = ("name", "名称", "姓名", "title", "标题", "label", "chn_nm", "idv_nm", "eng_nm", "value")
_GENERATED_ID = re.compile(r"(?:row|entity|edge|fk|node)_[0-9a-f]{16,}$", re.I)
_MEASURE_FIELD = re.compile(r"price|amount|weight|score|rate|ratio|rto|pct|cost|duration|距离|数量|金额|价格|评分|占比|比例", re.I)
_ID_FIELD = re.compile(r"(?:^|_)(?:id|uuid|key|code|index)(?:$|_)|(?:编号|代码|编码|序号)$", re.I)


def _fields(row):
    props = row["properties"]
    # CSV/SQL source rows and the TXT entity extractor both keep names in data.
    data = props.get("data")
    return data if isinstance(data, dict) else props


def _short(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 80 and value.isprintable()


def _name(node):
    fields = _fields(node)
    by_key = {key.casefold(): value for key, value in fields.items() if _short(value)}
    for key in _NAME_KEYS:
        if key in by_key:
            return by_key[key].strip()
    identity = node["id"]
    return identity if _short(identity) and not _GENERATED_ID.fullmatch(identity) else None


def _number(value):
    if type(value) not in (int, float, str) or isinstance(value, str) and len(value) > 40:
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _measure(rows):
    """Find a comparable field with at least two different observed values."""
    first, comparable = {}, Counter()
    for row in islice(rows, 512):
        for field, value in islice(_fields(row).items(), 80):
            if not _short(field) or _ID_FIELD.search(field) or field in {"row_index", "mentions"}:
                continue
            number = _number(value)
            if number is None:
                continue
            key = (row["type"], field)
            if number != first.setdefault(key, number):
                comparable[key] += 1
    return max(comparable, key=lambda k: (bool(_MEASURE_FIELD.search(k[1])), comparable[k]), default=None)


def _relation(edge):
    kind = edge["type"]
    if not _GENERATED_ID.fullmatch(kind):
        return kind if _short(kind) else None
    fk = schema_foreign_key(edge)
    if fk and len(fk["columns"]) <= 4 and all(_short(c) for c in fk["columns"]) and _short(fk["ref_table"]):
        return ", ".join(fk["columns"]) + " → " + fk["ref_table"]
    return None


def graph_questions(graph):
    zh, en, zh_titles, en_titles = [], [], [], []

    def add(zh_title, en_title, chinese, english):
        zh_titles.append(zh_title)
        en_titles.append(en_title)
        zh.append(chinese)
        en.append(english)

    nodes, edges = graph["nodes"], graph["edges"]
    if nodes:
        node_types = Counter(n["type"] for n in nodes)
        common = node_types.most_common(1)[0][0]
        degree = Counter()
        neighbors = {}
        for edge in edges:
            a, b = edge["source"], edge["target"]
            degree.update((a, b))
            if a == b:
                continue
            for node, other in ((a, b), (b, a)):
                pair = neighbors.setdefault(node, [])
                if len(pair) < 2 and other not in pair:
                    pair.append(other)
        by_id = {node["id"]: node for node in nodes}
        hub = max(nodes, key=lambda n: (len(neighbors.get(n["id"], ())), degree[n["id"]]))
        anchor = _name(hub)
        if edges:
            incident = next(e for e in edges if hub["id"] in (e["source"], e["target"]))
            relation = _relation(incident)
            if anchor:
                zh_relation = f"沿着「{relation}」关系，" if relation else ""
                en_relation = f" through “{relation}”" if relation else ""
                add("探索直接关系", "Explore direct connections",
                    f"{zh_relation}「{anchor}」直接连接了哪些实体？按实体类型整理，并展示对应的边。",
                    f"Which entities connect directly to “{anchor}”{en_relation}? Group them by type and show the supporting edges.")
            else:
                add("寻找连接中心", "Find connection hubs",
                    f"哪些「{hub['type']}」节点连接的不同对象最多？列出排名、相邻节点和对应关系。",
                    f"Which “{hub['type']}” nodes have the most distinct neighbors? Rank them and show the neighbors and relationships.")
        else:
            add("查看节点记录", "Explore node records",
                f"图中有哪些「{common}」节点？选取几个实例，对照它们实际保存的属性。",
                f"Which “{common}” nodes are in this graph? Compare the stored properties of a few examples.")

        for rows, is_edge in ((edges, True), (nodes, False)):
            measure = _measure(rows)
            if measure:
                kind, field = measure
                if is_edge and _GENERATED_ID.fullmatch(kind):
                    continue
                zh_kind, en_kind = ("关系", "relationships") if is_edge else ("节点", "nodes")
                add(f"比较{field}", f"Compare {field}",
                    f"哪些「{kind}」{zh_kind}的「{field}」最高，哪些最低？列出对应的记录和值，看差距有多大。",
                    f"Which “{kind}” {en_kind} have the highest and lowest “{field}” values? Show the records and compare the gap.")
                break

        pair = neighbors.get(hub["id"], [])
        if len(pair) == 2:
            left, right = (_name(by_id[key]) for key in pair)
            if left and right and left != right:
                add("寻找共同邻居", "Find shared neighbors",
                    f"「{left}」和「{right}」有哪些共同邻居？画出经这些邻居相连的两条边，并标出关系类型。",
                    f"Which neighbors do “{left}” and “{right}” share? Show the two edges via each shared neighbor and label their types.")
            elif anchor:
                add("查看两步连接", "Explore two-edge connections",
                    f"哪些不同节点可以经由「{anchor}」相连？展示一个包含两条边的实例，说明每条边的含义。",
                    f"Which distinct nodes are connected via “{anchor}”? Show a two-edge example and explain each edge.")

        evidence_edge = next((e for e in edges if isinstance(e["properties"].get("evidence"), str)
                              and e["properties"]["evidence"].strip() and _relation(e)), None)
        isolated = next((n for n in nodes if not degree[n["id"]]), None)
        if evidence_edge:
            relation = _relation(evidence_edge)
            add("核对关系证据", "Check relationship evidence",
                f"哪些原文片段支持「{relation}」这一关系？列出两端的实体，区分原文直接陈述和需要推断的部分。",
                f"Which source passages support “{relation}”? Identify the entities at both ends and distinguish explicit statements from inferences.")
        elif isolated is not None and edges:
            kind = isolated["type"]
            add("检查孤立节点", "Inspect isolated nodes",
                f"哪些「{kind}」节点没有任何连边？列出它们的属性，与图中有连接的记录对照。",
                f"Which “{kind}” nodes have no incident edges? Show their properties alongside records that have connections.")
        elif len(node_types) > 1:
            kinds = "、".join(k for k, _ in node_types.most_common(3))
            types_en = ", ".join(k for k, _ in node_types.most_common(3))
            add("对比节点类型", "Compare node types",
                f"比较「{kinds}」这几类节点的数量和代表性属性。哪些差异最值得继续分析？",
                f"Compare counts and representative properties for these node types: {types_en}. Which differences warrant a closer look?")
        elif edges:
            add("连接分布", "Connection patterns",
                f"在「{common}」节点中，连接是集中在少数节点上，还是分布均匀？用度数分布和具体节点说明。",
                f"Are connections concentrated among a few “{common}” nodes or spread evenly? Use the degree distribution and example nodes.")
        elif len(nodes) > 1:
            add("比较记录完整度", "Compare field coverage",
                f"「{common}」节点有哪些共有字段？找出字段缺失或取值不同的记录，并列出依据。",
                f"Which fields do “{common}” nodes share? Show records with missing or differing values and cite the fields.")
    return {"questions": zh, "question_titles": zh_titles,
            "translations": {"en": {"questions": en, "question_titles": en_titles}}}

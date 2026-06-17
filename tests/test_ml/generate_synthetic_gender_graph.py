from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import List, Sequence, Tuple

MALE_FIRST_NAMES: Sequence[str] = (
    "Liam", "Noah", "Oliver", "Elijah", "James", "William", "Benjamin", "Lucas",
    "Henry", "Alexander", "Mason", "Michael", "Ethan", "Daniel", "Jacob", "Logan",
    "Jackson", "Levi", "Sebastian", "Mateo", "Jack", "Owen", "Theodore", "Aiden",
    "Samuel", "Joseph", "John", "David", "Wyatt", "Matthew", "Luke", "Asher",
    "Carter", "Julian", "Grayson", "Leo", "Jayden", "Gabriel", "Isaac", "Lincoln",
    "Anthony", "Hudson", "Dylan", "Ezra", "Thomas", "Charles", "Christopher",
    "Jaxon", "Maverick", "Josiah", "Isaiah", "Andrew", "Elias", "Joshua",
    "Nathan", "Caleb", "Ryan", "Adrian", "Miles", "Eli",
)

FEMALE_FIRST_NAMES: Sequence[str] = (
    "Olivia", "Emma", "Charlotte", "Amelia", "Sophia", "Isabella", "Ava", "Mia",
    "Evelyn", "Luna", "Harper", "Camila", "Sofia", "Scarlett", "Elizabeth",
    "Eleanor", "Emily", "Chloe", "Mila", "Violet", "Penelope", "Gianna", "Aria",
    "Abigail", "Ella", "Avery", "Hazel", "Nora", "Lily", "Aurora", "Nova",
    "Ellie", "Madison", "Grace", "Isla", "Willow", "Zoey", "Riley", "Stella",
    "Eliana", "Ivy", "Victoria", "Hannah", "Emilia", "Zoe", "Naomi", "Lucy",
    "Elena", "Leah", "Lillian", "Addison", "Aubrey", "Eleanor", "Bella",
    "Skylar", "Paisley", "Everly", "Anna", "Caroline", "Genesis",
)

LAST_NAMES: Sequence[str] = (
    "Smith", "Johnson", "Brown", "Taylor", "Anderson", "Thomas", "Jackson",
    "White", "Harris", "Martin", "Thompson", "Garcia", "Martinez", "Robinson",
    "Clark", "Rodriguez", "Lewis", "Lee", "Walker", "Hall", "Allen", "Young",
    "Hernandez", "King", "Wright", "Lopez", "Hill", "Scott", "Green", "Adams",
    "Baker", "Nelson", "Carter", "Mitchell", "Perez", "Roberts", "Turner",
    "Phillips", "Campbell", "Parker", "Evans", "Edwards", "Collins", "Stewart",
    "Sanchez", "Morris", "Rogers", "Reed", "Cook", "Morgan", "Bell", "Murphy",
    "Bailey", "Rivera", "Cooper", "Richardson", "Cox", "Howard", "Ward", "Torres",
)


def _sample_unique_names(
    rng: random.Random,
    first_name_pool: Sequence[str],
    count: int,
    used: set[str],
) -> List[str]:
    names: List[str] = []
    max_retry = 10000
    retry = 0
    while len(names) < count and retry < max_retry:
        first = str(rng.choice(first_name_pool))
        last = str(rng.choice(LAST_NAMES))
        name = f"{first} {last}"
        retry += 1
        if name in used:
            continue
        used.add(name)
        names.append(name)
    if len(names) != count:
        raise RuntimeError("姓名库可用组合不足，无法生成唯一节点名称。")
    return names


def _add_edge(edge_set: set[Tuple[int, int]], u: int, v: int) -> None:
    if u == v:
        return
    if u > v:
        u, v = v, u
    edge_set.add((u, v))


def build_graph_document(
    *,
    seed: int = 42,
    feature_dim: int = 8,
    intra_extra_prob: float = 0.12,
    inter_prob: float = 0.03,
    ring_neighbors: int = 4,
    visible_label_count: int = 40,
) -> Tuple[dict, dict]:
    rng = random.Random(seed)

    male_count = 50
    female_count = 50
    num_nodes = male_count + female_count
    male_ids = list(range(male_count))
    female_ids = list(range(male_count, num_nodes))

    # 1) 节点名称与类别
    used_names: set[str] = set()
    male_names = _sample_unique_names(rng, MALE_FIRST_NAMES, male_count, used_names)
    female_names = _sample_unique_names(rng, FEMALE_FIRST_NAMES, female_count, used_names)
    all_names = male_names + female_names

    node_type = ["person"] * num_nodes
    node_label_groundtruth = (["男性"] * male_count) + (["女性"] * female_count)
    visible_ids = set(rng.sample(list(range(num_nodes)), k=visible_label_count))
    node_label_observed = [
        node_label_groundtruth[i] if i in visible_ids else None
        for i in range(num_nodes)
    ]

    # 2) 节点特征：N(0, 0.5) / N(1, 0.5)
    std = math.sqrt(0.5)

    def sample_feat(mu: float) -> List[float]:
        return [rng.gauss(mu, std) for _ in range(feature_dim)]

    node_feat = [sample_feat(0.0) for _ in range(male_count)] + [
        sample_feat(1.0) for _ in range(female_count)
    ]

    # 3) 连边：社区内稠密 + 社区间稀疏
    edges: set[Tuple[int, int]] = set()

    def build_community(ids: Sequence[int]) -> None:
        n = len(ids)
        # 环形近邻，保证每个社区连通且局部紧密
        for i, u in enumerate(ids):
            for k in range(1, ring_neighbors + 1):
                v = ids[(i + k) % n]
                _add_edge(edges, u, v)
        # 额外随机边提升密度
        for i in range(n):
            for j in range(i + 1, n):
                if rng.random() < intra_extra_prob:
                    _add_edge(edges, ids[i], ids[j])

    build_community(male_ids)
    build_community(female_ids)

    for u in male_ids:
        for v in female_ids:
            if rng.random() < inter_prob:
                _add_edge(edges, u, v)

    # 至少保证存在跨社区连接
    if not any((u < male_count <= v) for u, v in edges):
        for _ in range(5):
            u = int(rng.choice(male_ids))
            v = int(rng.choice(female_ids))
            _add_edge(edges, u, v)

    edge_list = sorted(edges)
    src = [u for u, _ in edge_list]
    dst = [v for _, v in edge_list]
    edge_index = [src, dst]

    edge_names = [f"e{i:04d}" for i in range(len(edge_list))]
    edge_weight = [1.0] * len(edge_list)
    edge_type = [
        "community_internal" if (u < male_count and v < male_count) or (u >= male_count and v >= male_count)
        else "cross_community"
        for u, v in edge_list
    ]

    id2node = {str(i): all_names[i] for i in range(num_nodes)}

    graph_doc = {
        "schema": "graph_data.v1",
        "num_nodes": num_nodes,
        "directed": False,
        "name": "synthetic_gender_community_100",
        "graph_desc": (
            "100节点合成图：男性/女性各50；节点特征分别采样自 N(0,0.5) 与 N(1,0.5)，"
            "社区内高密度连接，社区间有少量连接。仅40个节点标签可见，其余标签缺失。"
        ),
        "id2node": id2node,
        "edge_index": edge_index,
        "edge_weight": edge_weight,
        "edge_names": edge_names,
        "node_feat": node_feat,
        "node_label": node_label_observed,
        "node_type": node_type,
        "edge_type": edge_type,
        "graph_label": "gender_community_graph",
    }
    groundtruth_doc = {
        "schema": "synthetic_label_groundtruth.v1",
        "num_nodes": num_nodes,
        "visible_label_count": visible_label_count,
        "visible_node_ids": sorted(visible_ids),
        "node_label_groundtruth": node_label_groundtruth,
        "node_label_observed": node_label_observed,
        "id2node": id2node,
    }
    return graph_doc, groundtruth_doc


def main() -> None:
    parser = argparse.ArgumentParser(description="生成可被 graph_data_service 识别的图数据 JSON。")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "synthetic_gender_community_100.json",
        help="输出 JSON 文件路径",
    )
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--feature-dim", type=int, default=8, help="节点特征维度")
    parser.add_argument("--intra-extra-prob", type=float, default=0.12, help="社区内额外随机连边概率")
    parser.add_argument("--inter-prob", type=float, default=0.03, help="社区间连边概率")
    parser.add_argument("--ring-neighbors", type=int, default=4, help="每节点在社区环形结构中连接的近邻数")
    parser.add_argument("--visible-label-count", type=int, default=40, help="可见节点标签数量")
    parser.add_argument(
        "--groundtruth-output",
        type=Path,
        default=Path(__file__).resolve().parent / "synthetic_gender_community_100_groundtruth.json",
        help="groundtruth 输出 JSON 文件路径",
    )
    args = parser.parse_args()

    if args.visible_label_count < 2 or args.visible_label_count > 100:
        raise ValueError("visible-label-count 必须在 [2, 100] 范围内。")

    doc, gt = build_graph_document(
        seed=args.seed,
        feature_dim=args.feature_dim,
        intra_extra_prob=args.intra_extra_prob,
        inter_prob=args.inter_prob,
        ring_neighbors=args.ring_neighbors,
        visible_label_count=args.visible_label_count,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    args.groundtruth_output.parent.mkdir(parents=True, exist_ok=True)
    with args.groundtruth_output.open("w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=2)

    edge_count = len(doc["edge_index"][0])
    labeled_count = sum(1 for x in doc["node_label"] if x is not None)
    print(f"saved: {args.output}")
    print(f"saved: {args.groundtruth_output}")
    print(f"nodes={doc['num_nodes']} edges={edge_count} directed={doc['directed']}")
    print(f"visible_labels={labeled_count} missing_labels={doc['num_nodes'] - labeled_count}")


if __name__ == "__main__":
    main()

"""Large-path compatibility contract for public small-graph algorithms.

The small-graph implementation and tool catalogue remain untouched. Tests
compare this mapping to those read-only sources so a missing large-graph
counterpart becomes a CI failure.
"""

from __future__ import annotations


SMALL_GRAPH_ALGORITHM_MODES: dict[str, tuple[str, ...]] = {
    "shortest_path": ("default", "yen_k_shortest"),
    "all_shortest_paths": (),
    "maximum_flow": (),
    "minimum_cut": (),
    "global_minimum_cut": (),
    "minimum_spanning_tree": (),
    "community_detection": (
        "louvain",
        "greedy_modularity",
        "label_propagation",
        "label_propagation_asyn",
        "leiden",
        "girvan_newman",
        "asyn_fluidc",
    ),
    "node_centrality": (
        "degree",
        "betweenness",
        "closeness",
        "pagerank",
        "eigenvector",
        "harmonic",
        "katz",
        "load",
        "voterank",
        "percolation",
    ),
    "graph_connectivity": (),
    "find_cycles": (),
    "maximum_cycle": ("length", "weight"),
    "topological_sort": (),
    "degree_analysis": (),
    "subgraph_extraction": (
        "ego_graph",
        "k_core",
        "k_truss",
        "k_corona",
        "node_subset",
    ),
    "link_prediction_score": (
        "jaccard",
        "adamic_adar",
        "resource_allocation",
        "preferential_attachment",
    ),
    "path_analysis": (
        "all_simple_paths",
        "has_path",
        "average_shortest_path_length",
    ),
    "clique_analysis": ("find_cliques", "clique_number", "number_of_cliques"),
    "reachability": ("transitive_closure", "descendants", "ancestors"),
    "global_metrics": (
        "density",
        "diameter",
        "radius",
        "transitivity",
        "average_clustering",
        "degree_assortativity",
    ),
    # Grouped operators added on dev.  Their complete selector contract is
    # implemented by the partitioned grouped-analysis kernel.
    "graph_structure_analysis": (
        "is_tree",
        "is_forest",
        "is_directed_acyclic_graph",
        "bfs_tree",
        "dfs_tree",
        "lexicographical_topological_sort",
        "all_topological_sorts",
        "is_at_free",
        "check_planarity",
        "is_tournament",
        "score_sequence",
        "is_bipartite",
        "weisfeiler_lehman_graph_hash",
    ),
    "connectivity_analysis": (
        "node_connectivity",
        "edge_connectivity",
        "minimum_node_cut",
        "minimum_edge_cut",
        "articulation_points",
        "bridges",
        "local_bridges",
        "biconnected_component_edges",
        "bridge_components",
        "condensation",
    ),
    "tree_dag_analysis": (
        "maximum_spanning_tree",
        "random_spanning_tree",
        "lowest_common_ancestor",
        "immediate_dominators",
        "dominance_frontiers",
        "dag_longest_path",
        "dag_longest_path_length",
        "antichains",
        "is_aperiodic",
        "colliders",
        "v_structures",
    ),
    "matching_coloring_analysis": (
        "max_weight_matching",
        "min_weight_matching",
        "maximal_matching",
        "min_edge_cover",
        "is_matching",
        "greedy_color",
        "equitable_color",
    ),
    "distance_measures": (
        "center",
        "periphery",
        "eccentricity",
        "wiener_index",
        "global_efficiency",
        "local_efficiency",
        "average_neighbor_degree",
        "average_degree_connectivity",
        "rich_club_coefficient",
        "reciprocity",
        "flow_hierarchy",
        "barycenter",
        "estrada_index",
        "girth",
    ),
}

SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS: dict[str, str] = {
    "shortest_path": "algorithm",
    "community_detection": "algorithm",
    "node_centrality": "centrality_type",
    "maximum_cycle": "optimize_by",
    "subgraph_extraction": "method",
    "link_prediction_score": "method",
    "path_analysis": "method",
    "clique_analysis": "method",
    "reachability": "method",
    "global_metrics": "metric",
    "graph_structure_analysis": "method",
    "connectivity_analysis": "method",
    "tree_dag_analysis": "method",
    "matching_coloring_analysis": "method",
    "distance_measures": "metric",
}

SMALL_GRAPH_ALGORITHM_MODE_DEFAULTS: dict[str, str] = {
    "shortest_path": "default",
    "community_detection": "louvain",
    "node_centrality": "degree",
    "maximum_cycle": "length",
}

SMALL_GRAPH_ALGORITHM_NAMES: frozenset[str] = frozenset(
    SMALL_GRAPH_ALGORITHM_MODES
)

LARGE_GRAPH_FAST_ALGORITHM_NAMES: tuple[str, ...] = (
    "degree",
    "in_degree",
    "out_degree",
    "degree_centrality",
)

# GraphScope-only operations are intentionally absent from the small-graph
# catalogue.  They are exposed only by ``run_large_graph_algorithm`` so adding
# native large-graph capabilities never changes GraphCompute/NetworkX routing.
GRAPHSCOPE_ONLY_ALGORITHM_NAMES: tuple[str, ...] = (
    "strongly_connected_components",
    "biconnected_components",
    "bridges",
    "articulation_points",
    "minimum_spanning_forest",
    "graph_coloring",
    "maximal_matching",
    "hits",
    "k_shell",
    "personalized_pagerank",
    "approximate_diameter",
    "fluid_community",
    "densest_subgraph",
    "independent_set_cover",
    "motif_counting",
)

LARGE_GRAPH_ALGORITHM_NAMES: frozenset[str] = (
    SMALL_GRAPH_ALGORITHM_NAMES
    | frozenset(LARGE_GRAPH_FAST_ALGORITHM_NAMES)
    | frozenset(GRAPHSCOPE_ONLY_ALGORITHM_NAMES)
)

# Retained as a compatibility export for callers/tests written while the five
# grouped operators were guarded.  All grouped modes now have a large path.
LARGE_GRAPH_GUARDED_ALGORITHM_NAMES: frozenset[str] = frozenset()

SMALL_GRAPH_CONCRETE_ALGORITHM_COUNT = sum(
    len(modes) if modes else 1
    for modes in SMALL_GRAPH_ALGORITHM_MODES.values()
)

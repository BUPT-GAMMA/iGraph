---
name: graph_analysis_community
description: Select community detection algorithms, interpret communities, and investigate related subgraphs.
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [community_detection, run_large_graph_algorithm]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis]
---

# Community analysis

## When to use
Use for clustering, groups, modules, communities, suspicious groups, or partitioning questions.

## Procedure
1. Confirm graph state.
2. Run community detection.
3. Explain the number and sizes of communities.
4. For follow-up questions, inspect key nodes or representative members within relevant communities.

## Community membership of high-in-degree nodes
When the user asks which communities contain high-in-degree or most-cited nodes, follow this order:

1. Run exact `in_degree` once on the original graph version. Use the requested `top_k`, or 20 if unspecified. For large graphs, use `run_large_graph_algorithm`. Do not substitute `degree_analysis.top_degree_nodes`, which ranks by total degree, for an in-degree ranking.
2. Extract node IDs from `summary.top` in the exact in-degree result.
3. On the same original graph version, run `community_detection` once, passing those IDs as `focus_nodes`. For graphs with tens of millions of nodes, default to `algorithm=label_propagation` and `max_iterations=5`. Explain that this is a scalable, weighted label-propagation approximation.
4. Use `focus_node_communities` to summarize each high-in-degree node's in-degree, community ID, community size, and representative nodes.

For this question, do not call `subgraph_extraction/ego_graph`, repeat community detection for each node, or set `create_version=true`. If exact in-degree computation fails, report the error and stop; do not fall back to total-degree rankings.

## Verification
Explain the algorithm's assumptions and how directed or weighted edges affect the results.

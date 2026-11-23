---
name: graph_path_connectivity
description: Analyze shortest paths, connected components, reachability, and connectivity.
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [shortest_path, connected_components]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis]
---

# Paths and connectivity

## When to use
Use for shortest paths, reachability, disconnected graphs, connected components, or bridging questions.

## Procedure
1. Confirm graph state and directionality.
2. Select a path or connected-component tool appropriate to the question.
3. Run the smallest relevant analysis.
4. Clearly explain unreachable cases.

## Verification
Check whether edge weights should affect path length.

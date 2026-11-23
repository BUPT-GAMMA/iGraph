---
name: graph_analysis_centrality
description: Select and explain centrality algorithms, their applicability, and result verification.
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [node_centrality]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis]
---

# Centrality analysis

## When to use
Use for questions about important or influential nodes, hubs, bridges, or rankings.

## Procedure
1. Confirm graph state and directionality.
2. Select a centrality method appropriate to the question.
3. Run the centrality analysis.
4. Explain the metric in plain language and state its limitations.

## Notes
Do not present centrality as causal influence unless the graph's semantics support that interpretation.

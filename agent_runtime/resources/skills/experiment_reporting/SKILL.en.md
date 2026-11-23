---
name: experiment_reporting
description: Organize graph analysis or GraphML results into reports covering methods, metrics, and limitations.
metadata:
  version: 1.0.0
  domain: reporting
  risk_level: low
  igraph:
    any_tools: [get_graph_info]
    requires_graph_state: none
---

# Experiment reporting

## When to use
Use when the user requests a report, summary, experiment log, or explanation of reproducible results.

## Procedure
1. Describe the graph or dataset context.
2. Summarize the method and sequence of tool calls.
3. Report key metrics and results.
4. State assumptions, limitations, and recommended follow-up checks.

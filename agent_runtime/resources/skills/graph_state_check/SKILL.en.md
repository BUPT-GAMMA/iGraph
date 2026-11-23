---
name: graph_state_check
description: Check graph loading status, size, directionality, weights, and attributes.
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [get_graph_info]
    requires_graph_state: none
---

# Graph state check

## When to use
Use before analysis, training, inference, or path search, or when the user asks what data is currently loaded.

## Procedure
1. Call `get_graph_info`.
2. Confirm whether a graph is loaded.
3. Report node count, edge count, directionality, weight availability, and relevant attributes.
4. Confirm the source type using `session_state.data_state.source_format/source_type`.
   Check construction status only for SQL/CSV/TXT sources. When ready, use `load_constructed_graph` to reuse the saved graph; if the algorithm is not ready, explain according to `graph_construction`.
   Existing graph files such as PT do not use the construction workflow. Analyze them directly when loaded; otherwise guide the user to select and load them in the dataset panel.
   If construction status is `not_applicable`, verify with `get_graph_info` and do not retry construction tools.
   Ask the user for data only when no usable dataset is available.

## Verification
Do not run downstream algorithms until loading status is clear.

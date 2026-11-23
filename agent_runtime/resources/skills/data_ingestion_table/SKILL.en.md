---
name: data_ingestion_table
description: Check table and TXT upload formats; route SQL/CSV/TXT sources to graph_construction and load other supported graph formats through their existing workflows.
metadata:
  version: 1.0.0
  domain: data_ingestion
  risk_level: medium
  igraph:
    any_tools: [load_graph_from_upload]
    requires_graph_state: none
    activates_on_intents: [data_ingestion]
---

# Table data ingestion

## When to use
Use when the user uploads or references tabular edge/node data, or uploads plain TXT text. For SQL, CSV, and TXT files, first follow the `graph_construction` skill to check dataset status. Load the saved graph when ready; otherwise use the construction entry in the dataset panel. Report unavailable algorithms accurately.
Do not substitute this skill's legacy upload-loading steps for the construction algorithm.

## Required checks
- Before loading other supported table formats, confirm the input format specified by the user.
- Preserve that format's assumptions about directionality and weights.
- For TXT, distinguish strictly delimited tables, line-by-line records, and semantic extraction. `auto` / `lines` do not infer entity relationships. To extract entities and edges from ordinary text, use the explicit `entities` mode in `graph_construction`.
- `entities` does not require PK/FK, but requires an LLM. Explain before starting that the complete text will be sent to the construction model in chunks. Results pass endpoint grounding and graph structure checks only, and still need human review.

## Procedure
1. Confirm the intended graph input format. Route SQL, CSV, and TXT to the dataset construction workflow first.
2. For other established graph formats, use `load_graph_from_upload` after confirming the format.
3. Report node count, edge count, directionality, and weight availability. For semantic TXT, also remind the user to check the extracted entities and relationships.

## Verification
After construction, use the graph state tool or the returned result to verify that the graph loaded successfully.

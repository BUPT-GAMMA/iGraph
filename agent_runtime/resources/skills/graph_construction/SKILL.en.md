---
name: graph_construction
description: Check SQL/CSV/TXT construction status, reuse saved graphs, and preview, confirm, and persist six types of graph optimization.
metadata:
  version: 1.0.0
  domain: data_ingestion
  risk_level: medium
  igraph:
    requires_tools: [get_graph_construction_status, get_constructed_graph, load_constructed_graph]
    any_tools: [get_graph_construction_capabilities, preview_graph_optimization, commit_graph_optimization, visualize_subgraph]
    requires_graph_state: none
    activates_on_intents: [data_ingestion, graph_construction, dataset_management, graph_optimization]
---

# Dataset graph construction and optimization

SQL, CSV, and TXT files are saved as raw dataset sources. Construction algorithms have a separate extension entry point. Do not bypass dataset construction with legacy edge-table, text, or triple loaders.

## Choosing a TXT mode
- By default, `auto` recognizes only strictly tab-delimited tables; otherwise it deterministically preserves each line as a `text` record. `lines` always creates one record per line. Neither infers entity relationships from ordinary text or sends the full text to a model just because it resembles natural language. Explicitly select rule mode in the workbench for a fully offline workflow.
- When the user explicitly wants entities and edges extracted from plain TXT, guide them to choose "Extract entities and relationships (LLM)" in the construction workbench. This submits `txt_mode=entities` and `mode=llm`. It handles prose and one-fact-per-line text without PK/FK.
- Before semantic extraction, explain that the complete text is sent in chunks to the configured construction model. The system publishes only fully processed graphs whose endpoints are grounded in source chunks; a final failure in any chunk leaves no partial graph. Endpoint grounding and graph contract validation do not prove relationship semantics. The user must still review entity merging, relationship types, and directions.
- `entities` cannot be combined with delimiters or `rules`. If model settings are missing, no explicit relationships exist, or character/chunk/call/graph-size limits are exceeded, report the actual failure. Do not switch modes to fabricate success.

## Status and reuse
1. Obtain the target ID from `session_state.data_state.active_dataset_id` or a verified dataset list. Do not guess IDs, paths, or owners.
   If the current message includes attachments, first inspect `session_state.data_state.construction_sources`, which provides each attachment's `dataset_id`, `name`, `source_filename`, `source_format`, and `status`. For a request about an attachment, select its record rather than another `active_dataset_id`. Distinguish multiple attachments by filename and user intent.
   `available_constructed_datasets` lists constructed datasets that the current user can load or optimize in the current project/personal scope. Loading and preview/commit accept only IDs from this list or the active dataset. Select a dataset in the frontend first if a valid session scope is missing.
2. Call `get_graph_construction_status(dataset_id)`. Call `get_graph_construction_capabilities()` when algorithm support needs to be checked.
3. `ready`: load the saved version with `load_constructed_graph(dataset_id)`, then use existing analysis and visualization tools. Reuse the same dataset across sessions without reconstructing it.
4. `not_built`: guide the user to click "Construct graph" in the dataset panel. `algorithm_unavailable`: explain that the format's construction algorithm is not integrated and that the original file is retained; do not claim success. `failed`: report the actual error.
5. Do not use `load_graph_from_upload` for these sources, or copy their contents to `load_graph_from_text`, `load_graph`, or triple tools to bypass construction. Use `entities` above for semantic extraction from uploaded plain TXT. Independent inline data pasted directly by the user still follows the existing ingestion workflow.
6. With a constructed property graph loaded, generic loaders with `append=true` return `UNSUPPORTED_PROPERTY_GRAPH_APPEND` and preserve the graph. Explain this limitation and use supported optimization operations for applicable edits. Replacing or clearing the graph requires an explicit user request; it is not an automatic retry for failed appending.

## Six editing operations
First call `get_constructed_graph` to read the revision and actual nodes, edges, and properties. Each operation includes an `op` field:

| op | Required parameters | Effect |
|---|---|---|
| collapse_node | node_id, edge_type | Collapse an intermediate node used only for connections into edges |
| node_property_to_edge | node_id, property, edge_ids | Move a node property to specified incident edges; optional target_property and remove_from_node |
| extract_property_node | node_ids, property, node_type, edge_type | Extract a property into a separate node; optional remove_from_node |
| delete | target=node/edge, ids | Delete specified elements; if property is specified, delete only that property; cascade must be explicit |
| reverse_edges | edge_ids | Reverse specified edge directions |
| rename_type | target=node/edge, old_type, new_type | Rename a business type |

1. Call `preview_graph_optimization` with the retrieved `base_revision` and complete `operations`.
2. Show the preview differences and explain the effects on nodes, edges, and properties. Preserve content outside the user's intended changes. Property overwrites or invalid collapses may be rejected; adjust based on the actual error.
3. Only after a successful preview, call `commit_graph_optimization` with exactly the same revision and operations, obtaining approval through the existing tool confirmation flow. Do not describe a preview as saved.
4. On conflict, reread, preview, and confirm again. Do not simply change to the latest revision and replay. If commit fails or status is unknown, check the status before taking action; do not automatically resubmit.
5. After success, report the new revision, reload the current session with `load_constructed_graph`, verify with `get_graph_info`, and display the saved result with `visualize_subgraph`.

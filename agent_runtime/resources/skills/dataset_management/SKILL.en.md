---
name: dataset_management
description: Save, load, version, and reuse graph datasets.
metadata:
  version: 1.0.0
  domain: dataset
  risk_level: medium
  igraph:
    any_tools: [load_dataset, save_as_dataset, list_datasets, get_graph_construction_status, load_constructed_graph]
    requires_graph_state: none
---

# Dataset management

## When to use
Use when the user wants to save, reload, list, version, or reuse a graph dataset.

## Procedure
1. When loading, list or confirm the dataset and version.
2. When saving, confirm that the current graph is the intended output.
3. Preserve the name, description, and intended reuse.
4. On success, report the dataset ID and version.

SQL/CSV/TXT datasets use the `graph_construction` workflow: check construction status first, then call `load_constructed_graph` when the status is `ready` to load the persisted result. There is no need to reconstruct the graph or save a duplicate dataset.
Graph optimizations are previewed, confirmed, and saved as a new revision of the original dataset. Later sessions can load the latest version directly.

---
name: graph_visualization
description: Visualize graphs and highlight nodes, paths, subgraphs, and communities.
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    activates_on_intents: [visualize_graph, show_graph, highlight_nodes, highlight_path, highlight_subgraph, graph_visualization, display_graph, draw_graph, plot_graph, show_shortest_path, show_community, show_functional_group]
    any_tools: [visualize_subgraph]
    requires_graph_state: loaded
---

# Graph visualization

Use this skill when the user wants to display a graph or highlight particular nodes, paths, subgraphs, or communities.

## When to use
- Requests to visualize, draw, or show a graph.
- Highlighting particular nodes, such as key nodes or nodes on a shortest path.
- Highlighting paths, such as the shortest path from A to B.
- Highlighting subgraphs, such as ego graphs or community subgraphs.
- Highlighting communities from community detection results.
- Displaying molecular functional groups or key network structures.

## Procedure
1. **Confirm that a graph is loaded.** If not, ask the user to load graph data first.
2. **Call the visualization tool.** Use `visualize_subgraph` to obtain visualization data for the whole graph.
3. **Build the highlights parameter.** Translate the user's intent into `highlights` using the format below.
4. **Combine highlights.** Multiple types, such as shortest paths and communities, can specify `paths`, `nodes`, and `subgraphs` together.
5. **Community coloring.** Keep `include_community: true` by default, including when adding node, path, or subgraph highlights. Set it to `false` only if the user asks to disable community coloring.

## Unified highlights format (agent input)

```json
{
  "nodes": [
    {"ids": ["A", "F"], "label": "Key nodes"}
  ],
  "paths": [
    {"node_sequence": ["A", "C", "D", "B"], "label": "Shortest path A to B"}
  ],
  "subgraphs": [
    {"nodes": ["C1", "C2", "O"], "label": "Functional group"}
  ]
}
```

### Parameters

| Field | Type | Description |
|---|---|---|
| `nodes` | array | Node highlight groups |
| `nodes[].ids` | array | Node IDs to highlight |
| `nodes[].label` | string | Optional highlight label |
| `paths` | array | Path highlight groups |
| `paths[].node_sequence` | array | Ordered sequence of path nodes |
| `paths[].label` | string | Optional path label |
| `subgraphs` | array | Subgraph/community highlight groups |
| `subgraphs[].nodes` | array | Nodes in the subgraph |
| `subgraphs[].label` | string | Optional subgraph label |

### Highlight types and colors

| Type | Color | Description |
|---|---|---|
| `paths` | Yellow (#f59e0b) | Path highlights |
| `nodes` | Green (#10b981) | Node highlights |
| `subgraphs` | Purple (#8b5cf6) | Subgraph/community highlights |

## Verification
- The visualization panel opens automatically and displays the graph.
- Highlight buttons appear and toggle their corresponding highlights.
- Highlight types use distinct colors: paths (yellow), nodes (green), subgraphs (purple), and communities (pink).

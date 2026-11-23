---
name: graph_ml_inference
description: Prepare GraphML inference parameters, validate model_id, and interpret results.
metadata:
  version: 1.2.0
  domain: graph_ml
  risk_level: medium
  igraph:
    requires_tools: [infer]
    any_tools: [infer, visualize_model_architecture]
    requires_graph_state: loaded
    activates_on_intents: [graph_ml_infer]
---

# GraphML inference

## Required checks
- Prepare parameters from the `infer` tool schema.
- Verify that `model_id` is compatible with the task.
- Expand the complete `infer` parameter set from the schema where possible. Explicitly list every applicable field and its default, even if the user has not requested a change.

## Exact `query` formats
- Node classification: `{"nodes":["0","1","2"]}`, or use `split`.
- Link prediction for specified edges: `{"edges":[["0","8"],["0","1"],["1","344"]]}`. Each edge must be a two-element array of strings. Do not use objects such as `{"source":"0","target":"8"}`.
- Link prediction from a source node: `{"source_node":"0","candidate_nodes":["8","1"],"topk":10}`.
- Graph classification: `{"graph_indices":[0,1,2]}`. Indices must be integers.
- The MCP runtime may encode the entire `query` as a JSON string. This is allowed, but the decoded structure must still match the formats above.

## Procedure
1. Confirm graph state and the inference goal.
2. Build the complete `infer` parameter set from the user request and the exact formats above, including applicable defaults.
3. Call `infer`. Inference is read-only: execute after validating parameters without asking for confirmation again. Preserve the user's `query`; do not retry with an empty query after a representation error.
4. Explain the results and their limitations.

---
name: gnn_node_classification_training
description: Train GCN, GIN, APPNP, GATv2, Graph Transformer, GENConv, and other GNNs for node classification, with neighbor majority voting as an environment-failure fallback.
metadata:
  version: 1.1.0
  domain: graph_ml
  risk_level: medium
  igraph:
    any_tools: [get_graph_info, get_train_config, train, infer, get_node_info, tool_select]
    requires_graph_state: weighted
    activates_on_intents: [train GCN, graph neural network, node classification training, train GIN, train APPNP, train GAT, train GATv2, train Graph Transformer, train GENConv, train SAGE, train node_classification, gender prediction, infer, model_id]
---

# GNN node classification training with an environment-failure fallback

## When to use
Use when the user wants to train GCN, GIN, APPNP, GAT, GATv2, GraphSAGE, Graph Transformer, or GENConv for node classification. The graph must have feature vectors and node labels. Examples include user attribute prediction, such as gender or age group, and community role classification. If training is unavailable because of an environment issue, such as a missing torch installation, fall back to neighbor majority voting for approximate results.

## Procedure
1. **Check prerequisites.** Call `get_graph_info` and confirm:
   - `has_node_feat = true`
   - `has_node_label = true`
   - `graph_ml_readiness.node_classification.supported = true`
   - Record node count, edge count, feature dimension, labeled-node count, and label classes.
   - If any prerequisite fails, explain what is missing and stop.
2. **Get the training configuration.** Call `get_train_config(task=node_classification, model=GCN)`, or choose APPNP/GIN/GAT/GATV2/SAGE/TRANSFORMER/GEN. Retrieve the defaults, including:
   - `split` (train/validation/test ratios)
   - `epochs`, `lr`, `hidden_dim`, `num_layers`, and `dropout`
   - Show the template and allow the user to override any field.
3. **Start training.** Call `train`, for example:
   ```text
   {
     "task": "node_classification",
     "model": "GCN",
     "data": { "feature_source": "graph" },
     "train_config": { <parameters from step 2> },
     "algo_config": { <user settings or defaults> }
   }
   ```
   Call `train` only once; it returns an asynchronous `job_id`. Preserve explicitly requested settings such as `epochs` and `hidden_dim`. Do not drop them and retry on a parameter error, which could silently revert to defaults.
4. **Poll the job.** Use `get_job_status(job_id=<job_id>)` for that same job until it reaches `completed`, `failed`, or `cancelled`. Do not call `train` again during polling or call `cancel_job` after a terminal state.
5. **Handle the result.**

   **Training succeeded:**
   - Record the returned `model_id` and metrics.
   - Call `infer(model_id=<model_id>, query={nodes: [target node IDs]})` for each node's `pred_label` and `probabilities`.
   - Summarize and show the predictions.

   **Training failed because of the environment:**
   - Explain that the failure is a **server environment issue**, such as `No module named torch`, rather than a problem with the user's data.
   - Provide the complete reusable JSON configuration for retrying locally or in another environment.
   - **Fall back to neighbor majority voting:**
     1. Call `get_node_info` for each target's complete neighbor list.
     2. Batch calls to `get_node_info` for all neighbors and collect known labels.
     3. Count votes and report valid votes, total neighbors, votes per label, and agreement ratio.
     4. Present the winning label as an approximate prediction.
6. **Load tools dynamically.** If a required tool is absent from the current context, first call `tool_select(query=<tool capability>, top_k=4)`.
7. **Optional cross-check.** If GCN inference and neighbor voting agree, explain their corroboration and improved confidence. If they disagree, discuss possible causes such as sparse labels or heterogeneous structure and suggest further analysis.

## Tool sequences
**Normal:** `get_graph_info` → `get_train_config` → `train` (once) → `get_job_status` (same job) → `infer`

**Fallback:** `get_graph_info` → `get_train_config` → `train` (failure) → `get_node_info` (targets) → `get_node_info` (batched neighbors)

**Missing tool:** insert `tool_select` → `<required tool>`.

## Verification
- **Prerequisites:** `has_node_feat`, `has_node_label`, and `graph_ml_readiness.node_classification.supported` are all `true`.
- **Training quality:** `val_acc` should exceed the random baseline (`1 / number of classes`). If `best_epoch` is 1, flag possible data leakage or a configuration issue.
- **Inference completeness:** every target has `pred_label` and `probabilities`, with probabilities summing to approximately 1.0.
- **Fallback reliability:** a valid-vote/neighbor ratio ≥ 0.3 gives useful reference information. Below this threshold, flag sparse labeling and limited reliability.
- **Agreement:** explicitly label matching predictions from both methods as "high confidence".

## Notes
- `train` submits asynchronously. Submit once and poll the same returned `job_id`.
- MCP clients may serialize `data`, `train_config`, `algo_config`, `query`, and `infer_config` as JSON strings. The server supports both representations. Do not remove user parameters after a representation error.
- Distinguish environment failures from data failures so the user is not misled about graph quality.
- Neighbor voting has limited effectiveness with sparse graphs or very few labeled neighbors. State these limitations.
- GIN, APPNP, GAT, GATv2, GraphSAGE, Graph Transformer, and GENConv use the same calling pattern as GCN; change `model`. APPNP propagation is configurable with `appnp_k` and `appnp_alpha`; GAT/GATV2/TRANSFORMER attention heads use `gat_heads`.
- Undirected graphs naturally include neighbors in both directions. For directed graphs, consider both incoming and outgoing neighbors to improve coverage.

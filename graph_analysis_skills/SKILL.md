---
name: graph_virtual_ml
description: Deterministic virtual graph machine-learning train and inference actions for benchmark attempts.
---

# Virtual Graph Machine Learning

These actions preserve the benchmark's graph-ML interface without performing
real optimization, GPU training, network access, or LLM calls. A train action
validates the supplied arrays and returns a deterministic virtual model state.
An infer action returns deterministic predictions and, when the harness runs an
attempt-scoped broker, the expected result for that attempt. The broker keeps
that value in runner memory and records the actual action; it does not write the
answer into this tool or the agent workspace. Never edit this tool's source to
inject a result.

The only valid actions are:

- `structural_node_embedding_classification_train`
- `structural_node_embedding_classification_infer`
- `structural_node_embedding_link_prediction_train`
- `structural_node_embedding_link_prediction_infer`
- `structural_graph_embedding_classification_train`
- `structural_graph_embedding_classification_infer`

Call an action with named arguments. Values may be `.npy` files, `.json` files,
or inline JSON:

```bash
python {baseDir}/graph_ml_tools.py structural_node_embedding_classification_train --node_features x.npy --edge_index edge_index.npy --labels y.npy
python {baseDir}/graph_ml_tools.py structural_node_embedding_classification_infer --model_state model_state.json --node_features x.npy --edge_index edge_index.npy
```

Use the matching train action before its infer action, pass the returned model
state unchanged, and treat the single JSON document printed by the tool as the
authoritative result. A nonzero exit status means the call failed and its output
must not be used as evidence.

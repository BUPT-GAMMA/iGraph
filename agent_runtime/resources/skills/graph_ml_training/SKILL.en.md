---
name: graph_ml_training
description: Prepare GraphML training parameters from the tool schema and summarize metrics after training.
metadata:
  version: 1.1.0
  domain: graph_ml
  risk_level: medium
  igraph:
    requires_tools: [train]
    any_tools: [train, visualize_model_architecture]
    requires_graph_state: loaded
    activates_on_intents: [graph_ml_train]
---

# GraphML training

## Required checks
- Prepare parameters from the `train` tool schema.
- Expand the complete `train` parameter set from the schema where possible. Explicitly list every applicable field and its default, even if the user has not requested a change.

## Procedure
1. Confirm graph state and task type.
2. Build the complete `train` parameter set from the user request and tool schema, including applicable defaults.
3. Call `train` once. The user's training request authorizes this task. Do not ask for confirmation again during epochs, polling, or evaluation, and do not submit the same training job repeatedly. If validation fails, do not retry by dropping user-specified settings.
4. After `train` returns a `job_id`, call only `get_job_status` for that job until it reaches `completed`, `failed`, or `cancelled`. Do not call `cancel_job` on a finished job.
5. Summarize metrics, model/artifact IDs, and next steps.

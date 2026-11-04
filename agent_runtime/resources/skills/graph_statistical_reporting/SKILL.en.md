---
name: graph_statistical_reporting
description: Create graph analysis report artifacts from the user's analysis goal and factual tool results.
metadata:
  version: 1.0.0
  domain: reporting
  risk_level: low
  igraph:
    any_tools: [create_analysis_report]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis, graph_report, statistical_summary, structural_explanation, diagnostic_analysis, comparison_analysis]
---

# Graph analysis reports

## When to use
Use when the user wants an overview, statistical summary, structural explanation, pattern discovery, comparison, diagnosis, or report. Support the report with tool results, graph state, or data explicitly provided by the user.

## Procedure
1. Confirm the current graph state and the user's `analysis_goal`.
2. Call the analysis tools needed for that goal, such as `get_graph_info`, `degree_analysis`, `node_centrality`, `global_metrics`, `community_detection`, or other relevant graph analysis tools.
3. Collect the original tool responses as `source_results`, keyed by their actual tool names.
4. Call `create_analysis_report` with `report_intent`, `analysis_goal`, `title`, `source_results`, and `options`.
5. Give only 1–3 short concluding sentences in the final reply and direct the user to the report entry in the chat for the full report.

## Choosing `report_intent`
- `overview`: an overview, overall understanding, statistical summary, or quick introduction to the current graph.
- `analysis`: structural characteristics, pattern explanations, key objects, distributions, or general graph analysis.
- `diagnosis`: anomalies, bottlenecks, risks, weaknesses, problem localization, or causes.
- `comparison`: comparisons between nodes, subgraphs, groups, algorithm results, or states over time.
- `custom`: a specific report goal that none of the other intents accurately describes.

## Organizing `source_results`
- Keys must be actual tool names, such as `get_graph_info`, `degree_analysis`, `node_centrality`, `global_metrics`, or `community_detection`.
- Values must be the corresponding original tool responses. Do not rewrite them or invent fields.
- Prefer to include `get_graph_info` or equivalent basic graph information for the title, summary, and scale context.
- Include only reliable results already obtained and directly relevant to the user's goal.
- Charts, metrics, and insights must be traceable to `source_results`, graph state, or data explicitly provided by the user.

## Recommended `options`
- Default to `top_k: 10`, `max_sections: 6`, and `include_narrative: true`.
- Reduce `top_k` or `max_sections` for a concise, quick, or highlights-only report.
- Increase detail when requested, within the limits of the tool schema.
- Use `include_narrative: false` when the user wants only factual tables or charts.

## Lightweight answers
Keep requests for a single node, edge, path, metric, or an explicitly brief answer as short text replies.

## Failure handling
- If `create_analysis_report` is unavailable, do not invent a report link. Summarize the obtained results in ordinary text.
- If report generation fails or facts are insufficient, explain the direct reason and retain the conclusions supported by the available evidence.
- A report failure does not invalidate completed graph analysis. Still provide a readable, traceable answer in the chat.

## Combining reports and visualization
If the analysis includes paths, key nodes, local structures, communities/groups, or other useful visual objects, also call `visualize_subgraph` to create a graph artifact. Report and graph artifacts can coexist.

## Verification
- Every metric, chart, and insight is traceable to tool results, graph state, or user-provided data.
- Keep only the summary and report-viewing guidance in chat. Do not repeat complete tables, chart data, or report JSON.

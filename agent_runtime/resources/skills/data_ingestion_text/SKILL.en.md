---
name: data_ingestion_text
description: Extract relationships from text pasted by the user or other supported documents; route SQL/CSV/TXT uploads to the graph_construction dataset workflow.
metadata:
  version: 3.0.0
  domain: data_ingestion
  risk_level: medium
  igraph:
    requires_tools: [load_graph_from_upload, load_graph_from_text]
    any_tools: [load_graph_from_triples, get_graph_info, job_status, job_result, search_node]
    requires_graph_state: none
    activates_on_intents: [data_ingestion, graph_rag, knowledge_extraction]
---

# Text data ingestion

SQL, CSV, and TXT uploads belong to the dataset construction workflow. Read `graph_construction` first. For entities and relationships in uploaded plain TXT, use its explicit `txt_mode=entities`; do not copy the file contents into inline text tools.
Reuse the saved graph of a ready dataset. Clearly state when a construction algorithm is not integrated. Do not use the legacy upload workflow below or copy source files into inline text/triple tools to bypass this limitation.

## When to use

| User input | Recommended tool |
|---|---|
| SQL, CSV, TXT uploads | `graph_construction`: check dataset status; choose `entities` for semantic extraction from plain TXT |
| Other supported uploads | `load_graph_from_upload`: automatic format detection |
| Inline text pasted directly by the user | `load_graph_from_text(text=...)` |
| Triples provided directly by the user (JSON / manual entry) | `load_graph_from_triples` |
| Other supported structured tables | The data_ingestion_table skill |

**Core rule: use dataset construction for SQL/CSV/TXT. The `entities` mode sends all plain TXT content to the construction LLM in chunks; inform the user and explain that human review is required. Use `load_graph_from_upload` only for other supported uploads, without copying file contents into `text`.**

## Step 1 — Uploaded files
After confirming that the source is not SQL/CSV/TXT, call `load_graph_from_upload(filename=...)`.

The tool runs an LLM precheck (FileFormatPreCheckSubAgent) to detect:
- **binary / JSON** (.npz / .pt / .json): direct structured loading, synchronous response.
- **edge_list** (src dst [weight] per line): direct parsing, synchronous response.
- **triple** (source relation target per line): direct parsing, synchronous response.
- **freetext** (prose, articles, documents): the precheck also infers `extraction_goal`, submits an asynchronous text2graph job, and returns `job_id`.

If the LLM precheck is unavailable, the tool falls back to heuristic format detection without inferring `extraction_goal`.
If no file has been uploaded, ask the user to upload it before proceeding.

## Step 2 — Handle the response

### Synchronous formats (binary / edge_list / triple)
After success, call `get_graph_info` and report the size and structure summary.

### Asynchronous format (freetext)
Example response:
```json
{ "async": true, "job_id": "xxx", "status": "queued", "format_detected": "freetext" }
```
Poll until completion:
```text
job_status(job_id="xxx")   -> Check progress (queued / running / done / failed)
job_result(job_id="xxx")   -> Fetch the complete result after completion
```
A 2–3 second polling interval and a timeout of roughly 5 minutes are suggested for large files. Call `get_graph_info` after completion as well.

## Step 3 — Inline text
Use `load_graph_from_text` only for text pasted directly by the user:
```text
load_graph_from_text(
    text="<text pasted by the user>",
    extraction_goal="Extract only investment and employment relationships between companies and people",
    directed=true,
    append=false
)
```
`extraction_goal` is the most important quality-control parameter. Omitting it uses general extraction, which may introduce more noise.

| Scenario | Example extraction_goal |
|---|---|
| Corporate relationships | "Extract only investment, employment, and merger relationships between companies and people" |
| Research knowledge graph | "Extract only usage and comparison relationships between methods, datasets, and evaluation metrics" |
| Supply chain | "Extract supply and production relationships between suppliers, products, and factories" |
| Event timeline | "Extract only event nodes and temporal ordering; ignore descriptions of people" |
| General / uncertain | Leave blank, but explain that noise may be higher |

If the goal is unclear, **ask which entity and relationship types matter** before calling the tool.

## Step 4 — Merge mode
**append** (default `false`):
- First graph: use `false` to replace the current session graph.
- Add documents to an existing graph: `append=true` merges and deduplicates without deleting existing nodes.
- Constructed property graphs do not support appending. The tool returns `UNSUPPORTED_PROPERTY_GRAPH_APPEND` and preserves the graph. Explain the limitation. Replacing or clearing it requires an explicit user request; do not automatically retry with `append=false`.

## Step 5 — Interpret results and assess quality

| Field | Meaning | Quality signal |
|---|---|---|
| `chunks_processed` | Number of processed chunks | Should be > 1 for long text |
| `raw_triple_count` | Raw extracted triples | — |
| `deduped_triple_count` | Triples after deduplication | Much lower than raw suggests repeated descriptions |
| `skipped_count` | Skipped triples | Consistently high values suggest noise or an overly broad extraction_goal |
| `num_nodes` / `num_edges` | Final graph size | — |
| `sample_triples` | Sample triples | **Must be inspected** for semantic accuracy |
| `warnings` | Extractor warnings | Explain warnings to the user |
| `format_detected` | Detected format | Tell the user how the file was classified |

**Reference thresholds**
- `deduped / raw < 0.3`: high noise; suggest narrowing `extraction_goal` and retrying.
- `skipped / raw > 0.5`: high format-matching failure rate, possibly caused by mixed languages or unusual structure.
- `num_nodes < 3`: extraction may have failed or the text may contain too little information; ask whether the user can provide more text.

## Step 6 — Failure handling

### Empty extraction (num_edges = 0)
1. Check whether the text contains extractable relationships; purely descriptive text may not.
2. Try a more specific `extraction_goal` for inline text. The upload path infers it during precheck.
3. If the content is a table or code, use `load_graph_from_triples` or another appropriate skill.

### Clearly incorrect sample semantics
1. Show the incorrect sample and explain the problem.
2. Suggest refining `extraction_goal`.
3. Run extraction again with `append=false` to replace the result.

### Asynchronous job failure (status: failed)
- Call `job_result` for error details.
- Text too long: suggest splitting it into batches and using `append=true` for each additional batch.
- LLM error: tell the user to retry later.

### Incorrect format detection
If `format_detected` does not match the actual content, such as an edge list classified as freetext, explain the issue and suggest converting to a standard edge-list format before uploading again.

### Correcting individual triples
Guide the user to `load_graph_from_triples` to manually add or replace specific relationships.

## Step 7 — Verify and report
After success, **always**:
1. State `format_detected` and explain the detected format.
2. Report `chunks_processed`, `raw_triple_count`, `deduped_triple_count`, `num_nodes`, and `num_edges`.
3. Show 2–3 `sample_triples` so the user can check the semantics.
4. Explain each warning, if present.
5. If `append=true`, describe the change in total graph size after merging.

Example report:
> Detected format: {format}. Processed {chunks} chunks, extracted {raw} raw triples, and retained {dedup} after deduplication. The graph contains {nodes} nodes and {edges} edges.
> Sample relationships: {source} —[{relation}]→ {target}…
> {Explain any warnings.}

# iGraph Documentation

Documentation is organized by purpose. Add new documents to the appropriate directory; place completed plans, one-off verification reports, and reviews in `archive/`.

## Start here

- [Technical report](https://igraph-docs.graphagent.workers.dev): the Graph Data Agent, its workflow, system design, and graph capabilities. English and Chinese editions are available online.
- [Deployment guide](guides/DEPLOYMENT.md): the complete English guide to installing and operating a single-user iGraph workspace.
- [Built-in experiences](../README.md#-built-in-experiences): Financial Risk Control, AI for Science, and Relationship Insights, with prepared graphs and example questions.
- [Current architecture](architecture/AGENTOS_FOUR_MODULE_REFACTOR.md): the four application modules and their responsibilities.

This navigation page is in English. Individual implementation notes and historical documents may retain their original language.

> Start with [`architecture/AGENTOS_FOUR_MODULE_REFACTOR.md`](architecture/AGENTOS_FOUR_MODULE_REFACTOR.md) for the current architecture.
> Documents in `legacy/` describe the earlier in-process runtime. They are retained for behavioral comparison and are not current startup instructions.

## guides/ — Usage, testing, and deployment

- [TESTING.md](guides/TESTING.md): local backend regressions, frontend builds, CI scope, coverage analysis, and pull-request validation.
- [DEPLOYMENT.md](guides/DEPLOYMENT.md): the English deployment guide covering Linux setup, Docker Compose, model configuration, optional GPU support, backups, and troubleshooting.
- [DEPLOYMENT.zh-CN.md](guides/DEPLOYMENT.zh-CN.md): the Chinese deployment reference, including hardware sizing, rootless Docker, registry mirrors, GPU setup, backups, and troubleshooting.
- [MANUAL_DEPLOYMENT.md](guides/MANUAL_DEPLOYMENT.md): manual deployment steps and checks for `main`.
- [SINGLE_USER_DEPLOYMENT.zh-CN.txt](guides/SINGLE_USER_DEPLOYMENT.zh-CN.txt): the Chinese single-user server deployment and operations reference.
- [MODEL_PROVIDERS.md](guides/MODEL_PROVIDERS.md): model connections and provider management.
- [NEBULAGRAPH.md](guides/NEBULAGRAPH.md): NPZ/GraphData and NebulaGraph hybrid storage, configuration, synchronization tools, native queries, and validation.
- [GRAPH_TOOLS_DEVELOPMENT_GUIDE.md](guides/GRAPH_TOOLS_DEVELOPMENT_GUIDE.md): graph-tool and algorithm interfaces, conventions, and development guidance.
- [datasets/](guides/datasets/): dataset-loading guides, search indexes, and quick references.

## architecture/ — Current architecture and runtime behavior

- [AGENTOS_FOUR_MODULE_REFACTOR.md](architecture/AGENTOS_FOUR_MODULE_REFACTOR.md): **the primary architecture document** covering module responsibilities, AgentOS mappings, migration stages, and dependency direction.
- [TOOL_DAG_EXECUTION.md](architecture/TOOL_DAG_EXECUTION.md): tool resource catalogs, DAG scheduling, background jobs, and concurrency settings.
- [STREAM_RECOVERY.md](architecture/STREAM_RECOVERY.md): stream recovery, complete snapshots, approval safety, dependency contracts, failure tests, and troubleshooting.
- [RUN_CANCELLATION.md](architecture/RUN_CANCELLATION.md): task cancellation, job isolation, persistent control, failure validation, and remaining release checks.
- [AGENT_TOOLS.md](architecture/AGENT_TOOLS.md): the agent's available tools.
- [GRAPH_DATA_JSON_FORMAT.md](architecture/GRAPH_DATA_JSON_FORMAT.md): the `graph_data.v1` JSON storage and service-exchange format.
- [GRAPH_ML_SERVICE.md](architecture/GRAPH_ML_SERVICE.md): GraphData's GraphML module and training/inference parameters.
- [SHORT_TERM_MEMORY.md](architecture/SHORT_TERM_MEMORY.md): short-term context memory for long conversations.

## features/ — Feature documentation

- [GRAPH_CONSTRUCTION.md](features/GRAPH_CONSTRUCTION.md): construction interfaces, dataset reuse, optimization previews, and saving results.
- [TABULAR_CONSTRUCTION_SUBAGENT.md](features/TABULAR_CONSTRUCTION_SUBAGENT.md): the design and use of the tabular graph-construction subagent.
- [GRAPH_WORKSPACE_FEATURES.md](features/GRAPH_WORKSPACE_FEATURES.md): graph construction, exploration, and project-data reuse.
- [SOURCE_DATA_PREVIEW.md](features/SOURCE_DATA_PREVIEW.md): previewing source data before construction.
- [PERSONAL_DATA_AND_PROJECT_DELETION.md](features/PERSONAL_DATA_AND_PROJECT_DELETION.md): standalone conversations, personal datasets, and project deletion.
- [USER_CASES.md](features/USER_CASES.md): bundled graphs and example questions.
- [USER_CASE_SOURCES.md](features/USER_CASE_SOURCES.md): construction sources for the bundled examples.
- [FINANCE_WORKFLOW_IMPLEMENTATION_2026-09-26.md](features/FINANCE_WORKFLOW_IMPLEMENTATION_2026-09-26.md): implementation of the financial ownership workflow.
- [DATASET_API_MIGRATION.md](features/DATASET_API_MIGRATION.md): dataset API and client migration.
- [model-management-plan.md](features/model-management-plan.md): model management and session switching.
- [graph-analysis-and-gnn-upgrade.md](features/graph-analysis-and-gnn-upgrade.md): graph-analysis operators and modern GNN upgrades.
- [POPUP_MOTION.md](features/POPUP_MOTION.md): popup-window animations.

## Other directories

- [adr/](adr/): architecture decision records.
- [research/](research/): technical investigations.
- [legacy/](legacy/): the pre-refactor baseline (`DESIGN.md`, `RUN_AND_TEST.md`, `DEPLOY_SERVERS.md`, `SKILLS_MEMORY.md`, and `REFACTOR_BASELINE.md`), retained for behavioral comparison.
- [archive/](archive/): completed plans (`plans/`, `superpowers/`), verification and measurement reports (`reports/`), code reviews (`reviews/`), demo outlines (`demos/`), and dated acceptance records (`testing/`). These are historical references, not current implementation entry points.

## Pre-refactor baseline

> The following entries describe the earlier in-process runtime, not the current default implementation.
> For current Memory, Learning, Session Context, DecisionLog, and Skills behavior, use the AgentOS four-module architecture document.

- **GraphML within GraphData:** the separate `graph_ml_service/` project was merged into `graph_data_service/graph_ml/`. It required no separate deployment and accessed graph data in session memory directly.
- **Two ProtocolDriver modes:** `LLM_TOOL_PROTOCOL=text|native` selected text-based tool calls or native function calling without changing the frontend interface.
- **ProtocolDriver within ContextManager:** the `context/` package managed context assembly and streaming, invoked through `context_manager.stream_turn()`.
- **StatefulStreamParser:** combined stream parsing and result extraction, replacing the earlier stream → normalize → parse chain. Parsing failures retried up to `_MAX_PROTOCOL_RETRIES` times.
- **Module organization:** `context/` (protocol handling), `memory/` (memory engine), `skills/` (progressive disclosure), and `tools/` (LLM-based selection) became separate packages. `model_registry.py` centralized model names.
- **Planning modes:** `never`, `auto`, and `always` could be changed through `PATCH /api/sessions/{id}/planning-mode`.
- **Dataset APIs:** introduced `GET /api/datasets`, `POST /api/sessions/{id}/load-dataset`, and `POST /api/sessions/{id}/save-dataset`.
- **Central tool catalog:** `graph_data_service/tool_catalog.py` defined tool specifications; the MCP server registered them dynamically instead of hard-coding them.
- **Lazy agent creation:** `activate_session` read metadata only. The agent was created on the first `chat_stream`, and switching between historical sessions no longer destroyed other agents.
- **Memory architecture:** introduced `MemoryManager` and `MemoryProvider`, a separate `user_profile.json`, field-level priority truncation, frozen snapshots for prefix-cache stability, and the `memory_read` / `memory_delete` virtual tools.
- **Embedding removal in the legacy runtime:** removed `backend/agent/embedding.py`. `LLMToolSelector` used an LLM subagent instead of vector-based tool selection, and memory loaded complete frozen snapshots instead of semantic retrieval. This removed the sentence-transformers dependency while retaining Torch for GraphML training and inference.
- **Progressive skill disclosure:** `SKILL.md` used YAML frontmatter. A compact index remained in context, with full text loaded through `skill_view(name)`. Variables could come from `~/.igraph/config.yaml`.
- **Memory safeguards:** `safety.py` checked memory, playbooks, and skills for injection patterns. Writes used atomic replacement (`temp` + `fsync` + `os.replace`) and migration markers for idempotency.
- **Per-turn tool-selection cache:** `LLMToolSelector.set_turn_id()` reset at each user turn. Multiple reasoning/action cycles in the same turn reused the decision; fallback used keyword matching instead of loading all tools.
- **Plan-aware agent:** QueryAnalyzer and Planner produced structured plans for complex tasks and executed them step by step through `_run_plan_solver` and `solve_*` events.
- **Algorithm expansion:** increased the graph-algorithm set from 19 to more than 50 and added an Algorithm Catalog knowledge layer.
- **Dataset platform:** added a Dataset Registry with CRUD and versioning, table/text-to-graph pipelines, and lightweight RAG.
- **Persistent graph snapshots:** GraphSession supported snapshot writes, restoration, and undo, with automatic graph restoration after service restarts.
- **NebulaGraph hybrid storage:** NPZ/GraphData continued to handle snapshots and GraphML. Graphs could be synchronized explicitly and asynchronously to NebulaGraph, using fixed `FETCH PROP`, `GO`, and `FIND SHORTEST PATH` tools for native queries.
- **Entity search:** `search_node` and `search_edge` supported exact names, fuzzy semantic matching, and type/degree/weight filters using versioned index caches. Search could precede path and subgraph analysis.
- **Graph construction from triples:** `build_graph_from_triples` mapped structured triples to graphs, using relation as edge type, evidence as edge text, and confidence as an edge feature.
- **LLM-assisted text extraction:** the `TextGraphExtractor` subagent in `text_graph_extractor.py` supported `build_graph_from_text`, long-text chunking, per-chunk extraction, triple deduplication, and confidence filtering.
- **Playbook deduplication:** `PlaybookDedup` in `playbook_dedup.py` periodically detected semantically duplicated playbooks and proposed merging or archiving them.

## Current entry points

- Backend: `python -m backend` (`python agent_server.py` is a compatibility entry point).
- Agent Runtime: `python -m agent_runtime`.
- Graph Engine HTTP: `python -m graph_engine api`.
- Graph Engine MCP: `python -m graph_engine mcp http`.

## Get started

Follow the [English deployment guide](guides/DEPLOYMENT.md) for configuration, startup, model connections, and validation. It includes server access through an SSH tunnel, optional GPU setup, backups, and troubleshooting.

After deployment, open the workspace and choose a built-in Financial Risk Control, AI for Science, or Relationship Insights example, or upload your own data.

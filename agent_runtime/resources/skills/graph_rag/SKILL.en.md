---
name: graph_rag
description: Chunk and retrieve documents, answer questions with graph knowledge, and respect citation boundaries.
metadata:
  version: 1.0.0
  domain: graph_rag
  risk_level: medium
  igraph:
    any_tools: [rag_query, load_graph_from_text]
    requires_graph_state: none
    activates_on_intents: [graph_rag]
---

# Graph RAG

## When to use
Use for document-based graph questions, relationship lookup, or evidence-supported answers.

## Procedure
1. Clarify whether the answer should rely on uploaded documents, graph structure, or both.
2. Retrieve relevant evidence before answering factual questions.
3. Distinguish conclusions derived from the graph from the original document text.
4. Explain the limits of the evidence and any missing context.

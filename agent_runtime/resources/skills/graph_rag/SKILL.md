---
name: graph_rag
description: 文档分块、检索、图知识辅助问答和引用边界流程。
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

## 适用场景
用于基于文档的图问答、关系查找或有证据支撑的回答。

## 操作步骤
1. 明确回答应依赖上传文档、图结构还是两者结合。
2. 回答事实性问题前先检索相关证据。
3. 区分图推导的结论与文档原文。
4. 说明证据边界和缺失的上下文。

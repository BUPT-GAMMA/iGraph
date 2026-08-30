"""Agno LearningMachine assembly.

Mapping from the former implementation:
  user_profile -> UserProfile
  long_term    -> UserMemory
  short-term session summaries/plans -> SessionContext
  playbooks    -> user-scoped LearnedKnowledge in PROPOSE mode
  important tool decisions -> DecisionLog
"""

from __future__ import annotations

from agno.knowledge import Knowledge
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.learn import (
    LearnedKnowledgeConfig,
    LearningMachine,
    LearningMode,
    SessionContextConfig,
    UserMemoryConfig,
    UserProfileConfig,
)
from agno.vectordb.pgvector import PgVector, SearchType

from agent_runtime.config import AgentRuntimeConfig


def create_learning_machine(
    config: AgentRuntimeConfig,
    *,
    planning_mode: str,
) -> LearningMachine:
    learned_knowledge: LearnedKnowledgeConfig | bool = False
    knowledge: Knowledge | None = None

    if config.embedding_model and config.embedding_api_key:
        knowledge = Knowledge(
            vector_db=PgVector(
                db_url=config.database_url,
                table_name="igraph_learned_knowledge",
                search_type=SearchType.hybrid,
                embedder=OpenAIEmbedder(
                    id=config.embedding_model,
                    api_key=config.embedding_api_key,
                    base_url=config.embedding_base_url or None,
                ),
            )
        )
        learned_knowledge = LearnedKnowledgeConfig(
            mode=LearningMode.PROPOSE,
            namespace="user",
        )

    return LearningMachine(
        knowledge=knowledge,
        user_profile=UserProfileConfig(mode=LearningMode.ALWAYS),
        user_memory=UserMemoryConfig(mode=LearningMode.AGENTIC),
        session_context=SessionContextConfig(enable_planning=planning_mode != "never"),
        learned_knowledge=learned_knowledge,
        decision_log=True,
    )


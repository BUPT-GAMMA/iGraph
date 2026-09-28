"""Agno LearningMachine assembly.

Mapping from the former implementation:
  user_profile -> UserProfile
  long_term    -> UserMemory
  short-term session summaries/plans -> SessionContext
  playbooks    -> user-scoped LearnedKnowledge in PROPOSE mode
  important tool decisions -> DecisionLog
"""

from __future__ import annotations

from agno.exceptions import RunCancelledException
from agno.knowledge import Knowledge
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agent_runtime.cancellation_compat import CancellableEmbedder, PostHookRunCancelled
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
from execution_protocol import controls_enabled
from agent_runtime.execution_scope import current_scope


class CancellableLearningMachine(LearningMachine):
    async def aprocess(self, *args, **kwargs):
        scope = current_scope.get()
        if scope is None:
            return await super().aprocess(*args, **kwargs)
        if scope.stopped.is_set():
            return  # A stopped partial conversation starts no new extraction.
        return await scope.wait(super().aprocess(*args, **kwargs))


class CompletedTurnLearningMachine(CancellableLearningMachine):
    """Capture only after the native response and its guardrails finish.

    Agno 2.9 starts automatic extraction before the model responds and snapshots
    the message list at that point. A current refusal is therefore absent from
    the summary used by the next turn. The factory's awaited post-hook is the
    sole capture entry point; native early/background calls do no extraction.
    """
    def process(self, *args, **kwargs):
        return None

    async def aprocess(self, *args, **kwargs):
        return None

    async def acapture_completed_turn(self, **kwargs):
        try:
            return await super().aprocess(**kwargs)
        except RunCancelledException as exc:
            raise PostHookRunCancelled(str(exc)) from exc


async def completed_turn_learning_hook(agent, run_output, session, user_id=None, run_context=None):
    machine = getattr(agent, "_learning", None)
    if not isinstance(machine, CompletedTurnLearningMachine):
        return
    status = str(getattr(run_output.status, "value", run_output.status)).lower()
    if status in {"paused", "cancelled", "error", "regenerated"}:
        return
    scope = current_scope.get()
    if scope is not None and scope.stopped.is_set():
        raise PostHookRunCancelled("用户终止了当前任务")
    messages = list(run_output.messages or [])
    if not messages or messages[-1].role != "assistant" or messages[-1].tool_calls:
        return
    await machine.acapture_completed_turn(
        messages=messages,
        user_id=user_id,
        session_id=session.session_id,
        agent_id=agent.id,
        team_id=agent.team_id,
        run_metrics=run_output.metrics,
        run_context=run_context,
        metadata=getattr(run_context, "metadata", None),
        dependencies=getattr(run_context, "dependencies", None),
        session_state=getattr(run_context, "session_state", None),
    )


class CancellablePgVector(PgVector):
    async def async_insert(self, *args, **kwargs):
        scope = current_scope.get()
        operation = super().async_insert(*args, **kwargs)
        return await scope.wait(operation) if scope else await operation

    async def async_upsert(self, *args, **kwargs):
        scope = current_scope.get()
        operation = super().async_upsert(*args, **kwargs)
        return await scope.wait(operation) if scope else await operation


def create_learning_machine(
    config: AgentRuntimeConfig,
    *,
    planning_mode: str,
) -> LearningMachine:
    learned_knowledge: LearnedKnowledgeConfig | bool = False
    knowledge: Knowledge | None = None

    if config.embedding_model and config.embedding_api_key:
        knowledge = Knowledge(
            vector_db=(CancellablePgVector if controls_enabled() else PgVector)(
                db_url=config.database_url,
                table_name="igraph_learned_knowledge",
                search_type=SearchType.hybrid,
                embedder=(CancellableEmbedder if controls_enabled() else OpenAIEmbedder)(
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

    return CompletedTurnLearningMachine(
        knowledge=knowledge,
        user_profile=UserProfileConfig(mode=LearningMode.ALWAYS),
        user_memory=UserMemoryConfig(mode=LearningMode.AGENTIC),
        session_context=SessionContextConfig(enable_planning=planning_mode != "never"),
        learned_knowledge=learned_knowledge,
        decision_log=True,
    )

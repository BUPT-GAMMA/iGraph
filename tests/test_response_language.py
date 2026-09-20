from __future__ import annotations

from agent_runtime.agent.context.context_manager import (
    ContextManager,
    build_role_system_prompt,
)
from agent_runtime.instructions import instructions_for
from response_language import (
    AUTO_LANGUAGE,
    build_turn_language_instruction,
    resolve_response_language,
)


def test_resolves_clear_english_and_chinese_turns() -> None:
    assert resolve_response_language("Please analyse the graph.") == "English"
    assert resolve_response_language("请分析一下这个图。") == "Chinese"


def test_explicit_language_request_overrides_message_language() -> None:
    assert resolve_response_language("请用英文回答，分析这个图。") == "English"
    assert resolve_response_language("Please answer in Chinese and analyse the graph.") == "Chinese"


def test_resolves_common_non_english_languages_without_ui_locale() -> None:
    assert resolve_response_language("Por favor analiza este grafo.") == "Spanish"
    assert resolve_response_language("このグラフを分析してください。") == "Japanese"
    assert resolve_response_language("이 그래프를 분석해 주세요.") == "Korean"


def test_ambiguous_input_defers_to_model_without_guessing_english() -> None:
    assert resolve_response_language("Cora GCN") == AUTO_LANGUAGE


def test_leading_request_language_wins_over_pasted_source_text() -> None:
    message = "Please summarize this document:\n" + "这是一段中文资料。" * 30
    assert resolve_response_language(message) == "English"


def test_instruction_locks_language_across_the_whole_run() -> None:
    instruction = build_turn_language_instruction("English")
    assert "Use English for this turn" in instruction
    assert "entire run" in instruction
    assert "tool-call cycle" in instruction
    assert "UI locale" in instruction


def test_agent_instructions_no_longer_force_chinese() -> None:
    text = "\n".join(
        instructions_for(
            reasoning_mode="direct",
            planning_mode="auto",
            response_language="English",
        )
    )
    assert "最终回答使用中文" not in text
    assert "Use English for this turn" in text


def test_legacy_prompts_no_longer_force_chinese() -> None:
    for protocol in ("text", "native"):
        prompt = build_role_system_prompt("direct", protocol=protocol)
        assert "最终答复必须使用中文" not in prompt
        assert "中文答复" not in prompt


def test_legacy_context_keeps_turn_language_during_chinese_internal_steps() -> None:
    captured: dict[str, str] = {}

    class FakeDriver:
        def build_model_messages(self, working_messages, tool_specs, **kwargs):
            captured.update(kwargs)
            return working_messages

    context = ContextManager(session_id="language-lock")
    context._driver = FakeDriver()
    context.lock_response_language("Please analyse the graph.")

    context.build_model_messages([], query="请汇总规划步骤与工具结果。")

    assert "Use English for this turn" in captured["language_prompt"]

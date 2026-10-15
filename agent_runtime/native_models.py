"""Thin lifecycle wrappers around Agno's native provider implementations."""
from agno.models.anthropic import Claude
from agno.models.google import Gemini
from agno.models.openai.responses import OpenAIResponses
from agent_runtime.cancellation_compat import CancellableModelMixin, track_stream_method
from agent_runtime.model_history import ModelHistoryMixin
from agent_runtime.tool_dag import DAGModelMixin


class NativeClaude(ModelHistoryMixin, DAGModelMixin, CancellableModelMixin, Claude):
    # Claude's SDK stream context manager already owns and closes the response.
    pass


class NativeGemini(ModelHistoryMixin, DAGModelMixin, CancellableModelMixin, Gemini):
    def _format_messages(self, messages, *args, **kwargs):
        formatted, system = super()._format_messages(messages, *args, **kwargs)
        # Agno 2.9.0 keeps only the last system/developer message for Gemini.
        # Preserve every instruction, including request-scoped language reminders.
        system = '\n\n'.join(message.get_content_string() for message in messages
                             if message.role in {'system', 'developer'} and message.content) or system
        return formatted, system

    def get_client(self):
        client = super().get_client()
        track_stream_method(client.aio.models, 'generate_content_stream', always=True)
        return client


class NativeResponses(ModelHistoryMixin, DAGModelMixin, CancellableModelMixin, OpenAIResponses):
    def get_async_client(self):
        client = super().get_async_client()
        track_stream_method(client.responses, 'create')
        return client

    def _format_messages(self, messages, *args, **kwargs):
        formatted = super()._format_messages(messages, *args, **kwargs)
        # Agno 2.9.0 omits encrypted reasoning when its message also has tool
        # calls. Stateless Responses must replay that item before the call.
        for message in messages:
            reasoning = (message.provider_data or {}).get('reasoning_output')
            if reasoning and message.tool_calls:
                first_id = message.tool_calls[0]['id']
                index = next((i for i, item in enumerate(formatted) if isinstance(item, dict)
                              and item.get('type') == 'function_call' and item.get('id') == first_id), None)
                if index is not None:
                    formatted.insert(index, reasoning)
        return formatted

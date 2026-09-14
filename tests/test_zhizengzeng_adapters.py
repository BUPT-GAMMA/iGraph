from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

from scripts.zhizengzeng_config import load_codex_zhizengzeng_config
from scripts import zhizengzeng_judge_adapter as judge_adapter
from scripts.zhizengzeng_judge_adapter import _request, _score, _validate_envelope


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


class _SequenceOpener:
    def __init__(self, *responses: bytes | Exception) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout: float):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)


def _provider_response(content: str | None) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": content}}]},
        separators=(",", ":"),
    ).encode("utf-8")


def _judge_envelope() -> dict[str, object]:
    return {
        "schema": "igraph.benchmark.judge_task.v1",
        "original_question": "Which node is central?",
        "ground_truth": "node 4",
        "final_answer": "Node 4.",
        "rubric": "Return a binary score.",
    }


class ZhizengzengAdapterTests(unittest.TestCase):
    def test_static_codex_profile_is_loaded_without_shell_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "run.sh"
            profile.write_text(
                '''if [ "$AGENT" = "openclaw" ]; then
API_URL="https://example.invalid/v1"
API_KEY="not-used"
elif [ "$AGENT" = "codex" ]; then
MODEL="old-model"
API_URL="https://provider.example.invalid/v1"
API_KEY="test-key"
else
API_URL="https://example.invalid/v1"
API_KEY="not-used"
fi
''',
                encoding="utf-8",
            )
            config = load_codex_zhizengzeng_config(profile)

        self.assertEqual(config.base_url, "https://provider.example.invalid/v1")
        self.assertEqual(config.api_key, "test-key")

    def test_non_https_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "run.sh"
            profile.write_text(
                '''elif [ "$AGENT" = "codex" ]; then
API_URL="http://provider.example.invalid/v1"
API_KEY="test-key"
else
true
fi
''',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_codex_zhizengzeng_config(profile)

    def test_provider_hostname_can_be_restricted_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "run.sh"
            profile.write_text(
                '''elif [ "$AGENT" = "codex" ]; then
API_URL="https://provider.example.invalid/v1"
API_KEY="test-key"
fi
''',
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"IGRAPH_PROVIDER_ALLOWED_HOSTS": "different.example.invalid"},
            ):
                with self.assertRaisesRegex(ValueError, "ALLOWED_HOSTS"):
                    load_codex_zhizengzeng_config(profile)

    def test_judge_score_parser_accepts_only_binary_results(self) -> None:
        self.assertEqual(_score('{"score":1}'), 1)
        self.assertEqual(_score('```json\n{"score": 0}\n```'), 0)
        with self.assertRaises(ValueError):
            _score('{"score": 0.5}')
        with self.assertRaises(ValueError):
            _score('Example {"score":0}; final {"score":1}')

    def test_judge_envelope_requires_the_benchmark_schema(self) -> None:
        envelope = _judge_envelope()
        self.assertEqual(_validate_envelope(envelope), envelope)
        with self.assertRaisesRegex(ValueError, "schema"):
            _validate_envelope({**envelope, "schema": "unexpected"})

    def test_judge_uses_minimal_reasoning_with_modern_token_parameter(self) -> None:
        opener = _SequenceOpener(_provider_response('{"score":1}'))
        with mock.patch.object(judge_adapter, "_OPENER", opener):
            score = _request(
                base_url="https://provider.example.invalid/v1",
                api_key="test-key",
                model="gpt-5-mini",
                envelope=_judge_envelope(),
                timeout=10.0,
                max_completion_tokens=512,
            )

        self.assertEqual(score, 1)
        payload = json.loads(opener.requests[0][0].data)
        self.assertEqual(payload["reasoning_effort"], "minimal")
        self.assertEqual(payload["max_completion_tokens"], 512)
        self.assertNotIn("max_tokens", payload)

    def test_judge_retries_one_empty_response(self) -> None:
        opener = _SequenceOpener(b"", _provider_response('{"score":0}'))
        with mock.patch.object(judge_adapter, "_OPENER", opener):
            score = _request(
                base_url="https://provider.example.invalid/v1",
                api_key="test-key",
                model="gpt-5-mini",
                envelope=_judge_envelope(),
                timeout=10.0,
                max_completion_tokens=512,
            )

        self.assertEqual(score, 0)
        self.assertEqual(len(opener.requests), 2)

    def test_judge_empty_content_retry_is_bounded(self) -> None:
        opener = _SequenceOpener(_provider_response(None), _provider_response("  "))
        with mock.patch.object(judge_adapter, "_OPENER", opener):
            with self.assertRaisesRegex(RuntimeError, "repeatedly returned an empty"):
                _request(
                    base_url="https://provider.example.invalid/v1",
                    api_key="test-key",
                    model="gpt-5-mini",
                    envelope=_judge_envelope(),
                    timeout=10.0,
                    max_completion_tokens=512,
                )

        self.assertEqual(len(opener.requests), 2)

    def test_judge_does_not_retry_nonempty_invalid_json(self) -> None:
        opener = _SequenceOpener(b"not-json")
        with mock.patch.object(judge_adapter, "_OPENER", opener):
            with self.assertRaises(json.JSONDecodeError):
                _request(
                    base_url="https://provider.example.invalid/v1",
                    api_key="test-key",
                    model="gpt-5-mini",
                    envelope=_judge_envelope(),
                    timeout=10.0,
                    max_completion_tokens=512,
                )

        self.assertEqual(len(opener.requests), 1)

    def test_judge_falls_back_across_openai_compatible_400s(self) -> None:
        def bad_request() -> HTTPError:
            return HTTPError(
                "https://provider.example.invalid/v1/chat/completions",
                400,
                "Bad Request",
                {},
                None,
            )

        opener = _SequenceOpener(
            bad_request(),
            bad_request(),
            _provider_response('{"score":1}'),
        )
        with mock.patch.object(judge_adapter, "_OPENER", opener):
            score = _request(
                base_url="https://provider.example.invalid/v1",
                api_key="test-key",
                model="gpt-5-mini",
                envelope=_judge_envelope(),
                timeout=10.0,
                max_completion_tokens=512,
            )

        self.assertEqual(score, 1)
        payloads = [json.loads(request.data) for request, _ in opener.requests]
        self.assertEqual(payloads[0]["reasoning_effort"], "minimal")
        self.assertNotIn("reasoning_effort", payloads[1])
        self.assertIn("max_completion_tokens", payloads[1])
        self.assertNotIn("reasoning_effort", payloads[2])
        self.assertEqual(payloads[2]["max_tokens"], 512)
        self.assertNotIn("max_completion_tokens", payloads[2])


if __name__ == "__main__":
    unittest.main()

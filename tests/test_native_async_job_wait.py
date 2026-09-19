from __future__ import annotations

import json
import unittest

from agno.tools.function import ToolResult

from agent_runtime.graph_tools import wait_for_async_job_result


def _result(payload: dict) -> ToolResult:
    return ToolResult(content=json.dumps(payload, ensure_ascii=False))


class NativeAsyncJobWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_async_result_is_returned_unchanged(self) -> None:
        initial = _result({"success": True, "value": 3})

        async def unexpected_call(_job_id: str) -> ToolResult:
            self.fail("同步工具不应查询任务状态")

        actual = await wait_for_async_job_result(
            initial,
            call_status=unexpected_call,
            call_result=unexpected_call,
            poll_interval_seconds=0.1,
            wait_timeout_seconds=0,
        )

        self.assertIs(actual, initial)

    async def test_async_job_is_polled_until_done_and_returns_final_result(self) -> None:
        statuses = iter(
            [
                _result({"success": True, "job": {"status": "queued"}}),
                _result({"success": True, "job": {"status": "running"}}),
                _result({"success": True, "job": {"status": "done"}}),
            ]
        )
        status_job_ids: list[str] = []
        result_job_ids: list[str] = []

        async def call_status(job_id: str) -> ToolResult:
            status_job_ids.append(job_id)
            return next(statuses)

        final = _result(
            {
                "success": True,
                "result": {
                    "status": "done",
                    "summary": {"score_max": 6985},
                },
            }
        )

        async def call_result(job_id: str) -> ToolResult:
            result_job_ids.append(job_id)
            return final

        actual = await wait_for_async_job_result(
            _result({"success": True, "async": True, "job_id": "lg_test"}),
            call_status=call_status,
            call_result=call_result,
            poll_interval_seconds=0.001,
            wait_timeout_seconds=0,
        )

        self.assertIs(actual, final)
        self.assertEqual(status_job_ids, ["lg_test", "lg_test", "lg_test"])
        self.assertEqual(result_job_ids, ["lg_test"])

    async def test_failed_job_still_fetches_bounded_terminal_result(self) -> None:
        async def call_status(_job_id: str) -> ToolResult:
            return _result({"success": True, "job": {"status": "failed"}})

        final = _result(
            {
                "success": False,
                "result": {"status": "failed", "error": "resource rejected"},
            }
        )

        async def call_result(_job_id: str) -> ToolResult:
            return final

        actual = await wait_for_async_job_result(
            _result({"success": True, "async": True, "job_id": "lg_failed"}),
            call_status=call_status,
            call_result=call_result,
            poll_interval_seconds=0.001,
            wait_timeout_seconds=0,
        )

        self.assertIs(actual, final)


if __name__ == "__main__":
    unittest.main()

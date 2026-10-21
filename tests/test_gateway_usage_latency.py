"""Usage/timing are diagnostics, never speech or authority to execute a tool."""
import asyncio
import json
from uuid import UUID

import httpx
import pytest

from app.gemini_gateway import MODEL, sse
from test_gemini_gateway import frame, post, request_body, setup, tool_stream


def test_usage_only_frame_is_not_speech_and_observed_counts_stay_content_free(tmp_path, monkeypatch):
    # A random transport ID can legitimately contain the same digits as a token
    # count. Assert on parsed speech/usage fields, including that collision.
    monkeypatch.setattr("app.gemini_gateway.uuid4", lambda: UUID("00000000-0000-4270-8000-000000000001"))
    def provider(request):
        payload = json.loads(request.content)
        assert payload["stream_options"] == {"include_usage": True}
        usage = {"prompt_tokens": 4200, "completion_tokens": 70, "total_tokens": 4270,
                 "prompt_tokens_details": {"cached_tokens": 4096, "private": "do-not-save"},
                 "completion_tokens_details": {"reasoning_tokens": 40}, "private": "do-not-save"}
        stream = (frame({"content": "A short synthetic answer."}) + frame(reason="stop")
                  + sse({"model": MODEL, "choices": [], "usage": usage}) + b"data: [DONE]\n\n")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)
    app, gateway, _ = setup(tmp_path, provider)
    async def run():
        try:
            response = await post(app, request_body())
            assert response.status_code == 200 and "A short synthetic answer." in response.text
            chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            assert all("usage" not in chunk for chunk in chunks)
            spoken = "".join(choice.get("delta", {}).get("content", "")
                             for chunk in chunks for choice in chunk.get("choices", []))
            assert spoken == "A short synthetic answer."
            assert "do-not-save" not in response.text
            [row] = gateway.latency_snapshot("call-a")
            assert row["usage"] == {"input_tokens": 4200, "output_tokens": 70, "total_tokens": 4270,
                                    "cached_input_tokens": 4096, "reasoning_tokens": 40}
            assert row["milestones_ms"]["first_upstream_text"] <= row["milestones_ms"]["first_text"]
            assert row["durations_ms"]["validated_text_hold"] >= 0
            assert "do-not-save" not in json.dumps(row)
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": True}, {"prompt_tokens": -1},
    {"completion_tokens": "40"}, {"total_tokens": 100_000_001},
    {"prompt_tokens_details": "private", "completion_tokens_details": {"reasoning_tokens": False}}])
def test_absent_or_invalid_usage_remains_unknown_without_failing_generation(tmp_path, usage):
    def provider(_):
        stream = (frame({"content": "Synthetic reply."}) + frame(reason="stop")
                  + sse({"choices": [], "usage": usage}) + b"data: [DONE]\n\n")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)
    app, gateway, _ = setup(tmp_path, provider)
    async def run():
        try:
            assert (await post(app, request_body())).status_code == 200
            [row] = gateway.latency_snapshot("call-a")
            assert all(value is None for value in row["usage"].values())
        finally:
            await gateway.close()
    asyncio.run(run())


def test_diagnostic_failure_does_not_release_unverified_tool_preface_or_fail_tool(tmp_path):
    app, gateway, _ = setup(tmp_path, lambda _: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, content=tool_stream()))
    class BrokenSink:
        def record(self, _):
            raise OSError("private-path-must-not-escape")
    gateway.timing_sink = BrokenSink()
    async def run():
        try:
            response = await post(app, request_body())
            assert response.status_code == 200 and '"tool_calls"' in response.text
            assert "Let me check." not in response.text and "private-path" not in response.text
            [row] = gateway.latency_snapshot("call-a")
            assert row["milestones_ms"]["first_upstream_text"] is not None
            assert row["milestones_ms"]["first_text"] is None
            assert row["durations_ms"]["validated_text_hold"] is None
        finally:
            await gateway.close()
    asyncio.run(run())

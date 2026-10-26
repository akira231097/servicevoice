import asyncio
import json

import httpx
import pytest

from app.gemini_gateway import MODEL
from app.observability import safe_timing_row
from app.generation_observability import project_gateway_timings
from test_gemini_gateway import setup, request_body, frame, post, KEY, SECRET


@pytest.mark.parametrize("owned", [True, False])
def test_faster_model_is_scoped_and_actual_model_is_preserved(tmp_path, owned):
    async def run():
        seen = []
        selected = "gemini-3.5-flash-lite" if owned else MODEL
        def handler(request):
            seen.append(json.loads(request.content))
            wire = frame({"content": "Tell me more about the vehicle."})+frame(reason="stop")+b"data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire.replace(MODEL.encode(), selected.encode()))
        app, gateway, _ = setup(tmp_path, handler)
        gateway.waiting_cue_assistant = "our-assistant" if owned else "another-assistant"
        gateway.config_loader = lambda: {"GEMINI_GATEWAY_SECRET": SECRET, "GEMINI_API_KEY": KEY,
            "SERVICEVOICE_TWILIO_MODEL": "gemini-3.5-flash-lite"}
        try:
            result = await post(app, request_body())
            assert result.status_code == 200
            assert seen[0]["model"] == selected
            assert seen[0]["reasoning_effort"] == ("minimal" if owned else "low")
            row = gateway.latency_snapshot("call-a")[-1]
            assert row["model"] == selected
            # Disk and re-export boundaries retain the actual selected model.
            row["call_key"] = "a"*16
            projected = safe_timing_row(row)
            assert projected["model"] == selected
            again = project_gateway_timings([projected])["generations"][0]
            assert again["model"] == selected
        finally: await gateway.close()
    asyncio.run(run())


def test_candidate_response_from_wrong_model_is_rejected(tmp_path):
    async def run():
        app, gateway, _ = setup(tmp_path, lambda request: httpx.Response(200,
            headers={"content-type": "text/event-stream"},
            content=frame({"content": "Wrong provider model."})+frame(reason="stop")+b"data: [DONE]\n\n"))
        gateway.waiting_cue_assistant = "our-assistant"
        gateway.config_loader = lambda: {"GEMINI_GATEWAY_SECRET": SECRET, "GEMINI_API_KEY": KEY,
            "SERVICEVOICE_TWILIO_MODEL": "gemini-3.5-flash-lite"}
        try:
            response = await post(app, request_body())
            assert "Wrong provider model" not in response.text
            assert gateway.latency_snapshot("call-a")[-1]["outcome"] == "stream_unavailable"
        finally: await gateway.close()
    asyncio.run(run())


def test_empty_completed_provider_response_is_not_success(tmp_path):
    async def run():
        app, gateway, _ = setup(tmp_path, lambda request: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, content=frame(reason="stop")+b"data: [DONE]\n\n"))
        try:
            result = await post(app, request_body())
            assert b"[DONE]" not in result.content
            assert gateway.latency_snapshot("call-a")[-1]["outcome"] == "stream_unavailable"
        finally: await gateway.close()
    asyncio.run(run())

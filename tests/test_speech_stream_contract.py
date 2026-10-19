"""Fragmented speech text must survive the gateway once, without deduplication."""
import asyncio
import json

import httpx
import pytest

from test_gemini_gateway import setup, post, request_body, frame


@pytest.mark.parametrize("packet_bytes", [1, 7, 31])
def test_fragmented_multiscript_speech_and_repeated_digits_preserved_exactly(tmp_path, packet_bytes):
    # Splits occur in SSE framing and inside UTF-8 codepoints. Repeated digits
    # and deliberate corrections must not be removed by an audio symptom fix.
    fragments = ["Oil and filter service. ", "निदान जाँच। ", "వాహన తనిఖీ. ",
                 "Five ", "five ", "five. No, no booking yet."]
    events = [{"model": "gemini-3.8-flash", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]} for text in fragments]
    wire = b"".join(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8") for event in events)
    wire += frame(reason="stop") + b"data: [DONE]\n\n"

    class Fragmented(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(wire), packet_bytes):
                yield wire[offset:offset + packet_bytes]

    app, gateway, _ = setup(tmp_path, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Fragmented()))
    async def run():
        try:
            response = await post(app, request_body())
            assert response.status_code == 200
            frames = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
            assert frames.count("[DONE]") == 1
            parsed = [json.loads(value) for value in frames if value != "[DONE]"]
            assert not any("error" in event for event in parsed)
            rendered = [event["choices"][0]["delta"].get("content", "") for event in parsed]
            assert "".join(rendered) == "".join(fragments)
            assert sum(event["choices"][0]["finish_reason"] == "stop" for event in parsed) == 1
            assert not any(event["choices"][0]["delta"].get("tool_calls") for event in parsed)
            assert gateway.active == set()
        finally:
            await gateway.close()
    asyncio.run(run())

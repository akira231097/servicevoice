"""Content-free gateway timing and real loopback connection reuse; no provider calls."""
import asyncio
import hashlib
import importlib
import json
import sqlite3
import time
from types import SimpleNamespace

import dotenv
import httpx
import pytest
from fastapi.testclient import TestClient

from app import gemini_gateway as gateway_module
from app.gemini_gateway import GeminiGateway, GatewayLimits, OwnedStream, RequestTiming, TIMING_CAPACITY
from test_gemini_gateway import setup, post, request_body, frame, tool_stream


def test_text_is_withheld_until_done_and_timing_contains_no_content(tmp_path):
    _, gateway, _ = setup(tmp_path, lambda _: httpx.Response(500))

    async def scenario():
        waiting, release = asyncio.Event(), asyncio.Event()
        class SlowTail(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content": "PRIVATE-CALLER-"})
                yield frame({"content": "TEXT"})
                waiting.set()
                await release.wait()
                yield frame(reason="stop") + b"data: [DONE]\n\n"
        response = httpx.Response(200, stream=SlowTail())
        async def connected():
            return False
        iterator = gateway._relay(SimpleNamespace(is_disconnected=connected), response,
                                  "sid-a", "call-a", time.monotonic())
        next_chunk = asyncio.create_task(anext(iterator))
        try:
            await asyncio.wait_for(waiting.wait(), .5)
            assert not next_chunk.done() and not release.is_set(), "Upstream text is not validated speech yet"
            assert gateway.latency_snapshot("call-a") == [], "Live incomplete traces are not presented as finalized"
            release.set()
            first = await asyncio.wait_for(next_chunk, .5)
            assert b"PRIVATE-CALLER-TEXT" in first
            remaining = [chunk async for chunk in iterator]
            assert remaining[-1] == b"data: [DONE]\n\n"
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == "completed"
            assert row["milestones_ms"]["first_upstream_event"] <= row["milestones_ms"]["upstream_done"] <= row["milestones_ms"]["first_text"] <= row["milestones_ms"]["total"]
            assert row["milestones_ms"]["first_tool_fragment"] is None
            assert row["counts"]["text_chunks"] == 1
            assert row["counts"]["text_utf8_bytes"] == len("PRIVATE-CALLER-TEXT")
            assert "PRIVATE-CALLER-TEXT" not in json.dumps(row)
            assert "call-a" not in json.dumps(row)
            assert row["call_key"] == hashlib.sha256(b"call-a").hexdigest()[:16]
        finally:
            release.set()
            if not next_chunk.done():
                next_chunk.cancel()
            await asyncio.gather(next_chunk, return_exceptions=True)
            await iterator.aclose()
            await gateway.close()

    asyncio.run(scenario())


def test_tool_is_withheld_until_done_and_signature_persisted(tmp_path):
    _, gateway, path = setup(tmp_path, lambda _: httpx.Response(500))

    async def scenario():
        waiting, release = asyncio.Event(), asyncio.Event()
        class DelayedDone(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield tool_stream().removeprefix(frame({"content": "Let me check."})).removesuffix(b"data: [DONE]\n\n")
                waiting.set()
                await release.wait()
                yield b"data: [DONE]\n\n"
        async def connected():
            return False
        response = httpx.Response(200, stream=DelayedDone())
        iterator = gateway._relay(SimpleNamespace(is_disconnected=connected), response,
                                  "sid-a", "call-a", time.monotonic())
        next_chunk = asyncio.create_task(anext(iterator))
        try:
            await asyncio.wait_for(waiting.wait(), .5)
            assert not next_chunk.done()
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT count(*) FROM gemini_tool_signatures").fetchone()[0] == 0
            release.set()
            assert b'"tool_calls"' in await asyncio.wait_for(next_chunk, .5)
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT count(*) FROM gemini_tool_signatures").fetchone()[0] == 1
            _ = [chunk async for chunk in iterator]
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == "completed"
            assert row["milestones_ms"]["first_tool_fragment"] <= row["milestones_ms"]["upstream_done"] <= row["milestones_ms"]["tool_emitted"]
            assert row["durations_ms"]["tool_signature_persist"] >= 0
            assert row["counts"]["tool_calls"] == 1
            assert row["counts"]["text_chunks"] == 0
        finally:
            release.set()
            await next_chunk
            await iterator.aclose()
            await gateway.close()

    asyncio.run(scenario())


def test_trace_ignores_sensitive_transport_info_bounds_retention_and_returns_copies(tmp_path):
    _, gateway, _ = setup(tmp_path, lambda _: httpx.Response(500))

    class NeverInspect:
        def __str__(self):
            raise AssertionError("Transport info must not be formatted")
        def __repr__(self):
            raise AssertionError("Transport info must not be inspected")

    async def scenario():
        try:
            for index in range(TIMING_CAPACITY + 3):
                trace = RequestTiming("call-a" if index % 2 else "call-b", time.monotonic(), gateway.timings)
                await trace.transport_event("connection.connect_tcp.started", NeverInspect())
                await trace.transport_event("connection.start_tls.started", NeverInspect())
                await trace.transport_event("PRIVATE-HEADER-OR-TEXT", NeverInspect())
                trace.finish()
                trace.finish()
            rows = gateway.latency_snapshot()
            assert len(rows) == TIMING_CAPACITY
            assert all(row["counts"]["tcp_connects"] == row["counts"]["tls_handshakes"] == 1 for row in rows)
            assert "PRIVATE" not in json.dumps(rows)
            mine = gateway.latency_snapshot("call-a")
            assert len(mine) == TIMING_CAPACITY // 2
            mine[0]["counts"]["tcp_connects"] = 999
            assert gateway.latency_snapshot("call-a")[0]["counts"]["tcp_connects"] == 1
        finally:
            await gateway.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["headers", "stream"])
def test_failure_records_once_without_sensitive_error_or_false_completion(tmp_path, failure):
    def handler(request):
        if failure == "headers":
            raise httpx.ConnectError("PRIVATE-KEY-AND-REQUEST")
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"error":"PRIVATE-KEY-AND-REQUEST"}\n\n')
    app, gateway, _ = setup(tmp_path, handler)

    async def scenario():
        try:
            result = await post(app, request_body())
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == ("upstream_unavailable" if failure == "headers" else "stream_unavailable")
            assert row["milestones_ms"]["preflight"] is not None
            assert row["milestones_ms"]["tool_emitted"] is None
            assert row["counts"]["history_messages"] == 2
            assert not gateway.active
            encoded = json.dumps(row)
            for secret in ("PRIVATE", "private-session-capability", "+15555551234", "synthetic-google-key"):
                assert secret not in encoded and secret not in result.text
        finally:
            await gateway.close()

    asyncio.run(scenario())


def test_unstarted_stream_cleanup_finalizes_once(tmp_path):
    _, gateway, _ = setup(tmp_path, lambda _: httpx.Response(500))

    async def scenario():
        response = httpx.Response(200, content=b"unused")
        trace = RequestTiming("call-a", time.monotonic(), gateway.timings)
        async def body():
            raise AssertionError("The stream must never start")
            yield b"unreachable"
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            raise RuntimeError("downstream closed")
        stream = OwnedStream(body(), lambda: gateway._cleanup(response, "call-a", trace))
        try:
            with pytest.raises(RuntimeError):
                await stream({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            await gateway._cleanup(response, "call-a", trace)
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == "interrupted"
            assert row["milestones_ms"]["upstream_done"] is None
            assert response.is_closed
        finally:
            await gateway.close()

    asyncio.run(scenario())


def test_cancelled_stream_finalizes_once_and_closes_upstream(tmp_path):
    _, gateway, _ = setup(tmp_path, lambda _: httpx.Response(500))

    async def scenario():
        waiting = asyncio.Event()
        class Unfinished(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content": "A partial response"})
                waiting.set()
                await asyncio.Event().wait()
        async def connected():
            return False
        response = httpx.Response(200, stream=Unfinished())
        iterator = gateway._relay(SimpleNamespace(is_disconnected=connected), response,
                                  "sid-a", "call-a", time.monotonic())
        pending = asyncio.create_task(anext(iterator))
        try:
            await asyncio.wait_for(waiting.wait(), .5)
            assert not pending.done(), "Unfinished upstream prose must remain buffered"
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await iterator.aclose()
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == "cancelled"
            assert row["milestones_ms"]["upstream_done"] is None
            assert row["milestones_ms"]["first_upstream_event"] is not None
            assert row["milestones_ms"]["first_text"] is None
            assert row["counts"]["text_chunks"] == row["counts"]["text_utf8_bytes"] == 0
            assert "A partial response" not in json.dumps(row)
            assert response.is_closed and not gateway.active
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()
            await gateway.close()

    asyncio.run(scenario())


def test_late_old_cleanup_cannot_release_new_generation_of_same_call(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        provider_calls = []
        async def handler(request):
            provider_calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=frame({"content": "New generation"}) + frame(reason="stop") + b"data: [DONE]\n\n")
        app, gateway, _ = setup(tmp_path, handler, GatewayLimits(concurrency=1))
        old_response = httpx.Response(200, content=frame({"content": "Old generation"}) + frame(reason="stop") + b"data: [DONE]\n\n")
        old_timing = RequestTiming("call-a", time.monotonic(), gateway.timings)
        async def connected():
            return False
        gateway.active.add("call-a")
        old_iterator = gateway._relay(SimpleNamespace(is_disconnected=connected), old_response,
                                      "sid-a", "call-a", time.monotonic(), old_timing)
        newer = None
        try:
            _ = [chunk async for chunk in old_iterator]
            assert not gateway.active
            newer = asyncio.create_task(post(app, request_body()))
            await asyncio.wait_for(entered.wait(), 1)
            assert gateway.active == {"call-a"}
            owner = gateway.owners["call-a"]
            # OwnedStream's cleanup may run after an awaited downstream send.
            await gateway._cleanup(old_response, "call-a", old_timing)
            assert gateway.active == {"call-a"}
            assert gateway.owners["call-a"] is owner
            assert not owner.superseded.is_set() and not owner.released.is_set()
            # A new request for this same call intentionally supersedes its
            # owner. Use another call to probe whether late cleanup incorrectly
            # freed the occupied global capacity, without replacing the owner.
            assert (await post(app, request_body("call-b"))).status_code == 429
            assert gateway.owners["call-a"] is owner and len(provider_calls) == 1
            release.set()
            result = await newer
            assert result.status_code == 200 and "New generation" in result.text
            assert "Old generation" not in result.text and "generation_unavailable" not in result.text
            assert result.text.endswith("data: [DONE]\n\n")
            assert not gateway.active and not gateway.owners and not gateway.successors
            assert owner.released.is_set()
            assert len(gateway.latency_snapshot("call-a")) == 2  # Old and new generations.
            [rejected] = gateway.latency_snapshot("call-b")
            assert rejected["outcome"] == "capacity_rejected"
        finally:
            release.set()
            if newer is not None:
                await newer
            await old_iterator.aclose()
            await gateway.close()

    asyncio.run(scenario())


def test_gateway_client_reuses_real_connection_after_default_five_second_expiry(tmp_path, monkeypatch):
    # Exercise actual HTTP/1.1 pooling on loopback. A mock transport cannot prove
    # keep-alive reuse. No provider URL, credential or microphone is involved.
    monkeypatch.setenv("NO_PROXY", "*")
    app, temporary, _ = setup(tmp_path, lambda _: httpx.Response(500))

    async def scenario():
        accepted, writers, tasks = [], set(), set()
        async def serve(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            writers.add(writer)
            accepted.append(writer)
            try:
                while True:
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nOK")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writers.discard(writer)
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)
        await temporary.close()
        gateway = GeminiGateway(app, lambda: {}, lambda *args: "unused")
        baseline = httpx.AsyncClient(trust_env=False)  # HTTPX's unchanged 5-second default.
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        traces = []
        async def request(client):
            trace = RequestTiming("local-test", time.monotonic(), [])
            response = await client.get(url, extensions={"trace": trace.transport_event})
            assert response.text == "OK"
            traces.append(trace)
            return trace
        try:
            first, control_first = await request(gateway.client), await request(baseline)
            await asyncio.sleep(5.15)
            reused, control_reconnected = await request(gateway.client), await request(baseline)
            assert [value.row["counts"]["tcp_connects"] for value in
                    (first, control_first, reused, control_reconnected)] == [1, 1, 0, 1]
            assert len(accepted) == 3, "Extended client reuses one socket; default client opens two"
        finally:
            await baseline.aclose()
            await gateway.close()
            server.close()
            await server.wait_closed()
            for writer in list(writers):
                writer.close()
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)

    asyncio.run(scenario())


def test_actual_gateway_sse_reuses_completed_http_connection(tmp_path, monkeypatch):
    async def scenario():
        accepted, writers, tasks = [], set(), set()
        async def serve(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            writers.add(writer)
            accepted.append(writer)
            try:
                while True:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:"))
                    await reader.readexactly(length)
                    content = frame({"content": "Ready"}) + frame(reason="stop") + b"data: [DONE]\n\n"
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n"
                                 + f"{len(content):x}\r\n".encode() + content + b"\r\n0\r\n\r\n")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writers.discard(writer)
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)
        app, gateway, _ = setup(tmp_path, lambda _: httpx.Response(500))
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(trust_env=False, limits=httpx.Limits(keepalive_expiry=60))
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        monkeypatch.setattr(gateway_module, "GOOGLE_URL", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/")
        try:
            assert "Ready" in (await post(app, request_body())).text
            assert "Ready" in (await post(app, request_body())).text
            rows = gateway.latency_snapshot("call-a")
            assert [row["outcome"] for row in rows] == ["completed", "completed"]
            assert [row["counts"]["tcp_connects"] for row in rows] == [1, 0]
            assert len(accepted) == 1
        finally:
            await gateway.close()
            server.close()
            await server.wait_closed()
            for writer in list(writers):
                writer.close()
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("tail_mode,expected", [
    ("buffered_private", "eof"), ("timeout", "timeout"),
    ("read_error", "read_error"), ("oversize", "size_limit"),
])
def test_optional_eof_drain_is_bounded_private_and_reports_failure(tmp_path, tail_mode, expected):
    class Tail(httpx.AsyncByteStream):
        closed = False
        reads_after_done = 0
        async def __aiter__(self):
            content = tool_stream().removeprefix(frame({"content": "Let me check."}))
            if tail_mode == "buffered_private":
                content += b'data: {"PRIVATE-TAIL":"not a model instruction"}\n\n'
            yield content
            self.reads_after_done += 1
            if tail_mode == "timeout":
                await asyncio.sleep(1)
            elif tail_mode == "read_error":
                raise httpx.ReadError("PRIVATE-TRANSPORT-ERROR")
            elif tail_mode == "oversize":
                yield b"x" * (gateway_module.EOF_DRAIN_BYTES + 1)
                raise AssertionError("Oversized tail must not be read again")
        async def aclose(self):
            self.closed = True
    stream = Tail()
    app, gateway, path = setup(tmp_path, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream))

    async def scenario():
        try:
            started = time.monotonic()
            response = await post(app, request_body())
            assert time.monotonic() - started < .8
            assert "[DONE]" in response.text and '"tool_calls"' in response.text
            assert "PRIVATE" not in response.text
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == "completed", "Valid DONE remains authoritative despite optional drain failure"
            assert row["eof_drain_outcome"] == expected
            assert row["durations_ms"]["upstream_eof_drain"] < 300
            assert row["milestones_ms"]["upstream_done"] <= row["milestones_ms"]["tool_emitted"]
            assert row["counts"]["tool_calls"] == 1
            assert "PRIVATE" not in json.dumps(row)
            assert stream.closed and not gateway.active
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT count(*) FROM gemini_tool_signatures").fetchone()[0] == 1
        finally:
            await gateway.close()

    asyncio.run(scenario())


def test_eof_drain_cannot_extend_original_generation_deadline(tmp_path):
    class SlowEOF(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield tool_stream().removeprefix(frame({"content": "Let me check."}))
            await asyncio.sleep(1)
    app, gateway, path = setup(tmp_path, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowEOF()),
                               GatewayLimits(total_seconds=.02))

    async def scenario():
        try:
            # Isolate the EOF phase: the separate header-deadline test covers
            # preflight/admission, which can itself consume this 20 ms budget.
            # The original stream budget is unchanged, not widened for CI.
            started = time.monotonic()
            response = httpx.Response(200, stream=SlowEOF())
            async def connected():return False
            gateway.active.add("call-a")
            text = b"".join([part async for part in gateway._relay(
                SimpleNamespace(is_disconnected=connected),response,"sid-a","call-a",started)]).decode()
            assert time.monotonic() - started < .5
            assert "generation_unavailable" in text
            assert '"tool_calls"' not in text and "[DONE]" not in text
            assert response.is_closed and not gateway.active
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == "stream_unavailable"
            assert row["eof_drain_outcome"] != "eof"
            assert row["milestones_ms"]["tool_emitted"] is None
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT count(*) FROM gemini_tool_signatures").fetchone()[0] == 0
        finally:
            await gateway.close()

    asyncio.run(scenario())


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: {})
    monkeypatch.setenv("SERVICEVOICE_DB", str(tmp_path / "import.sqlite3"))
    module = importlib.import_module("app.main")
    app = module.create_app(tmp_path / "console.sqlite3")
    with TestClient(app) as client:
        yield client


def test_timing_endpoint_authorizes_session_isolates_calls_and_allows_ended(console):
    store, gateway = console.app.state.store, console.app.state.gemini_gateway
    mine, other, unbound = [store.create_session("live") for _ in range(3)]
    with store.connect() as db:
        db.execute("UPDATE sessions SET provider_call_id='call-a',ended=1 WHERE id=?", (mine["id"],))
        db.execute("UPDATE sessions SET provider_call_id='call-b' WHERE id=?", (other["id"],))
    for call_id in ("call-a", "call-b"):
        RequestTiming(call_id, time.monotonic(), gateway.timings).finish()
    path = f"/api/sessions/{mine['id']}/gateway-timing"
    assert console.get(path).status_code == 401
    assert console.get(path, headers={"x-session-token": other["token"]}).status_code == 401
    response = console.get(path, headers={"x-session-token": mine["token"]})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["session_id"] == mine["id"]
    assert response.json()["provider_call_id"] == "call-a"
    assert mine["token"] not in response.text and other["token"] not in response.text
    assert [row["call_key"] for row in response.json()["requests"]] == [hashlib.sha256(b"call-a").hexdigest()[:16]]
    unbound_response = console.get(f"/api/sessions/{unbound['id']}/gateway-timing",
                                  headers={"x-session-token": unbound["token"]}).json()
    assert unbound_response["requests"] == []
    assert unbound_response["session_id"] == unbound["id"]
    assert unbound_response["provider_call_id"] is None
    assert console.get(path, headers={"x-session-token": mine["token"], "host": "external.example"}).status_code == 403


def test_public_listener_has_no_timing_route_even_with_valid_token_and_localhost(console):
    session = console.app.state.store.create_session("live")
    module = importlib.import_module("app.main")
    with TestClient(module.create_public_app(console.app)) as public:
        for host in ("localhost", "127.0.0.1", "external.example"):
            response = public.get(f"/api/sessions/{session['id']}/gateway-timing",
                                  headers={"host": host, "x-session-token": session["token"]})
            assert response.status_code == 404

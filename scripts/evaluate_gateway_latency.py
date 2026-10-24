"""Bounded synthetic evaluation of the real streaming gateway and tool harness.

Dry-run is offline. --execute consumes Gemini tokens in disposable databases;
it never imports app.main, contacts Vapi, opens a microphone, or uses the live DB.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCENARIOS = ("hours", "location", "unclear_request", "constrained_availability",
             "first_available_confirm", "phone_correction_decline")
FAQ_UTTERANCES = {"hours": "What time do you close on weekdays?",
                  "location": "What is your verified street address?"}
MAX_BYTES = 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def source_manifest():
    paths = [*ROOT.glob("app/**/*.py"), ROOT / "runtime_sqlite.py", Path(__file__),
             ROOT / "requirements.txt", ROOT / "requirements-lock.txt"]
    return {str(path.relative_to(ROOT)).replace("\\", "/"): sha(path.read_bytes())
            for path in sorted(set(paths)) if path.is_file()}


class BudgetExhausted(Exception):
    pass


class EventProbe:
    """Observe fixed timing fields only; provider content never enters reports."""
    def __init__(self, row, started, clock=time.monotonic):
        self.row, self.started, self.clock = row, started, clock
        self.buffer = b""
        self.bounded = False

    def mark(self, name):
        if self.row.get(name) is None:
            self.row[name] = round(max(0, self.clock() - self.started) * 1000, 3)

    def feed(self, part):
        if self.bounded:
            return
        self.buffer += part
        if len(self.buffer) > MAX_BYTES:
            self.buffer, self.bounded = b"", True
            return
        self.buffer = self.buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in self.buffer:
            event, self.buffer = self.buffer.split(b"\n\n", 1)
            lines = [line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:")]
            if not lines:
                continue
            data = b"\n".join(lines)
            self.mark("first_event_ms")
            if data == b"[DONE]":
                self.mark("done_ms")
                continue
            try:
                value = json.loads(data)
                choices = value.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    continue
                delta = choices[0].get("delta")
                if not isinstance(delta, dict):
                    continue
                if isinstance(delta.get("content"), str) and delta["content"]:
                    self.mark("first_content_ms")
                if delta.get("tool_calls"):
                    self.mark("first_tool_ms")
            except (ValueError, TypeError, AttributeError, RecursionError):
                continue  # The gateway, not this timing probe, validates content.


def measured_provider(client, budget, rows):
    import httpx

    class Stream(httpx.AsyncByteStream):
        def __init__(self, response, probe):
            self.response, self.probe = response, probe

        async def __aiter__(self):
            try:
                async for part in self.response.aiter_bytes():
                    self.probe.feed(part)
                    yield part
            finally:
                self.probe.row["elapsed_ms"] = round((time.monotonic() - self.probe.started) * 1000, 3)

        async def aclose(self):
            await self.response.aclose()

    class Provider:
        def build_request(self, *args, **kwargs):
            return client.build_request(*args, **kwargs)

        async def send(self, request, **kwargs):
            if budget[0] <= 0:
                self.budget_exhausted = True
                raise BudgetExhausted("request_budget_exhausted")
            budget[0] -= 1
            row = {"index": len(rows) + 1, "status": "started", "http_status": None,
                   "headers_ms": None, "first_event_ms": None, "first_content_ms": None,
                   "first_tool_ms": None, "done_ms": None, "elapsed_ms": None,
                   "gateway_start_to_send_ms": round((time.monotonic()-self.generation_started)*1000, 3)
                   if self.generation_started is not None else None}
            rows.append(row)
            began = time.monotonic()
            try:
                response = await client.send(request, **kwargs)
                row.update(http_status=response.status_code, headers_ms=round((time.monotonic()-began)*1000, 3),
                           status="response_received" if response.status_code == 200 else "http_error")
                # aiter_bytes already decodes content compression. Do not decode
                # the wrapper a second time; connection closure still belongs to
                # the actual upstream response and its pooled client.
                headers = [(name, value) for name, value in response.headers.multi_items()
                           if name.lower() not in {"content-encoding", "content-length"}]
                return httpx.Response(response.status_code, headers=headers, request=response.request,
                                      stream=Stream(response, EventProbe(row, began)))
            except BaseException:
                row.update(status="request_failed", elapsed_ms=round((time.monotonic()-began)*1000, 3))
                raise

        async def aclose(self):
            await client.aclose()

    provider = Provider()
    provider.rows, provider.budget_exhausted, provider.generation_started = rows, False, None
    return provider


def synthetic_request(body, secret):
    from starlette.requests import Request
    sent = False
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": canonical(body).encode(), "more_body": False}
        await asyncio.Event().wait()
    return Request({"type": "http", "method": "POST", "path": "/llm/chat/completions",
                    "headers": [(b"authorization", ("Bearer " + secret).encode()),
                                (b"content-type", b"application/json")],
                    "query_string": b"", "scheme": "http", "server": ("testserver", 80),
                    "client": ("127.0.0.1", 1)}, receive)


class Conversation:
    def __init__(self, path, key, provider, *, narration="on", jev="off", jev_key=None, model="gemini-3.8-flash"):
        from fastapi import FastAPI, HTTPException
        from app.domain import Store
        from app.gemini_gateway import install_gateway
        from app.harness import ToolHarness
        self.store = Store(path)
        self.sid = self.store.create_session("live")["id"]
        self.call_id = "synthetic-latency-" + uuid4().hex
        self.secret = "synthetic-evaluation-gateway-secret-only"
        self.messages, self.generations, self.tools = [], [], []
        self.evidence = {}
        self.config = {"GEMINI_API_KEY": key, "GEMINI_GATEWAY_SECRET": self.secret,
                       "VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom",
                       "SERVICEVOICE_RECEIPT_NARRATION": narration, "SERVICEVOICE_JEV_FAQ": jev,
                       "TYPESAFE_API_KEY": jev_key or ""}
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET provider_call_id=? WHERE id=?", (self.call_id, self.sid))
        app = FastAPI()
        app.state.store = self.store
        def bind(call, assistant=None):
            if call != {"id": self.call_id, "assistantId": "synthetic-eval-assistant"}:
                raise HTTPException(403, "synthetic_binding_failed")
            self.gateway.store.pin_call_policy(self.sid, self.call_id, "synthetic-eval-assistant", self.config)
            return self.sid
        self.gateway = install_gateway(app, lambda: self.config, bind, client=provider)
        self.gateway.waiting_cue_assistant = "synthetic-eval-assistant"
        self.config["SERVICEVOICE_TWILIO_MODEL"] = model
        self.provider = provider
        self.harness = ToolHarness(self.store)

    async def start(self):
        await self.gateway.start()

    async def generate(self):
        from app.gemini_gateway import MODEL
        from app.tools import TOOL_SCHEMAS
        body = {"model": MODEL, "stream": True, "messages": self.messages,
                "tools": TOOL_SCHEMAS, "call": {"id": self.call_id, "assistantId": "synthetic-eval-assistant"}}
        response = None
        started = time.monotonic()
        self.provider.generation_started = started
        request_offset = len(self.provider.rows)
        probe_row = {"first_event_ms": None, "first_content_ms": None, "first_tool_ms": None, "done_ms": None}
        probe = EventProbe(probe_row, started)
        message = {"role": "assistant", "content": ""}
        buffer, size, completed = b"", 0, False
        try:
            response = await self.gateway.handle(synthetic_request(body, self.secret))
            async for part in response.body_iterator:
                size += len(part)
                if size > MAX_BYTES:
                    raise ValueError("public_stream_bounds")
                probe.feed(part)
                buffer += part
                while b"\n\n" in buffer:
                    event, buffer = buffer.split(b"\n\n", 1)
                    data = b"\n".join(line[5:].lstrip() for line in event.split(b"\n") if line.startswith(b"data:"))
                    if data == b"[DONE]":
                        completed = True
                        continue
                    if not data:
                        continue
                    value = json.loads(data)
                    if "error" in value:
                        raise ValueError("gateway_stream_unavailable")
                    delta = value["choices"][0].get("delta") or {}
                    if delta.get("content"):
                        message["content"] += delta["content"]
                    for tool in delta.get("tool_calls") or []:
                        if "extra_content" in tool:
                            raise ValueError("private_signature_exposed")
                        message.setdefault("tool_calls", []).append({k: v for k, v in tool.items() if k != "index"})
            if not completed or buffer or (not message["content"] and not message.get("tool_calls")):
                raise ValueError("incomplete_public_stream")
            return message
        finally:
            if response is not None:
                await response.body_iterator.aclose()
                await response.cleanup()
            row = {"index": len(self.generations) + 1, "public": probe_row,
                   "duration_ms": round((time.monotonic()-started)*1000, 3),
                   "speech_sha256": sha(message["content"]), "speech_characters": len(message["content"]),
                   "stream_completed": completed,
                   "model_request_indices": [item["index"] for item in self.provider.rows[request_offset:]],
                   "tool_names": [tool["function"]["name"] for tool in message.get("tool_calls", [])]}
            provider_rows = self.provider.rows[request_offset:]
            upstream_content = next((item["gateway_start_to_send_ms"] + item["first_content_ms"]
                                     for item in provider_rows if item["gateway_start_to_send_ms"] is not None
                                     and item["first_content_ms"] is not None), None)
            row["upstream_first_content_from_generation_ms"] = upstream_content
            row["validated_text_hold_ms"] = round(max(0, probe_row["first_content_ms"]-upstream_content), 3) if upstream_content is not None and probe_row["first_content_ms"] is not None else None
            snapshot = self.gateway.latency_snapshot(self.call_id)
            row["gateway"] = snapshot[-1] if snapshot else None
            # No raw call/object IDs or text hashes that are unnecessary for
            # latency attribution leave this disposable evaluation.
            if row["gateway"]:
                row["gateway"] = {k: v for k, v in row["gateway"].items()
                                  if k not in {"request_id", "call_key", "source_result_sha256", "output_text_sha256"}}
            self.generations.append(row)

    async def say(self, text):
        from app.tools import DEFINITIONS
        self.messages.append({"role": "user", "content": text})
        speech = ""
        for _ in range(6):
            message = await self.generate()
            self.messages.append(message)
            speech += message["content"]
            calls = message.get("tool_calls") or []
            if not calls:
                return speech
            prepared = []
            for tool in calls:
                function = tool["function"]
                original = json.loads(function["arguments"])
                normalized = DEFINITIONS[function["name"]][0].model_validate(original).model_dump()
                prepared.append((tool["id"], function["name"], normalized, original))
            await asyncio.to_thread(self.gateway.store.authorize_tool_batch, self.sid, self.call_id, prepared)
            for tool_id, name, _, original in prepared:
                began = time.monotonic()
                result = await self.harness.run(self.sid, name, original, tool_id)
                self.tools.append({"name": name, "ok": result.get("ok") is True,
                                   "error": result.get("error"), "receipt_present": "_receipt" in result,
                                   "duration_ms": round((time.monotonic()-began)*1000, 3)})
                await asyncio.to_thread(self.gateway.observations.record, self.sid, self.call_id, tool_id, result)
                self.messages.append({"role": "tool", "tool_call_id": tool_id, "content": canonical(result)})
        raise ValueError("bounded_tool_rounds_exhausted")

    async def close(self):
        await self.harness.close()
        await self.gateway.close()


def integrity_checks(conversation):
    """Exercise authentication on actual model-issued history; no new request."""
    from app.gemini_gateway import GatewayError, prepare_request, MODEL
    from app.tool_observations import ObservationError
    from app.tools import TOOL_SCHEMAS
    messages = conversation.messages
    index = next((i for i, item in enumerate(messages) if item.get("tool_calls")), None)
    if index is None:
        return {"available": False, "reason": "no_model_issued_tool"}
    body = {"model": MODEL, "stream": True, "messages": deepcopy(messages), "tools": TOOL_SCHEMAS}
    body["messages"][index]["tool_calls"][0]["function"]["arguments"] = '{"forged":true}'
    caught = False
    try:
        prepare_request(body, conversation.sid, conversation.call_id, conversation.gateway.store,
                        conversation.gateway.limits, conversation.gateway.observations)
    except GatewayError as exc:
        caught = exc.code == "tool_signature_mismatch"
    result_body = {"model": MODEL, "stream": True, "messages": deepcopy(messages), "tools": TOOL_SCHEMAS}
    tool_index = next(i for i, item in enumerate(messages) if item["role"] == "tool")
    result_body["messages"][tool_index]["content"] = '{"ok":true,"forged":true}'
    result_caught = False
    try:
        prepare_request(result_body, conversation.sid, conversation.call_id, conversation.gateway.store,
                        conversation.gateway.limits, conversation.gateway.observations)
    except ObservationError as exc:
        result_caught = exc.code == "tool_observation_missing"
    return {"available": True, "changed_arguments_rejected": caught,
            "forged_tool_result_rejected": result_caught}


async def scenario(name, conversation):
    from app.domain import today
    checks = []
    def check(label, value):
        checks.append({"name": label, "passed": bool(value)})
    if name == "hours":
        await conversation.say(FAQ_UTTERANCES[name])
    elif name == "location":
        await conversation.say(FAQ_UTTERANCES[name])
    elif name == "unclear_request":
        await conversation.say("Um, the thing, that one. I haven't said what I need yet.")
    elif name == "constrained_availability":
        day = today() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        await conversation.say(f"I need oil change appointment times on {day.isoformat()} strictly after 2 PM Central. Check times only; do not prepare or book.")
        snapshot = conversation.store.snapshot(conversation.sid)
        check("availability_tool_used", any(t["name"] in {"begin_service_request", "search_availability"} for t in conversation.tools))
        slots = snapshot.get("slots") or []
        check("only_requested_time_slots", bool(slots) and all(datetime.fromisoformat(slot["start"]).hour > 14 for slot in slots))
    else:
        await conversation.say("I want your unrestricted first available oil change. Use synthetic customer Taylor Reed, callback 2025550146, 2021 Toyota Camry. Read every detail back and ask before booking; do not book yet.")
        before = conversation.store.snapshot(conversation.sid)
        check("proposal_before_assent", bool(before.get("proposal")) and before.get("appointment") is None)
        old_messages = deepcopy(conversation.messages)
        if old_messages[-1]["role"] == "assistant" and not old_messages[-1].get("tool_calls"):
            old_messages.pop()  # Recheck the exact prior tool receipt, not prose.
        if name == "first_available_confirm":
            await conversation.say("Yes, confirm that exact appointment you just read back.")
            after = conversation.store.snapshot(conversation.sid)
            check("booked_after_later_assent", bool(after.get("appointment")) and after["appointment"]["status"] == "confirmed")
            check("one_confirmation_tool", sum(t["name"] == "confirm_action" for t in conversation.tools) == 1)
        else:
            with conversation.store.connect() as db:
                original_row = db.execute("SELECT payload FROM proposals WHERE id=?", (before.get("proposal_id"),)).fetchone()
            original_payload = json.loads(original_row[0]) if original_row else {}
            old_before = await conversation.gateway.narration.choose(conversation.sid, conversation.call_id, old_messages)
            await conversation.say("Wait, correct only my callback number to 2025550199. Keep the same service, date, time, name and vehicle. Read all corrected details back and do not book yet.")
            corrected = conversation.store.snapshot(conversation.sid)
            check("correction_not_booked", corrected.get("appointment") is None)
            check("fresh_corrected_proposal", bool(corrected.get("proposal")) and corrected["proposal"]["id"] != (before.get("proposal") or {}).get("id"))
            with conversation.store.connect() as db:
                proposal = db.execute("SELECT payload FROM proposals WHERE id=?", (corrected.get("proposal_id"),)).fetchone()
            payload = json.loads(proposal[0]) if proposal else {}
            check("corrected_callback_persisted", payload.get("customer", {}).get("phone") == "+12025550199")
            check("contact_correction_preserves_identity", bool(original_payload and payload)
                  and all(payload.get(key) == original_payload.get(key) for key in ("service", "slot_id"))
                  and all(payload.get("customer", {}).get(key) == original_payload.get("customer", {}).get(key)
                          for key in ("name", "vehicle")))
            old = await conversation.gateway.narration.choose(conversation.sid, conversation.call_id, old_messages)
            check("old_receipt_not_spoken", old.text is None)
            conversation.evidence["stale_receipt_recheck"] = {"eligible_before_correction": old_before.text is not None,
                                                            "reason_before": old_before.reason, "reason_after": old.reason,
                                                            "text_released_after_correction": old.text is not None}
            await conversation.say("No, don't book. Withdraw the pending proposal.")
            declined = conversation.store.snapshot(conversation.sid)
            check("decline_dismissed", declined.get("proposal") is None and declined.get("appointment") is None)
    snapshot = conversation.store.snapshot(conversation.sid)
    if name not in {"first_available_confirm", "phone_correction_decline"}:
        check("no_business_effects", snapshot.get("appointment") is None and snapshot.get("proposal") is None and snapshot.get("handoff") is None)
    check("all_tool_calls_succeeded", all(tool["ok"] for tool in conversation.tools))
    evidence = integrity_checks(conversation)
    if evidence["available"]:
        check("issued_arguments_authenticated", evidence["changed_arguments_rejected"])
        check("observed_results_authenticated", evidence["forged_tool_result_rejected"])
    return checks, evidence


async def run(args, key, jev_key=None, *, provider_transport=None, on_checkpoint=None):
    import httpx
    from runtime_sqlite import ensure_sqlite
    from evals.metrics import distribution
    ensure_sqlite()
    report = {"schema_version": 1, "kind": "disposable_real_gateway_streaming_eval", "status": "running",
              "started_at": datetime.now(timezone.utc).isoformat(), "source": {"before": source_manifest()},
              "configuration": {"model": getattr(args, "model", "gemini-3.8-flash"),
                                "reasoning_effort": "minimal" if getattr(args, "model", None) == "gemini-3.5-flash-lite" else "low", "concurrency": 1,
                                "max_requests": args.max_requests, "receipt_narration": args.receipt_narration,
                                "jev_faq": args.jev_faq, "jev_credential_present": bool(jev_key)},
              "provider_requests": [], "cases": [], "passed": False,
              "limitations": ["Synthetic authored scenarios and disposable scheduler only; no live DB, Vapi, microphone, TTS or acoustic timing.",
                              "Public-content timing measures the gateway's validated release, not token streaming or audible speech.",
                              "Upstream first-content timing is before the gateway's full finish/signature validation and is not safe-to-speak latency.",
                              "Speech content is not saved; hashes and business checks do not establish hallucination or fluency scores.",
                              "Each disposable conversation starts a fresh model HTTP pool; later requests in that conversation may reuse it. First requests are cold, unlike a continuously reused production pool.",
                              "Small samples, concurrency one, no model retries; no production percentile or multilingual claim."]}
    budget = [args.max_requests]
    if on_checkpoint is not None:
        on_checkpoint(report)
    with tempfile.TemporaryDirectory(prefix="servicevoice-gateway-eval-") as temp:
        for name in args.cases:
            row = {"scenario": name, "status": "started", "checks": [], "generations": [], "tools": []}
            report["cases"].append(row)
            client = httpx.AsyncClient(transport=provider_transport, timeout=30, follow_redirects=False, trust_env=False,
                                      limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=60))
            provider = measured_provider(client, budget, report["provider_requests"])
            conversation = Conversation(Path(temp) / (name + ".sqlite3"), key, provider,
                                        narration=args.receipt_narration, jev=args.jev_faq, jev_key=jev_key,
                                        model=getattr(args, "model", "gemini-3.8-flash"))
            try:
                async with asyncio.timeout(180):
                    await conversation.start()
                    row["checks"], row["history_integrity"] = await scenario(name, conversation)
                    row["status"] = "completed"
            except asyncio.CancelledError:
                row.update(status="interrupted", error_code="evaluation_interrupted")
                raise
            except Exception:
                row.update(status="failed", error_code="request_budget_exhausted" if provider.budget_exhausted else "gateway_or_scenario_failed")
            finally:
                row["generations"], row["tools"], row["extra_evidence"] = conversation.generations, conversation.tools, conversation.evidence
                try:
                    await conversation.close()
                except Exception:
                    row.update(status="failed", error_code="cleanup_failed")
                if on_checkpoint is not None:
                    on_checkpoint(report)
            print(canonical({"scenario": name, "status": row["status"],
                             "failed_checks": [check["name"] for check in row["checks"] if not check["passed"]]}), flush=True)
    report["source"]["after"] = source_manifest()
    report["source_stable"] = report["source"]["before"] == report["source"]["after"]
    generations = [generation for row in report["cases"] for generation in row["generations"]]
    routes = sorted({generation["gateway"]["route"] for generation in generations if generation.get("gateway")})
    report["metrics"] = {"requests_consumed": args.max_requests-budget[0],
                         "upstream_first_content_ms": distribution(row["first_content_ms"] for row in report["provider_requests"]),
                         "upstream_first_tool_ms": distribution(row["first_tool_ms"] for row in report["provider_requests"]),
                         "public_first_content_ms": distribution(row["public"]["first_content_ms"] for row in generations),
                         "public_first_tool_ms": distribution(row["public"]["first_tool_ms"] for row in generations),
                         "validated_text_hold_ms": distribution(row["validated_text_hold_ms"] for row in generations),
                         "gateway_routes": {route: {"generations": len(selected := [g for g in generations if g.get("gateway", {}).get("route") == route]),
                                                    "public_first_content_ms": distribution(g["public"]["first_content_ms"] for g in selected),
                                                    "duration_ms": distribution(g["duration_ms"] for g in selected)} for route in routes}}
    report["status"] = "complete"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = (report["source_stable"] and len(report["cases"]) == len(args.cases)
                        and all(row["status"] == "completed" and row["checks"] and all(c["passed"] for c in row["checks"])
                                for row in report["cases"]))
    if on_checkpoint is not None:
        on_checkpoint(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--model", choices=("gemini-3.8-flash", "gemini-3.5-flash-lite"), default="gemini-3.8-flash")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--case", action="append", choices=SCENARIOS, dest="cases")
    parser.add_argument("--receipt-narration", choices=("on", "off"), default="on")
    parser.add_argument("--jev-faq", choices=("on", "off"), default="off")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    args.cases = args.cases or list(SCENARIOS)
    if not 1 <= args.max_requests <= 30 or len(args.cases) != len(set(args.cases)):
        parser.error("Use a request cap from 1 through 30 and unique scenarios.")
    if not args.execute:
        print(canonical({"mode": "offline_plan", "provider_requests": 0, "scenarios": args.cases,
                         "max_requests": args.max_requests, "runtime_db_access": False}))
        return 0
    if args.output is None:
        parser.error("--execute requires a new --output file.")
    # Reserve before credentials or network. Reusing an evidence path cannot
    # silently overwrite findings or repeat paid work.
    try:
        file = args.output.open("x", encoding="utf-8")
    except OSError:
        print(canonical({"error": "output_unavailable", "provider_requests": 0}))
        return 2
    report = {"status": "not_run", "error_code": "evaluation_unavailable", "provider_requests": []}
    def checkpoint(value):
        nonlocal report
        report = value
        file.seek(0)
        json.dump(value, file, indent=2, ensure_ascii=False, allow_nan=False)
        file.write("\n")
        file.truncate()
        file.flush()
    try:
        from dotenv import dotenv_values
        values = dotenv_values(ROOT / ".env")
        key = os.environ.get("GEMINI_API_KEY") or values.get("GEMINI_API_KEY")
        jev_key = (os.environ.get("TYPESAFE_API_KEY") or values.get("TYPESAFE_API_KEY")) if args.jev_faq == "on" else None
        if not key:
            report["error_code"] = "credential_missing"
        else:
            report = asyncio.run(run(args, key, jev_key, on_checkpoint=checkpoint))
    except KeyboardInterrupt:
        report.update(status="interrupted", error_code="evaluation_interrupted", passed=False)
    except Exception:
        report.update(status="failed", error_code="evaluation_unavailable", passed=False)
        # Fixed error code only; provider exceptions may contain secrets.
    finally:
        with file:
            file.seek(0)
            json.dump(report, file, indent=2, ensure_ascii=False, allow_nan=False)
            file.write("\n")
            file.truncate()
    print(canonical({"output": str(args.output), "passed": report.get("passed", False),
                     "requests_consumed": report.get("metrics", {}).get("requests_consumed")}))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

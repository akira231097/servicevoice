"""Optional bounded first-turn classification for public static facts only."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import re
import sqlite3
import time

import httpx
from starlette.concurrency import run_in_threadpool

from .domain import TENANT, dealership_facts, today
from .jev_classifier import ClassificationUnavailable, JevClassifier, route_candidate

FAQ_VERSION = "jev-static-faq-2026-10-18-2"
QUESTION = re.compile(r"^(?:hi[,!. ]+|hello[,!. ]+)?(?:what|when|where|which|how|are|is|do|does|can|could)\b", re.I)
TOPIC = re.compile(r"\b(?:hours|open|opens|opening|close|closes|closing|location|address|located|services|offer|offers|support|advisor|adviser|advisors|advisers|transfer|transfers|callback|callbacks)\b", re.I)
# Eligibility is not intent authority. Broad exclusions trade coverage for no
# extra classifier latency on obvious actions, dangerous symptoms or a person.
EXCLUDED = re.compile(r"\b(?:book|booking|schedule|scheduled|scheduling|appointment|appointments|reschedule|cancel|cancellation|move|change|reserve|reservation|available|availability|urgent|emergency|danger|dangerous|smoke|fire|unsafe|safe|drive|driving|brake|brakes|warning|stuck|collision|human|person|speak|talk|connect|save|request|arrange|troubleshoot|diagnose|repair|today|tomorrow|holiday|price|cost|warranty|recall)\b|\b(?:call|transfer)\s+me\b", re.I)


@dataclass(frozen=True)
class FaqDecision:
    reason: str
    text: str | None = None
    elapsed_ms: float = 0
    classification_ms: float | None = None
    requests: int = 0
    probability: float | None = None
    margin: float | None = None
    language_probability: float | None = None


def eligible_text(messages):
    history = [message for message in messages if message.get("role") != "system"]
    if (not history or history[-1].get("role") != "user"
            or sum(message.get("role") == "user" for message in history) != 1
            or any(message.get("role") == "tool" or message.get("tool_calls") is not None for message in history)):
        return None, "not_first_user_turn"
    text = history[-1].get("content")
    if not isinstance(text, str) or not text.strip() or len(text) > 1600 or not text.isascii():
        return None, "input_or_language_bounds"
    text = text.strip()
    if EXCLUDED.search(text):return None, "action_or_complex_request"
    if not QUESTION.search(text) or not TOPIC.search(text):return None, "not_simple_faq_candidate"
    return text, "eligible_text"


def fresh_state(store, sid, call_id):
    try:
        with store.read_connection(0.05) as db:
            db.execute("BEGIN")
            session = db.execute("SELECT * FROM sessions WHERE id=? AND tenant=?", (sid, TENANT)).fetchone()
            if not session or session["ended"] or session["provider_call_id"] != call_id or time.time()-session["created"] > 4*3600:
                return False
            state = json.loads(session["state"])
            if state.get("stage") != "service" or state.get("proposal_id") or state.get("customer") or state.get("slots"):
                return False
            for table in ("appointments", "proposals", "handoffs", "tool_results", "tool_invocations"):
                if db.execute(f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (sid,)).fetchone():return False
            return True
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return False


def static_answer(label, facts):
    if label == "hours":
        return "Our service hours are " + facts["hours"]
    if label == "location":
        address = facts["physical_address"]
        return "Our verified service address is " + address if isinstance(address, str) and address else facts["location_note"]
    if label == "services":
        return "Our supported services are " + ", ".join(service["name"] for service in facts["services"]) + "."
    if label == "advisor_capability":
        capability = facts["handoff_capabilities"]
        if capability == {"local_request_queue": True, "live_transfer": False, "callback_delivery": False}:
            return "I can save a local advisor request, but this demo cannot transfer you or arrange a callback."
        local = "can" if capability["local_request_queue"] else "cannot"
        transfer = "is" if capability["live_transfer"] else "is not"
        callback = "is" if capability["callback_delivery"] else "is not"
        return f"This demo {local} save a local advisor request. Live transfer {transfer} connected, and callback delivery {callback} connected."
    return None


class JevFaqRouter:
    def __init__(self, store, *, client=None, timeout_seconds=0.35, read_capacity=4, connection_capacity=2):
        self.store, self.client = store, client
        self.owns_client = client is None
        self.timeout, self.capacity = timeout_seconds, read_capacity
        if type(connection_capacity) is not int or not 1<=connection_capacity<=16:
            raise ValueError("FAQ connection capacity must be between 1 and 16.")
        self.connection_capacity=connection_capacity
        self.reads = set()
        self.closing = False
        self._startup_task = None
        self.initialization_ms = None

    @property
    def ready(self):
        return self.client is not None and not self.client.is_closed and not self.closing

    def _build_client(self):
        # Windows SSL/client setup is synchronous and can exceed the entire
        # live classification budget. This factory runs only in a worker.
        return httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(max_connections=self.connection_capacity, max_keepalive_connections=self.connection_capacity, keepalive_expiry=60))

    async def _close_client(self, client):
        if client.is_closed:return
        with suppress(httpx.HTTPError, RuntimeError, TimeoutError):
            await asyncio.wait_for(client.aclose(), timeout=1)

    async def _initialize(self):
        began = time.monotonic()
        try:
            client = await run_in_threadpool(self._build_client)
            if self.closing:
                await self._close_client(client)
                return False
            self.client = client
            return True
        except Exception:
            return False  # Optional optimization stays unavailable until restart.
        finally:
            self.initialization_ms = round((time.monotonic()-began)*1000, 3)

    async def start(self):
        """Shared by both listeners; no network request and no live-path work."""
        if self.closing:return False
        if self.client is not None:return self.ready  # Preserve injected clients.
        if self._startup_task is None:
            self._startup_task = asyncio.create_task(self._initialize())
        return await asyncio.shield(self._startup_task)

    def connection(self):
        if not self.ready:raise ClassificationUnavailable("client_not_ready")
        return self.client

    async def close(self):
        self.closing = True
        if self._startup_task is not None and not self._startup_task.done():
            # Keep the initializer alive if teardown's wait expires: it sees
            # closing and closes its late-created client instead of publishing it.
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._startup_task), timeout=1)
        if self.owns_client and self.client is not None:
            await self._close_client(self.client)

    async def snapshot(self, sid, call_id):
        if len(self.reads) >= self.capacity:return False
        task = asyncio.create_task(run_in_threadpool(fresh_state, self.store, sid, call_id))
        self.reads.add(task)
        def finished(value):
            self.reads.discard(value)
            if not value.cancelled():value.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def choose(self, sid, call_id, messages, key, on_request=None):
        began = time.monotonic()
        info = {}
        def result(reason, text=None):
            return FaqDecision(reason, text, round((time.monotonic()-began)*1000, 3), **info)
        text, reason = eligible_text(messages)
        if text is None:return result(reason)
        if not isinstance(key, str) or not key:return result("credential_missing")
        if self.closing:return result("router_closed")
        if not self.ready:return result("classifier_not_ready")
        try:
            async with asyncio.timeout(self.timeout):
                if not await self.snapshot(sid, call_id):return result("active_or_unverified_state")
                state = {"utterance": text, "stage": "service", "has_pending_proposal": False,
                         "has_pending_operation": False, "has_appointment": False}
                # New immutable key binding per request; the pool survives key
                # rotation and no old request's client is closed underneath it.
                classifier = JevClassifier(key, client=self.connection(), timeout_seconds=self.timeout)
                info["requests"] = 1
                if on_request is not None:on_request()
                decision = await classifier.classify(state)
                scores = sorted(decision.probabilities.values(), reverse=True)
                info.update(classification_ms=decision.elapsed_ms, probability=scores[0],
                            margin=scores[0]-scores[1], language_probability=decision.language_probabilities["english_only"])
                route, reason = route_candidate(state, decision, deadline_ms=self.timeout*1000)
                if route != "static_faq":return result(reason)
                if self.closing or not await self.snapshot(sid, call_id):return result("state_changed")
                answer = static_answer(decision.label, dealership_facts(today()))
                if answer is None:return result("unknown_label")
                return result("verified_static_faq", answer)
        except TimeoutError:
            return result("classification_deadline")
        except ClassificationUnavailable as exc:
            # Only a small content-free category reaches telemetry.
            return result("classifier_" + (exc.code if exc.code in {"timeout", "transport_error", "model_mismatch", "invalid_response", "response_bounds", "inconsistent_confidence"} else "unavailable"))
        except Exception:
            return result("classifier_unavailable")

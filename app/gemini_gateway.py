"""Bounded Vapi -> Gemini 3.8 streaming bridge with durable tool signatures.

No tool executes here. The scheduler remains the authority for effects. Raw
request/response bodies, credentials and thought signatures are never logged.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import closing, suppress
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .http_boundary import read_json_body
from .tools import DEFINITIONS, SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION, TOOL_SCHEMAS, MAX_TOOL_BATCH
from .domain import dealership_facts, today
from .tool_observations import ObservationError, ToolObservationStore
from .receipt_narration import BoundedSelector, NARRATOR_VERSION
from .jev_faq import FAQ_VERSION, JevFaqRouter
from .jev_classifier import JEV_MODEL
from .backchannel_policy import BackchannelPolicy, WAITING_PHRASE_IDS, waiting_phrase, waiting_state, guarded_waiting_fallback
from .backchannel_catalog import PHRASES
from .backchannel_budget import BackchannelBudget
from .issuance_context import user_context
from .diagnostics import turn_identifier
from .call_lifecycle import END_CALL, CLOSING_POLICY, UNKNOWN_ATTEMPT_CONTEXT, enabled as closing_enabled, schema as closing_schema

MODEL = "gemini-3.8-flash"
UPSTREAM_MODELS = {MODEL: "low", "gemini-3.5-flash-lite": "minimal"}
# A low reasoning setting still spends output budget on hidden reasoning.
# 1024 truncated a constrained scheduling turn before it could emit a tool.
# This is a ceiling, not a target response length; voice prose stays concise.
MAX_OUTPUT_TOKENS = 2048
GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
TIMING_CAPACITY = 256
EOF_DRAIN_SECONDS = 0.05
EOF_DRAIN_BYTES = 16 * 1024
ACK_DELAY_SECONDS = 0.55
SERIAL_TOOL_POLICY = ("\nThis phone transport requires sequential actions: return at most ONE function call per response. "
    "For multiple requests, choose the next action, wait for its verified result, then handle the remaining request. "
    "Never combine an advisor request with an intake or booking command in one response.")
SERIAL_TOOL_RETRY = ("\nYour private candidate contained several function calls. NONE of that candidate's commands "
    "were issued or executed. Regenerate exactly ONE next function call, or a concise clarification. "
    "Do not claim that any of those actions succeeded.")
SERIAL_TOOL_CLARIFICATION = "Let's take those one at a time. Which request would you like me to handle first?"
PHONE_CONVERSATION_POLICY = ("\nPhone delivery: say one idea per turn, normally at most two short sentences and about "
    "20 words. Ask one question at a time. Do not repeat confirmed details or repeatedly use the caller's name. "
    "Complete booking readbacks, verified references and a short list of options are exceptions: never omit "
    "required information for brevity. The separate cue system handles waiting; do not add routine filler.")
PHONE_PRESENTATION_POLICY = ("\nFor this phone assistant, use the verified receipt reader for ordinary scheduling. "
    "For begin_service_request or search_availability, when the next reply only needs the returned offers, "
    "set presentation to an object. For prepare_booking, when the next reply is the complete readback, "
    "also set it to an object. For an English conversation about dealership-local appointments, use "
    '{"response_scope":"receipt_only","response_language":"en","caller_timezone":"dealership"}. '
    "The question asking the caller to choose an offer or confirm the readback is included in that receipt; "
    "it is NOT an extra unanswered question requiring presentation=null. Do not choose null just because "
    "the caller has not booked yet or has not explicitly named their language/timezone. Still use null for "
    "an additional unanswered policy/diagnostic question, unresolved correction, or uncertain language/timezone. "
    "Use the actual current language and resolved timezone, never this example blindly.")


class RequestTiming:
    """Fixed-schema, content-free observations; never retain transport info."""
    def __init__(self, call_id, started, sink, observer=None, correlation=None):
        self.started, self.sink, self.finished = started, sink, False
        self.observer,self.correlation=observer,correlation
        self.diagnostic_context={}
        self.admission_released = False
        self.generation = None
        self.outcome = "interrupted"
        # Runs once, inside finish(), before the row is copied anywhere. The
        # gateway uses it to settle an emitted waiting cue against its budget.
        self.before_finish = None
        self.backchannel_budget_stamp = None
        request_id=uuid4().hex
        self.row = {
            "request_id": request_id,
            "generation_id": str(UUID(request_id)),
            "generation_sequence": None,
            "generation_sequence_scope": "process_local_http_arrival_only",
            "predecessor_generation_id": None,
            "predecessor_relation": None,
            "superseded_by_generation_id": None,
            "supersession_cause": None,
            "superseded_at_ms": None,
            "payload_released_at_ms": None,
            "payload_release_scope": "gateway_yield_not_provider_receipt_or_playback",
            "downstream_disconnect_at_ms": None,
            "checkpoint_rejections": 0,
            "generation_terminal_state": None,
            "sse_response_id": None,
            "neutral_sse_response_id": None,
            "call_key": hashlib.sha256(call_id.encode()).hexdigest()[:16],
            "started_at_unix": time.time() - max(0, time.monotonic() - started),
            "system_prompt_sha256": None,
            "route": "gemini",
            "narration_reason": "feature_disabled",
            "narrator_version": NARRATOR_VERSION,
            "source_result_sha256": None,
            "output_text_sha256": None,
            "output_text_source": None,
            "jev_faq_version": FAQ_VERSION,
            "jev_model": JEV_MODEL,
            "jev_reason": "feature_disabled",
            "jev_probability": None,
            "jev_margin": None,
            "jev_language_probability": None,
            "outcome": None,
            "milestones_ms": {key: None for key in (
                "preflight", "upstream_headers", "first_upstream_event", "first_upstream_text", "first_text",
                "first_tool_fragment", "upstream_done", "tool_emitted", "total")},
            "durations_ms": {"tool_signature_persist": None, "upstream_eof_drain": None,
                             "jev_route": None, "jev_classification": None, "validated_text_hold": None},
            "usage": {key: None for key in (
                "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")},
            "eof_drain_outcome": "not_attempted",
            "counts": {key: 0 for key in (
                "history_messages", "history_tool_results", "upstream_events", "text_chunks",
                "text_utf8_bytes", "tool_calls", "tcp_connects", "tls_handshakes", "eof_tail_bytes", "jev_requests")},
        }

    def mark(self, milestone):
        if not self.finished and self.row["milestones_ms"][milestone] is None:
            self.row["milestones_ms"][milestone] = round(max(0, time.monotonic() - self.started) * 1000, 3)

    def note_time(self,field):
        if not self.finished and self.row[field] is None:
            self.row[field]=round(max(0,time.monotonic()-self.started)*1000,3)

    def note_superseded(self,replacement_id=None,cause="unknown"):
        if self.finished:return
        self.note_time("superseded_at_ms")
        if self.row["supersession_cause"] is None:self.row["supersession_cause"]=cause
        if self.row["superseded_by_generation_id"] is None:
            self.row["superseded_by_generation_id"]=replacement_id

    async def transport_event(self, event_name, _info):
        # HTTPCore info can include requests, headers, hostnames and exceptions.
        # Count only these two fixed event names; never inspect or save info.
        if self.finished:
            return
        if event_name == "connection.connect_tcp.started":
            self.row["counts"]["tcp_connects"] += 1
        elif event_name == "connection.start_tls.started":
            self.row["counts"]["tls_handshakes"] += 1

    def finish(self):
        if self.finished:
            return
        if self.before_finish is not None:
            hook, self.before_finish = self.before_finish, None
            try:hook()
            except Exception:pass  # Budget bookkeeping must never block evidence persistence.
        self.mark("total")
        self.row["outcome"] = self.outcome
        if self.outcome=="supersession_wait_expired":
            terminal="supersession_wait_expired"
        elif self.generation is not None and self.generation.disconnected:
            terminal="downstream_disconnected"
        elif self.generation is not None and self.generation.superseded.is_set():
            terminal="superseded_after_release" if self.generation.payload_emitted else "superseded_before_release"
        elif self.outcome in {"cancelled","interrupted"}:
            terminal="cancelled_unknown_cause"
        else:terminal=self.outcome
        self.row["generation_terminal_state"]=terminal
        self.finished = True
        self.sink.append(deepcopy(self.row))
        if self.observer is not None:
            try:self.observer(self.row,self.correlation)
            except Exception:pass


@dataclass(frozen=True)
class GatewayLimits:
    concurrency: int = 4
    messages: int = 100
    message_chars: int = 12000
    response_bytes: int = 1024 * 1024
    event_bytes: int = 128 * 1024
    tool_calls: int = MAX_TOOL_BATCH
    argument_chars: int = 8000
    extra_chars: int = 65536
    total_seconds: float = 30
    cleanup_seconds: float = 1
    supersession_seconds: float = 2
    signature_ttl: int = 4 * 3600
    signatures_per_call: int = 128
    signatures_total: int = 10000


class GatewayError(Exception):
    """Only fixed public codes are emitted; never include provider payloads."""
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


class GenerationSuperseded(Exception):
    """Internal control flow, never a provider-facing error."""


class Generation:
    def __init__(self, call_id, sequence, timing):
        self.call_id, self.sequence, self.timing = call_id, sequence, timing
        self.superseded, self.released = asyncio.Event(), asyncio.Event()
        self.task = None
        self.response = None
        self.cleanup_task = None
        self.disconnected = False
        self.payload_emitted = False


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def parse_json(value):
    try:
        return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError) as exc:
        raise GatewayError("invalid_json") from exc


def tool_identity(tool, limits, *, allow_end_call=False):
    if not isinstance(tool, dict):
        raise GatewayError("invalid_tool_history")
    tool_id, function = tool.get("id"), tool.get("function")
    if not isinstance(tool_id, str) or not 1 <= len(tool_id) <= 200 or not isinstance(function, dict):
        raise GatewayError("invalid_tool_history")
    name, arguments = function.get("name"), function.get("arguments")
    if (not isinstance(name, str) or (name not in DEFINITIONS and not (allow_end_call and name == END_CALL))
            or not isinstance(arguments, str) or len(arguments) > limits.argument_chars):
        raise GatewayError("invalid_tool_history")
    parsed = parse_json(arguments)
    if not isinstance(parsed, dict):
        raise GatewayError("invalid_tool_arguments")
    if name == END_CALL and parsed:
        raise GatewayError("invalid_platform_arguments")
    digest = hashlib.sha256(canonical({"name": name, "arguments": parsed}).encode()).hexdigest()
    return tool_id, name, arguments, digest


def configured_tool_policy(config):
    """Resolve only trusted local model selection, never incoming call overrides."""
    profile = (config.get("VAPI_MODEL_PROFILE") or "").strip()
    if not profile:
        profile = {"": "baseline", "gpt-4.1-mini": "baseline", "gpt-6-luna": "luna"}.get((config.get("VAPI_MODEL") or "").strip())
    if profile == "gemini-3.8-flash-custom":
        return "gemini-issued"
    if profile in ("baseline", "luna"):
        return "provider-native"
    raise GatewayError("tool_policy_configuration", 503)


class SignatureStore:
    def __init__(self, path, limits):
        self.path, self.limits = str(path), limits
        with closing(self.connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS gemini_tool_signatures (
                provider_call_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                extras TEXT NOT NULL, group_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                group_size INTEGER NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(provider_call_id,tool_call_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS provider_tool_policies (
                provider_call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                assistant_id TEXT NOT NULL, policy TEXT NOT NULL)""")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def check_live(self, sid, call_id):
        with closing(self.connect()) as db:
            session = db.execute("SELECT ended,created,provider_call_id FROM sessions WHERE id=? AND tenant='cedar-demo'", (sid,)).fetchone()
        if not session or session["ended"] or session["provider_call_id"] != call_id or time.time() - session["created"] > 4 * 3600:
            raise GatewayError("inactive_session", 409)

    def diagnostic_tool_context(self,sid,call_id,tool_id):
        """A best-effort join to existing issuance evidence, never authorization."""
        try:
            with closing(sqlite3.connect(self.path,timeout=.02)) as db:
                row=db.execute("SELECT extras FROM gemini_tool_signatures WHERE session_id=? AND provider_call_id=? AND tool_call_id=?",(sid,call_id,tool_id)).fetchone()
            value=json.loads(row[0]) if row else {}
            context=value.get("__servicevoice_issuance_context",{})
            ordinal=context.get("user_count")
            return {"user_count":ordinal} if type(ordinal) is int and ordinal>0 else {}
        except (sqlite3.Error,ValueError,TypeError,AttributeError):return {}

    def require_gemini_policy(self, sid, call_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT policy FROM provider_tool_policies WHERE provider_call_id=? AND session_id=?", (call_id, sid)).fetchone()
        if not row or row["policy"] != "gemini-issued":
            raise GatewayError("gateway_policy_mismatch", 409)

    def pin_call_policy(self, sid, call_id, assistant_id, config):
        """A call cannot downgrade its policy through overrides or later config edits."""
        def existing(db):
            row = db.execute("SELECT * FROM provider_tool_policies WHERE provider_call_id=? OR session_id=?", (call_id, sid)).fetchone()
            if row and (row["provider_call_id"] != call_id or row["session_id"] != sid or row["assistant_id"] != assistant_id):
                raise GatewayError("tool_policy_binding", 403)
            return row["policy"] if row else None
        with closing(self.connect()) as db:
            policy = existing(db)
            if policy:
                return policy  # Ordinary events and status polls take no write lock.
            db.execute("BEGIN IMMEDIATE")
            policy = existing(db)
            if policy:
                return policy
            policy = configured_tool_policy(config)
            db.execute("INSERT INTO provider_tool_policies VALUES (?,?,?,?)", (call_id, sid, assistant_id, policy))
            db.commit()
            return policy

    @staticmethod
    def _existing_exact_operation(db, sid, tool_id, name, normalized):
        # This is recovery of already admitted work, never permission for a new
        # effect. Compare the same normalized fingerprints as the two owners.
        expected = hashlib.sha256(canonical({"name": name, "args": normalized}).encode()).hexdigest()
        invocation = db.execute("SELECT fingerprint FROM tool_invocations WHERE session_id=? AND call_id=?", (sid, tool_id)).fetchone()
        result = db.execute("SELECT fingerprint FROM tool_results WHERE session_id=? AND call_id=?", (sid, tool_id)).fetchone()
        domain_expected = hashlib.sha256(json.dumps({"name": name, "args": normalized}, sort_keys=True).encode()).hexdigest()
        if invocation and invocation["fingerprint"] != expected or result and result["fingerprint"] != domain_expected:
            return False
        return bool(invocation or result)

    def authorize_tool_batch(self, sid, call_id, calls):
        """Preflight all commands before any effect; this is NOT spoken consent.

        Webhook transport authentication does not prove a tool came from our
        gateway. A signed call may arrive individually or in a reordered batch,
        so execution checks each identity without imposing history group order.
        """
        diagnostic_contexts = {}
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            policy = db.execute("SELECT policy FROM provider_tool_policies WHERE provider_call_id=? AND session_id=?", (call_id, sid)).fetchone()
            if not policy:
                raise GatewayError("tool_policy_missing", 403)
            if policy["policy"] == "provider-native":
                return diagnostic_contexts
            if policy["policy"] != "gemini-issued":
                raise GatewayError("tool_policy_configuration", 503)
            for tool_id, name, normalized, original in calls:
                # Existing journal/receipts remain recoverable after signature
                # expiry or collection. Harness replay cannot execute them anew.
                if self._existing_exact_operation(db, sid, tool_id, name, normalized):
                    continue
                _, _, _, digest = tool_identity({"id": tool_id, "function": {"name": name, "arguments": canonical(original)}}, self.limits)
                row = db.execute("SELECT fingerprint,expires,extras FROM gemini_tool_signatures WHERE provider_call_id=? AND session_id=? AND tool_call_id=?", (call_id, sid, tool_id)).fetchone()
                if not row or row["expires"] < time.time():
                    raise GatewayError("tool_not_issued", 409)
                if row["fingerprint"] != digest:
                    raise GatewayError("tool_issue_mismatch", 409)
                # Project optional observation from this existing authorization
                # read. It cannot authorize a tool, reject a valid operation,
                # or add another database/network hop to the response path.
                try:
                    extras = json.loads(row["extras"])
                    context = extras.get("__servicevoice_issuance_context", {})
                    ordinal = context.get("user_count")
                    if type(ordinal) is int and 1 <= ordinal <= 100_000:
                        diagnostic_contexts[tool_id] = {"user_count": ordinal}
                except (ValueError, TypeError, AttributeError, RecursionError):
                    pass
        return diagnostic_contexts

    def end_call_ready(self, sid):
        """Read durable operations; missing failure receipts remain unresolved.

        A failed journal entry alone is not an authoritative scheduler outcome.
        A proposal alone is not an accepted operation and does not block exit.
        This read is a release gate, not an atomic lease over later webhooks.
        """
        with closing(self.connect()) as db:
            unresolved=db.execute("""SELECT 1 FROM tool_invocations i
                LEFT JOIN tool_results r ON r.session_id=i.session_id AND r.call_id=i.call_id
                WHERE i.session_id=? AND i.status IN ('queued','running','unknown','failed')
                AND r.call_id IS NULL LIMIT 1""",(sid,)).fetchone()
        return unresolved is None

    def save_group(self, sid, call_id, calls, context=None, *, allow_end_call=False):
        from .issuance_context import encode_extras
        group_id, expires = uuid4().hex, time.time() + self.limits.signature_ttl
        records = []
        for index, tool in enumerate(calls):
            tool_id, name, _, digest = tool_identity(tool, self.limits, allow_end_call=allow_end_call)
            if name == END_CALL and len(calls) != 1:
                raise GatewayError("invalid_platform_batch")
            if name == END_CALL and context is None:
                raise GatewayError("platform_context_missing",409)
            extras = tool.get("extra_content", {})
            if not isinstance(extras, dict) or len(canonical(extras)) > self.limits.extra_chars:
                raise GatewayError("invalid_tool_signature")
            try:
                stored_extras = encode_extras(extras, context)
            except ValueError:
                raise GatewayError("invalid_tool_signature") from None
            records.append((call_id, tool_id, sid, digest, canonical(stored_extras), group_id, index, len(calls), expires))
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM gemini_tool_signatures WHERE expires<?", (time.time(),))
                count = db.execute("SELECT COUNT(*) FROM gemini_tool_signatures WHERE provider_call_id=?", (call_id,)).fetchone()[0]
                total = db.execute("SELECT COUNT(*) FROM gemini_tool_signatures").fetchone()[0]
                if count + len(records) > self.limits.signatures_per_call or total + len(records) > self.limits.signatures_total:
                    raise GatewayError("signature_capacity", 429)
                for record in records:
                    if db.execute("SELECT 1 FROM gemini_tool_signatures WHERE provider_call_id=? AND tool_call_id=?", record[:2]).fetchone():
                        raise GatewayError("tool_id_reused", 409)
                db.executemany("INSERT INTO gemini_tool_signatures VALUES (?,?,?,?,?,?,?,?,?)", records)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def restore_group(self, sid, call_id, calls, *, with_context=False, allow_end_call=False):
        from .issuance_context import decode_extras
        if not isinstance(calls, list) or not 1 <= len(calls) <= self.limits.tool_calls:
            raise GatewayError("invalid_tool_history")
        restored, group, ordinals, contexts = [], None, [], []
        with closing(self.connect()) as db:
            for tool in calls:
                tool_id, name, arguments, digest = tool_identity(tool, self.limits, allow_end_call=allow_end_call)
                if name == END_CALL and len(calls) != 1:
                    raise GatewayError("invalid_platform_batch")
                row = db.execute("SELECT * FROM gemini_tool_signatures WHERE provider_call_id=? AND tool_call_id=? AND session_id=?", (call_id, tool_id, sid)).fetchone()
                if not row or row["expires"] < time.time():
                    raise GatewayError("tool_signature_missing", 409)
                if row["fingerprint"] != digest:
                    raise GatewayError("tool_signature_mismatch", 409)
                try:
                    extras, context = decode_extras(parse_json(row["extras"]))
                except ValueError:
                    raise GatewayError("invalid_tool_signature", 409) from None
                supplied = tool.get("extra_content")
                if supplied is not None and canonical(supplied) != canonical(extras):
                    raise GatewayError("tool_signature_mismatch", 409)
                if group is not None and row["group_id"] != group or row["group_size"] != len(calls):
                    raise GatewayError("tool_group_mismatch", 409)
                group = row["group_id"]
                ordinals.append(row["ordinal"])
                contexts.append(context)
                value = {"id": tool_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                if extras:
                    value["extra_content"] = extras
                restored.append(value)
        if ordinals != list(range(len(calls))):
            raise GatewayError("tool_group_mismatch", 409)
        if any(context != contexts[0] for context in contexts):
            raise GatewayError("tool_group_context_mismatch", 409)
        if restored and restored[0]["function"]["name"]==END_CALL and contexts[0] is None:
            raise GatewayError("platform_context_missing",409)
        return (restored, contexts[0]) if with_context else restored


def tool_definitions(requested, *, allow_end_call=False):
    if not isinstance(requested, list) or len(requested) > len(DEFINITIONS) + int(allow_end_call):
        raise GatewayError("invalid_tools")
    names = []
    for entry in requested:
        if not isinstance(entry, dict) or entry.get("type") != "function" or not isinstance(entry.get("function"), dict):
            raise GatewayError("invalid_tools")
        name = entry["function"].get("name")
        if (not isinstance(name, str) or (name not in DEFINITIONS and not (allow_end_call and name == END_CALL))
                or name in names):
            raise GatewayError("invalid_tools")
        names.append(name)
    # Caller-provided schemas/descriptions cannot grant a new tool or behavior.
    tools=[deepcopy(schema) for schema in TOOL_SCHEMAS if schema["function"]["name"] in names]
    if END_CALL in names:tools.append(closing_schema())
    return tools


def render_system_prompt(local_date=None, *, allow_end_call=False):
    """Render one trusted system policy and fresh public facts from one date."""
    local_date = today() if local_date is None else local_date
    facts = dealership_facts(local_date)
    prompt = SYSTEM_PROMPT.replace("{{current_date}}", facts["today"]).replace("{{timezone}}", facts["timezone"])
    if allow_end_call:prompt += "\n" + CLOSING_POLICY
    return prompt + "\n<trusted_dealership_facts>\n" + canonical(facts) + "\n</trusted_dealership_facts>"


def normalize_causal_tool_history(messages, groups):
    """Move verified late tool blocks before later user instructions.

    Vapi can append an old tool/result pair after a newer caller turn. Its
    durable server-issued anchor identifies the exact preceding user prefix.
    Only complete, already authenticated groups enter here. Every group's
    anchor must match, otherwise retain the original order (legacy history and
    changed/merged ASR segments cannot be safely reconstructed).

    This does not authenticate spoken assent or revoke already executed tools.
    No text, user order, opaque provider extras or observed result is rewritten.
    """
    if not groups:
        return messages
    user_indices=[index for index,message in enumerate(messages) if message["role"]=="user"]
    prefixes={count:user_context(messages[:index+1]) for count,index in enumerate(user_indices,1)}
    moves,removed={},set()
    for start,size,context in groups:
        preceding=sum(index<start for index in user_indices)
        if context is None or context!=prefixes.get(context["user_count"]) or context["user_count"]>preceding:
            return messages
        if context["user_count"]==preceding:
            continue
        if messages[start].get("content"):
            # Some provider messages merge a tool call with spoken captions.
            # The user-prefix anchor dates the command, not that prose. Do not
            # move or drop ambiguous speech to reconstruct a command's place.
            return messages
        # The caller's next turn is the boundary, not an inferred timestamp or
        # an untrusted natural-language claim about when an action happened.
        boundary=user_indices[context["user_count"]]
        block=messages[start:start+size+1]
        moves.setdefault(boundary,[]).extend(block)
        removed.update(range(start,start+size+1))
    if not moves:
        return messages
    ordered=[]
    for index,message in enumerate(messages):
        ordered.extend(moves.get(index,[]))
        if index not in removed:
            ordered.append(message)
    return ordered


def prepare_request(body, sid, call_id, store, limits, observations, *, allow_end_call=False):
    if body.get("model") != MODEL or body.get("stream") is not True:
        raise GatewayError("unsupported_model_or_stream")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= limits.messages:
        raise GatewayError("invalid_history")
    cleaned = [{"role": "system", "content": render_system_prompt()}]
    waiting, used_ids, groups, platform_results = set(), set(), [], set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool"):
            raise GatewayError("invalid_history")
        role, content = message["role"], message.get("content")
        if role == "system":
            continue  # Provider/browser overrides never supply gateway policy.
        if waiting and role != "tool":
            raise GatewayError("incomplete_tool_history", 409)
        if content is not None and (not isinstance(content, str) or len(content) > limits.message_chars):
            raise GatewayError("invalid_history")
        value = {"role": role, "content": content or ""}
        if role == "assistant" and message.get("tool_calls") is not None:
            if waiting:
                raise GatewayError("incomplete_tool_history", 409)
            calls, context = store.restore_group(sid, call_id, message["tool_calls"], with_context=True,
                                                allow_end_call=allow_end_call)
            platform=bool(calls and calls[0]["function"]["name"]==END_CALL)
            if not platform:groups.append((len(cleaned),len(calls),context))
            for tool in calls:
                if tool["id"] in used_ids:
                    raise GatewayError("repeated_tool_history", 409)
                used_ids.add(tool["id"])
                (platform_results if platform else waiting).add(tool["id"])
            if platform:
                # Authenticate our own exact signature/session before retaining
                # an explicitly UNKNOWN platform attempt. Never accept a caller-
                # supplied platform result as scheduler evidence. No synthetic
                # success/failure tool result is inserted into model history.
                value["content"]=UNKNOWN_ATTEMPT_CONTEXT
            else:value["tool_calls"] = calls
        elif message.get("tool_calls") is not None:
            raise GatewayError("invalid_tool_history")
        if role == "tool":
            tool_id = message.get("tool_call_id")
            if allow_end_call and isinstance(tool_id,str) and tool_id in platform_results:
                platform_results.remove(tool_id)
                continue  # Its outcome is unknown even if this raw text claims success.
            if not isinstance(tool_id, str) or tool_id not in waiting:
                raise GatewayError("orphan_tool_result", 409)
            waiting.remove(tool_id)
            value["tool_call_id"] = tool_id
            # Vapi add-message accepts tool history from the browser. A valid
            # provider bearer does not authenticate the content of each message.
            authoritative = observations.lookup(sid, call_id, tool_id, content)
            value["content"] = canonical(authoritative)
        elif waiting and role in ("system", "user"):
            raise GatewayError("incomplete_tool_history", 409)
        cleaned.append(value)
    if len(cleaned) == 1:
        raise GatewayError("invalid_history")
    if waiting:
        raise GatewayError("incomplete_tool_history", 409)
    cleaned=normalize_causal_tool_history(cleaned,groups)
    tools = tool_definitions(body.get("tools", []), allow_end_call=allow_end_call)
    has_closing=any(tool["function"]["name"]==END_CALL for tool in tools)
    if has_closing:
        cleaned[0]["content"]=render_system_prompt(allow_end_call=True)
        if not store.end_call_ready(sid):
            tools=[tool for tool in tools if tool["function"]["name"]!=END_CALL]
    result = {"model": MODEL, "reasoning_effort": "low", "stream": True,
              "stream_options": {"include_usage": True}, "messages": cleaned, "max_tokens": MAX_OUTPUT_TOKENS}
    if tools:
        result.update({"tools": tools, "tool_choice": "auto"})
    return result


def merge_extras(target, incoming):
    if not isinstance(incoming, dict):
        raise GatewayError("invalid_tool_signature")
    for key, value in incoming.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            merge_extras(target[key], value)
        elif key in target and target[key] != value:
            raise GatewayError("conflicting_tool_signature")
        else:
            target[key] = deepcopy(value)


class ToolAssembler:
    def __init__(self, limits, *, allow_end_call=False):
        self.limits, self.parts, self.allow_end_call = limits, {}, allow_end_call
        self.complete_indices = set()

    def _index(self, fragment):
        if "index" in fragment:
            index = fragment["index"]
            if type(index) is not int or not 0 <= index < self.limits.tool_calls:
                raise GatewayError("invalid_upstream_tools")
            return index, False
        # Google's compatibility stream can emit an entire identified function
        # without an index. Never guess the target of an unidentified fragment.
        tool_id, _, _, _ = tool_identity(fragment, self.limits, allow_end_call=self.allow_end_call)
        matches = [index for index, item in self.parts.items() if item["id"] == tool_id]
        if matches:
            index = matches[0]
            if self.parts[index]["function"] != fragment["function"]:
                raise GatewayError("ambiguous_unindexed_tool")
            return index, True
        if sorted(self.parts) != list(range(len(self.parts))) or any(item["id"] is None for item in self.parts.values()):
            raise GatewayError("ambiguous_unindexed_tool")
        if len(self.parts) >= self.limits.tool_calls:
            raise GatewayError("upstream_tool_limit")
        return len(self.parts), False

    def add(self, fragments):
        if not isinstance(fragments, list):
            raise GatewayError("invalid_upstream_tools")
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise GatewayError("invalid_upstream_tools")
            index, repeated_complete = self._index(fragment)
            item = self.parts.setdefault(index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}, "extra_content": {}})
            if "id" in fragment:
                if not isinstance(fragment["id"], str) or item["id"] not in (None, fragment["id"]):
                    raise GatewayError("invalid_upstream_tool_id")
                item["id"] = fragment["id"]
            if fragment.get("type", "function") != "function":
                raise GatewayError("invalid_upstream_tools")
            function = fragment.get("function", {})
            if not isinstance(function, dict):
                raise GatewayError("invalid_upstream_tools")
            if index in self.complete_indices and not repeated_complete and any(function.get(key) for key in ("name", "arguments")):
                raise GatewayError("ambiguous_unindexed_tool")
            for key in ("name", "arguments"):
                if key in function:
                    if not isinstance(function[key], str):
                        raise GatewayError("invalid_upstream_tools")
                    if not repeated_complete:
                        item["function"][key] += function[key]
            if "index" not in fragment:
                self.complete_indices.add(index)
            if len(item["function"]["name"]) > 64 or len(item["function"]["arguments"]) > self.limits.argument_chars:
                raise GatewayError("upstream_tool_size")
            if "extra_content" in fragment:
                merge_extras(item["extra_content"], fragment["extra_content"])
            if len(canonical(item["extra_content"])) > self.limits.extra_chars:
                raise GatewayError("upstream_tool_size")

    def finish(self):
        if sorted(self.parts) != list(range(len(self.parts))):
            raise GatewayError("incomplete_upstream_tools")
        calls = [self.parts[index] for index in sorted(self.parts)]
        ids = set()
        for tool in calls:
            tool_id, name, _, _ = tool_identity(tool, self.limits, allow_end_call=self.allow_end_call)
            if name == END_CALL and len(calls) != 1:
                raise GatewayError("invalid_platform_batch")
            if tool_id in ids:
                raise GatewayError("duplicate_upstream_tool_id")
            ids.add(tool_id)
        # Gemini parallel groups sign their first call; later calls can be unsigned.
        first_extra = calls[0].get("extra_content", {}).get("google", {}) if calls else {}
        if not isinstance(first_extra, dict) or not isinstance(first_extra.get("thought_signature"), str) or not first_extra["thought_signature"]:
            raise GatewayError("upstream_signature_missing")
        return calls


def sse(value):
    return ("data: " + json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n\n").encode()


async def sse_events(response, limits, transport=None):
    transport = transport if transport is not None else {"iterator": response.aiter_bytes(), "buffer_bytes": 0, "received_bytes": 0}
    lines, buffer, event_size, total, count = [], b"", 0, 0, 0
    async for part in transport["iterator"]:
        total += len(part)
        transport["received_bytes"] = total
        if total > limits.response_bytes:
            raise GatewayError("upstream_response_size")
        buffer += part
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            transport["buffer_bytes"] = len(buffer)
            event_size += len(raw) + 1
            if event_size > limits.event_bytes:
                raise GatewayError("upstream_response_size")
            try:
                line = raw.removesuffix(b"\r").decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GatewayError("invalid_upstream_encoding") from exc
            if line == "":
                if lines:
                    count += 1
                    if count > 2048:
                        raise GatewayError("upstream_event_limit")
                    yield "\n".join(lines)
                lines, event_size = [], 0
            elif line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))
        if event_size + len(buffer) > limits.event_bytes:
            raise GatewayError("upstream_response_size")
    if lines or buffer:
        raise GatewayError("incomplete_upstream_event")


async def drain_sse_tail(transport, limits, timing, remaining):
    """Optionally reach HTTP EOF after valid DONE so HTTP/1.1 can be reused.

    Extra bytes are discarded, never parsed or forwarded. Failure preserves the
    prior valid-DONE semantics and leaves cleanup to close the unreusable stream.
    The original generation deadline still controls all subsequent work.
    """
    began = time.monotonic()
    consumed = transport["buffer_bytes"]
    outcome = "interrupted"
    try:
        if remaining <= 0:
            outcome = "budget_exhausted"
            raise TimeoutError()
        if consumed > EOF_DRAIN_BYTES:
            outcome = "size_limit"
        if consumed <= EOF_DRAIN_BYTES:
            try:
                async with asyncio.timeout(min(EOF_DRAIN_SECONDS, remaining)):
                    async for part in transport["iterator"]:
                        consumed += len(part)
                        transport["received_bytes"] += len(part)
                        if consumed > EOF_DRAIN_BYTES or transport["received_bytes"] > limits.response_bytes:
                            outcome = "size_limit"
                            break
                    else:
                        outcome = "eof"
            except TimeoutError:
                outcome = "timeout"
            except (httpx.HTTPError, OSError):
                outcome = "read_error"
    finally:
        timing.row["durations_ms"]["upstream_eof_drain"] = round((time.monotonic() - began) * 1000, 3)
        timing.row["counts"]["eof_tail_bytes"] = consumed
        timing.row["eof_drain_outcome"] = outcome


class GeminiGateway:
    def __init__(self, app, config_loader, bind_call, *, client=None, limits=None):
        self.limits = limits or GatewayLimits()
        self.store = SignatureStore(app.state.store.path, self.limits)
        self.observations = ToolObservationStore(app.state.store.path)
        self.config_loader, self.bind_call = config_loader, bind_call
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5), follow_redirects=False,
            limits=httpx.Limits(max_connections=self.limits.concurrency,
                               max_keepalive_connections=self.limits.concurrency, keepalive_expiry=60),
        )
        self.active, self.closing = set(), False
        self.owners, self.successors, self.latest_sequences = {}, {}, {}
        self.request_sequence = 0
        self.admission = asyncio.Lock()
        self.timings = deque(maxlen=TIMING_CAPACITY)
        self.narration = BoundedSelector(app.state.store,capacity=self.limits.concurrency)
        self.faq = JevFaqRouter(app.state.store,read_capacity=self.limits.concurrency,
                               connection_capacity=self.limits.concurrency if self.limits.concurrency>4 else 2)
        self.telemetry = None
        self.timing_sink = None
        self.diagnostics = None
        self._wal_anchor = None
        self._wal_anchor_startup_task = None
        self.wal_anchor_status = "disabled"
        self.backchannel_policy = BackchannelPolicy(connection_capacity=self.limits.concurrency)
        self.backchannel_budget = BackchannelBudget()
        self.backchannel_cache_ready = False
        self.backchannel_reads = set()
        # Installed only by the isolated conference lane. Other assistants keep
        # their existing transport; timing checks never authorize business work.
        self.waiting_cue_state = None
        self.closing_ready = None
        self.waiting_neutral_only = None
        self.waiting_cue_commit = None
        self.waiting_cue_assistant = None
        self.before_answer = None
        self.waiting_fallback_ready = None
        self.waiting_content_finished = None
        self.waiting_cue_track = None

    def _observe_timing(self,row,sid,context=None):
        if self.timing_sink is not None:
            try:self.timing_sink.record(row)
            except Exception:pass  # Diagnostics must never delay or fail a booking.
        if self.diagnostics is not None and context is not None:
            try:
                call_id=context["call_id"];turn_id=context.get("turn_id")
                def event(component,event_name,duration,attributes,phase_started=None,phase_outcome=None):
                    self.diagnostics.record(component,event_name,
                        phase_outcome or ("success" if row["outcome"] in {"completed","tool_emitted","narrated"} else "superseded" if row["outcome"]=="superseded" else "cancelled" if row["outcome"]=="cancelled" else "failure"),
                        call_id=call_id,turn_id=turn_id,request_id=row["request_id"],
                        start_unix=phase_started or row["started_at_unix"],duration_ms=duration,attributes=attributes)
                event("gateway","request_finished",row["milestones_ms"].get("total"),{
                    "route":row["route"],"model":row.get("model", MODEL),"user_ordinal":context.get("user_count"),
                    "first_text_ms":row["milestones_ms"].get("first_text"),"preflight_ms":row["milestones_ms"].get("preflight"),
                    "input_tokens":row["usage"].get("input_tokens"),"output_tokens":row["usage"].get("output_tokens"),
                    "total_tokens":row["usage"].get("total_tokens"),"evidence_source":"server_observed",
                    **{key:row.get(key) for key in ("generation_id","generation_sequence","generation_sequence_scope",
                        "predecessor_generation_id","predecessor_relation","superseded_by_generation_id",
                        "generation_terminal_state","supersession_cause","superseded_at_ms",
                        "payload_released_at_ms","payload_release_scope","downstream_disconnect_at_ms",
                        "checkpoint_rejections","sse_response_id","neutral_sse_response_id")}})
                if context.get("llm_started_at") is not None:
                    llm_end=row["milestones_ms"].get("upstream_done")
                    if llm_end is None:llm_end=row["milestones_ms"]["total"]
                    event("llm","generation_finished",max(0,(row["started_at_unix"]+llm_end/1000-context["llm_started_at"])*1000),{
                        "model":row.get("model", MODEL),"input_tokens":row["usage"].get("input_tokens"),"output_tokens":row["usage"].get("output_tokens"),"evidence_source":"server_observed"},context["llm_started_at"])
                if row["counts"].get("jev_requests"):
                    reason=row.get("jev_reason","")
                    failed=reason=="classification_deadline" or reason.startswith("classifier_")
                    code="timeout" if reason in {"classification_deadline","classifier_timeout"} else "upstream_unavailable" if failed else "none"
                    event("faq_router","classification_finished",row["durations_ms"].get("jev_route"),{
                        "route":row["route"],"error_code":code,"evidence_source":"server_observed"},
                        context.get("faq_started_at"),"failure" if failed else "success")
                if row["route"]=="deterministic_receipt":
                    event("receipt_renderer","render_finished",context.get("receipt_elapsed_ms"),{
                        "route":row["route"],"source_result_sha256":row.get("source_result_sha256"),
                        "output_text_sha256":row.get("output_text_sha256"),"evidence_source":"server_observed"},context.get("receipt_started_at"))
            except Exception:pass
        if self.telemetry is None:return
        attributes={"route":row["route"],"outcome":row["outcome"],"model":row.get("model", MODEL),
                    "prompt_sha256":row["system_prompt_sha256"],"prompt_version":SYSTEM_PROMPT_VERSION,
                    "narrator_version":row["narrator_version"]}
        for key in ("generation_id","generation_sequence","generation_sequence_scope","predecessor_generation_id",
                "predecessor_relation","superseded_by_generation_id","supersession_cause","superseded_at_ms",
                "payload_released_at_ms","payload_release_scope","downstream_disconnect_at_ms",
                "checkpoint_rejections","generation_terminal_state","sse_response_id","neutral_sse_response_id"):
            attributes[key]=row.get(key)
        for key,value in row["milestones_ms"].items():attributes[key+"_ms"]=value
        for key,value in row["durations_ms"].items():attributes[key+"_ms"]=value
        for key,value in row["counts"].items():attributes[key]=value
        for key,value in row["usage"].items():
            if type(value) is int:attributes[key]=value
        self.telemetry.record_event("gateway.request",attributes,correlation=sid,
            start_unix=row["started_at_unix"],duration_ms=row["milestones_ms"]["total"])

    def latency_snapshot(self, call_id=None):
        # Internal callers may inspect all rows. HTTP access must supply the
        # call ID derived from an authorized session, never a caller query ID.
        call_key = hashlib.sha256(call_id.encode()).hexdigest()[:16] if call_id is not None else None
        return deepcopy([row for row in self.timings if call_key is None or row["call_key"] == call_key])

    def _open_wal_anchor(self):
        """Keep WAL sidecars available to a separate read-only reviewer mount.

        This connection is itself read-only. The business process already owns
        the writable directory, so SQLite can initialize shared-memory sidecars.
        No BEGIN or cursor survives the probe: this is not a retained snapshot,
        does not pin a WAL checkpoint, and never changes journal mode.
        """
        connection=sqlite3.connect(Path(self.store.path).resolve().as_uri()+"?mode=ro",uri=True,
            isolation_level=None,check_same_thread=False,timeout=.2)
        try:
            with closing(connection.execute("PRAGMA query_only=ON")) as cursor:cursor.fetchall()
            with closing(connection.execute("PRAGMA journal_mode")) as cursor:
                mode=cursor.fetchone()[0]
            if mode!="wal":
                connection.close()
                return None
            with closing(connection.execute("SELECT name FROM sqlite_schema LIMIT 1")) as cursor:cursor.fetchall()
            if connection.in_transaction:
                raise sqlite3.OperationalError("unexpected_read_transaction")
            return connection
        except BaseException:
            connection.close()
            raise

    async def _initialize_wal_anchor(self):
        try:
            connection=await run_in_threadpool(self._open_wal_anchor)
            if self.closing:
                if connection is not None:await run_in_threadpool(connection.close)
                self.wal_anchor_status="closed"
                return False
            self._wal_anchor=connection
            self.wal_anchor_status="ready" if connection is not None else "not_wal"
            return connection is not None
        except Exception:
            self.wal_anchor_status="unavailable"
            # An explicitly enabled observation path must not silently appear
            # ready. Do not include SQLite errors, file paths or credentials.
            raise RuntimeError("gateway_wal_anchor_unavailable") from None

    async def start(self):
        if self.closing:return False
        config=await run_in_threadpool(self.config_loader)
        if self.closing:return False
        if config.get("SERVICEVOICE_TIMING_PERSISTENCE","off")=="on":
            if self._wal_anchor_startup_task is None:
                self._wal_anchor_startup_task=asyncio.create_task(self._initialize_wal_anchor())
                # A cancelled lifespan waiter does not abandon a created SQLite
                # connection. close() also handles a late initializer safely.
                self._wal_anchor_startup_task.add_done_callback(
                    lambda task:None if task.cancelled() else task.exception())
            await asyncio.shield(self._wal_anchor_startup_task)
        if config.get("SERVICEVOICE_BACKCHANNELS") == "on":
            if not isinstance(config.get("VAPI_BACKCHANNEL_ASSISTANT_ID"),str) or not config["VAPI_BACKCHANNEL_ASSISTANT_ID"]:
                raise RuntimeError("backchannel_candidate_identity_required")
            def warm_cache():
                from .backchannel_audio import BackchannelAudioCache, AudioSpec
                path = Path(config.get("SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST", ""))
                cache = BackchannelAudioCache(path.parent, AudioSpec(
                    model=config.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
                    voice=config.get("GEMINI_TTS_VOICE", "Kore")))
                cache.load_manifest(path)
                return all(cache.lookup_pcm(PHRASES[p], 16000) is not None
                           for p in ("neutral_ack", "acknowledge", *WAITING_PHRASE_IDS))
            self.backchannel_cache_ready = await run_in_threadpool(warm_cache)
            if not self.backchannel_cache_ready:raise RuntimeError("backchannel_cache_not_ready")
            await self.backchannel_policy.start()
        return await self.faq.start()

    async def _close_wal_anchor(self):
        task=self._wal_anchor_startup_task
        if task is not None and not task.done():
            with suppress(TimeoutError,RuntimeError):
                await asyncio.wait_for(asyncio.shield(task),timeout=1)
        connection,self._wal_anchor=self._wal_anchor,None
        if connection is not None:await run_in_threadpool(connection.close)
        if task is not None:self.wal_anchor_status="closed"

    async def close(self):
        self.closing = True
        self.faq.closing = True
        for successor in list(self.successors.values()):
            self._supersede(successor)
        for owner in list(self.owners.values()):
            self._supersede(owner)
        try:
            await self.client.aclose()
        finally:
            try:await self.faq.close()
            finally:
                try:await self.backchannel_policy.close()
                finally:await self._close_wal_anchor()

    def _release(self, call_id, timing=None):
        # Event-loop-local, synchronous removal cannot be interrupted midway.
        # The relay and response owner both clean up. An old response's second
        # cleanup must not release a newer generation admitted for the same call.
        if timing is not None:
            if timing.admission_released:
                return
            timing.admission_released = True
        generation = timing.generation if timing is not None else None
        if generation is not None:
            generation.task = None
            generation.released.set()
            if self.owners.get(call_id) is generation:
                self.owners.pop(call_id, None)
            if self.successors.get(call_id) is generation:
                self.successors.pop(call_id, None)
        if call_id not in self.owners and call_id not in self.successors:
            self.active.discard(call_id)

    def _supersede(self, generation, *, replacement=None):
        if generation.superseded.is_set():
            return
        generation.timing.note_superseded(replacement.timing.row["generation_id"] if replacement is not None else None,
            "newer_local_http_arrival" if replacement is not None else "gateway_shutdown" if self.closing else "unknown")
        generation.superseded.set()
        task = generation.task
        if task is not None and not task.done():
            if not task.cancelling():task.cancel()
        elif self.owners.get(generation.call_id) is generation:
            # Covers handle -> unstarted OwnedStream, or a downstream send.
            # No model work is running in this phase. Retain the lease until
            # its upstream response really closes, even if cleanup is slow.
            if generation.response is not None:
                self._start_owned_cleanup(generation.response, generation)
            else:
                generation.timing.outcome = "superseded"
                self._release(generation.call_id, generation.timing)
                generation.timing.finish()

    def _obsolete(self, timing):
        return timing.generation is not None and timing.generation.superseded.is_set()

    def _checkpoint(self, timing):
        generation = timing.generation
        if generation is not None and (generation.superseded.is_set() or self.owners.get(generation.call_id) is not generation):
            if not timing.finished:timing.row["checkpoint_rejections"]+=1
            raise GenerationSuperseded()

    def _phase(self, timing, active):
        generation = timing.generation
        if generation is not None:
            if active:
                self._checkpoint(timing)
                generation.task = asyncio.current_task()
            elif generation.task is asyncio.current_task():
                generation.task = None

    def _payload(self, timing):
        self._checkpoint(timing)
        timing.note_time("payload_released_at_ms")
        if timing.generation is not None:
            timing.generation.payload_emitted = True
        # Never cancel an ASGI send outside our generator's exception handler.
        # After this point this payload has escaped; supersession cannot revoke
        # an already emitted command or prove whether the caller heard text.
        self._phase(timing, False)

    async def _neutral_stream(self, request, timing=None):
        if await request.is_disconnected():
            return
        response_id=(timing.row.get("sse_response_id") if timing is not None
                     and timing.row.get("backchannel",{}).get("emitted_ms") is not None else None) or "chatcmpl-"+uuid4().hex
        if timing is not None and not timing.finished:timing.row["neutral_sse_response_id"]=response_id
        yield sse({"id":response_id,"object":"chat.completion.chunk",
                   "created":int(time.time()),"model":MODEL,
                   "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
        yield b"data: [DONE]\n\n"

    def _neutral_response(self, request, call_id, timing):
        return OwnedStream(self._neutral_stream(request,timing), lambda:self._cleanup_narration(call_id,timing))

    async def _admit(self, request, call_id, timing, sequence):
        generation = Generation(call_id, sequence, timing)
        timing.generation = generation
        async with self.admission:
            moment = time.monotonic()
            # Expired requests cannot displace newer work. Recent sequence
            # records outlive every older request's total budget, then expire.
            self.latest_sequences = {key:value for key,value in self.latest_sequences.items() if value[1]>moment}
            prior = self.latest_sequences.get(call_id)
            if prior is not None and sequence<prior[0]:
                timing.note_superseded(prior[2] if len(prior)>2 else None,"newer_local_http_arrival")
                generation.superseded.set()
                timing.outcome="superseded"
                return False
            if moment-timing.started >= self.limits.total_seconds:
                timing.outcome="upstream_unavailable";timing.finish()
                raise HTTPException(504,"Gemini request unavailable.")
            if self.closing:
                timing.outcome="capacity_rejected";timing.finish()
                raise HTTPException(429,"Gateway capacity unavailable.")
            owner = self.owners.get(call_id)
            previous = self.successors.get(call_id)
            predecessor=previous or owner
            if predecessor is not None:
                timing.row["predecessor_generation_id"]=predecessor.timing.row["generation_id"]
                timing.row["predecessor_relation"]="queued_successor" if previous is not None else "active_or_draining_owner"
            if owner is None and previous is None and len(self.active)>=self.limits.concurrency:
                timing.outcome="capacity_rejected";timing.finish()
                raise HTTPException(429,"Gateway capacity unavailable.")
            if call_id not in self.latest_sequences and len(self.latest_sequences)>=self.limits.signatures_total:
                timing.outcome="capacity_rejected";timing.finish()
                raise HTTPException(429,"Gateway admission metadata capacity unavailable.")
            self.latest_sequences[call_id]=(sequence,timing.started+self.limits.total_seconds+self.limits.cleanup_seconds,
                                            timing.row["generation_id"])
            if previous is not None:
                self._supersede(previous,replacement=generation)
            if owner is None:
                self.successors.pop(call_id,None)
                self.owners[call_id]=generation
                self.active.add(call_id)
                self._phase(timing,True)
                return True
            self.successors[call_id]=generation
            self._supersede(owner,replacement=generation)

        # Keep the actual running/draining owner's barrier, never a superseded
        # waiter's barrier. A -> B -> C must still wait for A to really finish.
        released = asyncio.create_task(owner.released.wait())
        obsolete = asyncio.create_task(generation.superseded.wait())
        disconnected = asyncio.Event()
        watcher = asyncio.create_task(self._watch_disconnect(request,asyncio.current_task(),disconnected,generation))
        try:
            remaining = self.limits.total_seconds-(time.monotonic()-timing.started)
            done,_=await asyncio.wait((released,obsolete),timeout=max(0,min(self.limits.supersession_seconds,remaining)),return_when=asyncio.FIRST_COMPLETED)
            async with self.admission:
                if self.closing or obsolete in done or generation.superseded.is_set() or self.successors.get(call_id) is not generation:
                    generation.superseded.set()
                    timing.outcome="superseded"
                    return False
                if not owner.released.is_set():
                    timing.note_superseded(cause="predecessor_cleanup_wait_expired")
                    generation.superseded.set()
                    timing.outcome="supersession_wait_expired"
                    return False
                self.successors.pop(call_id,None)
                self.owners[call_id]=generation
                self.active.add(call_id)
                self._phase(timing,True)
                return True
        finally:
            disconnected.set();watcher.cancel()
            for task in (released,obsolete):task.cancel()
            if self.owners.get(call_id) is not generation:
                if self.successors.get(call_id) is generation:
                    self.successors.pop(call_id,None)
                if call_id not in self.owners and call_id not in self.successors:
                    self.active.discard(call_id)
            await asyncio.gather(watcher,released,obsolete,return_exceptions=True)

    def _start_owned_cleanup(self, response, generation):
        if generation.cleanup_task is None:
            async def finish():
                try:
                    if not response.is_closed:
                        with suppress(httpx.HTTPError,OSError,RuntimeError):
                            await response.aclose()
                finally:
                    if generation.superseded.is_set():
                        generation.timing.outcome="superseded_after_release" if generation.payload_emitted else "superseded"
                    self._release(generation.call_id,generation.timing)
                    generation.timing.finish()
            generation.cleanup_task=asyncio.create_task(finish())
        return generation.cleanup_task

    async def _cleanup(self, response, call_id, timing=None):
        if timing is not None and timing.generation is not None:
            task=self._start_owned_cleanup(response,timing.generation)
            # A timeout stops waiting, not cleanup. Its physical upstream lease
            # remains occupied until the real close finishes.
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task),timeout=self.limits.cleanup_seconds)
            return
        try:
            if not response.is_closed:
                with suppress(httpx.HTTPError, OSError, RuntimeError, TimeoutError):
                    await asyncio.wait_for(response.aclose(), timeout=self.limits.cleanup_seconds)
        finally:
            self._release(call_id, timing)
            if timing is not None:
                timing.finish()

    async def _watch_disconnect(self, request, owner, stop, generation=None):
        while not stop.is_set():
            if await request.is_disconnected():
                if generation is not None:
                    generation.disconnected=True
                    generation.timing.note_time("downstream_disconnect_at_ms")
                if not owner.cancelling():owner.cancel()
                return
            await asyncio.sleep(0.1)

    async def handle(self, request: Request):
        received_at = time.monotonic()
        self.request_sequence += 1
        sequence = self.request_sequence
        config = await run_in_threadpool(self.config_loader)
        secret, key = config.get("GEMINI_GATEWAY_SECRET") or "", config.get("GEMINI_API_KEY") or ""
        if not isinstance(secret, str) or len(secret) < 32 or not isinstance(key, str) or not key:
            raise HTTPException(503, "Gemini gateway is not configured.")
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode("utf-8"), ("Bearer " + secret).encode("utf-8")):
            raise HTTPException(401, "Invalid gateway authentication.")
        body = await read_json_body(request)
        if not isinstance(body, dict):
            raise HTTPException(422, "Invalid request object.")
        call = body.get("call")
        sid = await run_in_threadpool(self.bind_call, call, body.get("assistant"))
        call_id = call["id"]
        request.state.diagnostic_call_id=call_id
        diagnostic_context={"call_id":call_id, "assistant_id":call.get("assistantId")}
        expected_backchannel_assistant = config.get("VAPI_BACKCHANNEL_ASSISTANT_ID")
        diagnostic_context["backchannel_enabled"] = (
            config.get("SERVICEVOICE_BACKCHANNELS") == "on"
            and isinstance(call.get("assistantId"), str) and bool(call.get("assistantId"))
            and (call.get("assistantId") == expected_backchannel_assistant
                 or call.get("assistantId") == self.waiting_cue_assistant))
        timing = RequestTiming(call_id, received_at, self.timings,
            lambda row,session:self._observe_timing(row,session,diagnostic_context),sid)
        timing.row["generation_sequence"]=sequence
        timing.diagnostic_context=diagnostic_context
        try:
            # A configured native/default call must never receive custom Gemini
            # output while retaining the weaker provider-native execution gate.
            await run_in_threadpool(self.store.require_gemini_policy, sid, call_id)
            await run_in_threadpool(self.store.check_live, sid, call_id)
            upstream = await run_in_threadpool(prepare_request, body, sid, call_id, self.store, self.limits, self.observations,
                                               **({"allow_end_call":True} if closing_enabled(config) else {}))
            selected_model = MODEL
            if self.waiting_cue_assistant and call.get("assistantId") == self.waiting_cue_assistant:
                diagnostic_context["serial_tools"] = True
                upstream["messages"][0]["content"] += SERIAL_TOOL_POLICY + PHONE_CONVERSATION_POLICY + PHONE_PRESENTATION_POLICY
                selected_model = config.get("SERVICEVOICE_TWILIO_MODEL", MODEL)
                if selected_model not in UPSTREAM_MODELS:
                    raise GatewayError("candidate_model_not_allowed", 503)
            upstream["model"] = selected_model
            upstream["reasoning_effort"] = UPSTREAM_MODELS[selected_model]
            timing.row["model"] = selected_model
            timing.row["reasoning_effort"] = UPSTREAM_MODELS[selected_model]
        except (GatewayError, ObservationError) as exc:
            timing.outcome = "rejected"
            timing.finish()
            raise HTTPException(exc.status, exc.code) from None
        except sqlite3.Error:
            timing.outcome = "state_unavailable"
            timing.finish()
            raise HTTPException(503, "Gateway state unavailable.") from None
        timing.mark("preflight")
        context=user_context(upstream["messages"])
        if context:
            diagnostic_context.update(user_count=context["user_count"],turn_id=turn_identifier(call_id,context["user_count"]))
        timing.row["system_prompt_sha256"] = hashlib.sha256(upstream["messages"][0]["content"].encode("utf-8")).hexdigest()
        timing.row["counts"]["history_messages"] = len(upstream["messages"])
        timing.row["counts"]["history_tool_results"] = sum(message["role"] == "tool" for message in upstream["messages"])
        try:
            admitted=await self._admit(request,call_id,timing,sequence)
        except asyncio.CancelledError:
            neutral = self._obsolete(timing) and not timing.generation.disconnected
            timing.outcome="superseded" if neutral else "cancelled"
            await self._cleanup_narration(call_id,timing)
            if neutral:return self._neutral_response(request,call_id,timing)
            raise
        if not admitted:
            return self._neutral_response(request,call_id,timing)
        try:
            self._phase(timing,True)
            # A terminal event may have won while this successor waited.
            await run_in_threadpool(self.store.check_live,sid,call_id)
            self._checkpoint(timing)
            return await self._generate(request,config,key,upstream,sid,call_id,received_at,timing)
        except (asyncio.CancelledError,GenerationSuperseded):
            if self._obsolete(timing) and not timing.generation.disconnected:
                timing.outcome="superseded"
                await self._cleanup_narration(call_id,timing)
                return self._neutral_response(request,call_id,timing)
            timing.outcome="cancelled"
            await self._cleanup_narration(call_id,timing)
            raise
        except GatewayError as exc:
            timing.outcome="rejected"
            await self._cleanup_narration(call_id,timing)
            raise HTTPException(exc.status,exc.code) from None
        except sqlite3.Error:
            timing.outcome="state_unavailable"
            await self._cleanup_narration(call_id,timing)
            raise HTTPException(503,"Gateway state unavailable.") from None
        except BaseException:
            await self._cleanup_narration(call_id,timing)
            raise
        finally:
            self._phase(timing,False)

    async def _generate(self,request,config,key,upstream,sid,call_id,started,timing):
        if (self.waiting_cue_assistant and timing.diagnostic_context.get("assistant_id")==self.waiting_cue_assistant
                and closing_enabled(config) and any(t["function"]["name"]==END_CALL for t in upstream.get("tools",[]))):
            from .phone_closing import candidate as closing_candidate
            closing = closing_candidate(upstream["messages"])
            if closing and self.closing_ready is not None and self.closing_ready(call_id,sid,closing):
                state=await run_in_threadpool(self._backchannel_context,sid,call_id)
                self._checkpoint(timing)
                if (state and state.get("allow_turn") is True and state.get("has_pending_proposal") is False
                        and state.get("has_pending_operation") is False and state.get("ended") is False
                        and await run_in_threadpool(self.store.end_call_ready,sid)
                        and self.closing_ready(call_id,sid,closing)):
                    timing.row["route"]="deterministic_closing"
                    return OwnedStream(self._close_phone(request,upstream,sid,call_id,timing,closing),
                        lambda:self._cleanup_narration(call_id,timing))
        if config.get("SERVICEVOICE_RECEIPT_NARRATION") == "on":
            try:
                timing.diagnostic_context["receipt_started_at"]=time.time()
                receipt_started=time.monotonic()
                decision = await self.narration.choose(sid, call_id, upstream["messages"],
                    **({"phone":True} if self.waiting_cue_assistant and timing.diagnostic_context.get("assistant_id")==self.waiting_cue_assistant else {}))
                timing.diagnostic_context["receipt_elapsed_ms"]=(time.monotonic()-receipt_started)*1000
                self._checkpoint(timing)
                timing.row["narration_reason"] = decision.reason
                if decision.text is not None:
                    timing.row["route"] = "deterministic_receipt"
                    timing.row["source_result_sha256"] = decision.result_sha256
                    return OwnedStream(self._narrate(request, decision.text, timing),
                                       lambda: self._cleanup_narration(call_id, timing))
            except BaseException as exc:
                timing.outcome = "superseded" if self._obsolete(timing) else "cancelled" if isinstance(exc, asyncio.CancelledError) else "state_unavailable"
                await self._cleanup_narration(call_id, timing)
                raise
        if config.get("SERVICEVOICE_JEV_FAQ") == "on":
            faq_started = time.monotonic()
            timing.diagnostic_context["faq_started_at"]=time.time()
            watch_stop = asyncio.Event()
            watcher = asyncio.create_task(self._watch_disconnect(request, asyncio.current_task(), watch_stop,timing.generation))
            try:
                def classifier_started():timing.row["counts"]["jev_requests"] = 1
                decision = await self.faq.choose(sid, call_id, upstream["messages"], config.get("TYPESAFE_API_KEY"), classifier_started)
                self._checkpoint(timing)
                timing.row["jev_reason"] = decision.reason
                timing.row["durations_ms"].update(jev_route=decision.elapsed_ms, jev_classification=decision.classification_ms)
                timing.row["counts"]["jev_requests"] = decision.requests
                timing.row.update(jev_probability=decision.probability, jev_margin=decision.margin,
                                  jev_language_probability=decision.language_probability)
                if decision.text is not None:
                    timing.row["route"] = "jev_static_faq"
                    return OwnedStream(self._narrate(request, decision.text, timing, "servicevoice-jev-faq"),
                                       lambda: self._cleanup_narration(call_id, timing))
            except BaseException as exc:
                timing.outcome = "superseded" if self._obsolete(timing) else "cancelled" if isinstance(exc, asyncio.CancelledError) else "state_unavailable"
                timing.row["jev_reason"] = timing.outcome
                timing.row["durations_ms"]["jev_route"] = round((time.monotonic()-faq_started)*1000, 3)
                await self._cleanup_narration(call_id, timing)
                raise
            finally:
                watch_stop.set()
                watcher.cancel()
                with suppress(asyncio.CancelledError):await watcher
        if timing.diagnostic_context.get("backchannel_enabled") and self.backchannel_cache_ready:
            return OwnedStream(self._backchannel_stream(request,config,key,upstream,sid,call_id,started,timing),
                               lambda:self._cleanup_backchannel(call_id,timing))
        return await self._generate_upstream(request,key,upstream,sid,call_id,started,timing)

    async def _generate_upstream(self,request,key,upstream,sid,call_id,started,timing):
        response = None
        watch_stop = asyncio.Event()
        watcher = asyncio.create_task(self._watch_disconnect(request, asyncio.current_task(), watch_stop,timing.generation))
        try:
            async with asyncio.timeout(max(0.001, self.limits.total_seconds-(time.monotonic()-started))):
                provider_request = self.client.build_request("POST", GOOGLE_URL, json=upstream, headers={"Authorization": "Bearer " + key})
                timing.diagnostic_context["llm_started_at"]=time.time()
                provider_request.extensions["trace"] = timing.transport_event
                response = await self.client.send(provider_request, stream=True)
                timing.generation.response=response
                self._checkpoint(timing)
                timing.mark("upstream_headers")
                if response.status_code != 200 or "text/event-stream" not in response.headers.get("content-type", ""):
                    raise GatewayError("upstream_unavailable", 502)
        except BaseException as exc:
            timing.outcome = "superseded" if self._obsolete(timing) else "cancelled" if isinstance(exc, asyncio.CancelledError) else "upstream_unavailable"
            if response is not None:
                await self._cleanup(response, call_id, timing)
            else:
                self._release(call_id, timing)
                timing.finish()
            if self._obsolete(timing) and not timing.generation.disconnected:
                raise GenerationSuperseded() from None
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(504 if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else 502, "Gemini request unavailable.") from None
        finally:
            watch_stop.set()
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        async def retry_single():
            retry = deepcopy(upstream)
            retry["messages"][0]["content"] += SERIAL_TOOL_RETRY
            retry_request = self.client.build_request("POST", GOOGLE_URL, json=retry, headers={"Authorization": "Bearer " + key})
            retry_request.extensions["trace"] = timing.transport_event
            fresh = await self.client.send(retry_request, stream=True)
            timing.generation.response = fresh
            try:
                self._checkpoint(timing)
                if fresh.status_code != 200 or "text/event-stream" not in fresh.headers.get("content-type", ""):
                    raise GatewayError("upstream_unavailable", 502)
                return fresh
            except BaseException:
                await fresh.aclose()
                raise
        return OwnedStream(self._relay(request, response, sid, call_id, started, timing,
                                       issuance_context=user_context(upstream["messages"]),
                                       allow_end_call=any(tool["function"]["name"]==END_CALL for tool in upstream.get("tools",[])),
                                       serial_tools=timing.diagnostic_context.get("serial_tools", False), retry_single=retry_single),
                           lambda: self._cleanup(response, call_id, timing))

    def _backchannel_context(self,sid,call_id):
        # Optional read-only context; failure suppresses the cue, never the answer.
        with self.narration.store.read_connection(0.05) as db:
            row=db.execute("SELECT state,ended,provider_call_id FROM sessions WHERE id=?",(sid,)).fetchone()
            if not row or row["ended"] or row["provider_call_id"] != call_id:return {"allow_turn":False}
            state=json.loads(row["state"])
            pending=db.execute("SELECT 1 FROM tool_invocations WHERE session_id=? AND status IN ('queued','running','unknown') LIMIT 1",(sid,)).fetchone()
            confirmation=False
            if state.get("stage")=="confirmation" and state.get("proposal_id"):
                confirmation=bool(db.execute("SELECT 1 FROM proposals WHERE id=? AND session_id=? AND kind='book' AND state='pending' AND expires>=?",
                    (state["proposal_id"],sid,time.time())).fetchone())
            elif state.get("stage")=="complete":
                confirmation=bool(db.execute("SELECT 1 FROM appointments WHERE session_id=? AND status='confirmed' LIMIT 1",(sid,)).fetchone())
            return {"allow_turn":True,"stage":state.get("stage"),
                    "active_confirmation":state.get("stage")=="confirmation",
                    "has_pending_proposal":bool(state.get("proposal_id")),
                    "has_pending_operation":bool(pending),"ended":False,"booking_confirmation_context":confirmation}

    def _backchannel_retired(self,call_ids):
        # Bounded known-ledger scan only at capacity. Missing sessions or read
        # failures never evict active budgets or refund an accepted cue.
        if not call_ids:return ()
        if len(call_ids)>256:raise ValueError("backchannel_budget_bounds")
        with self.narration.store.read_connection(0.05) as db:
            marks=",".join("?" for _ in call_ids)
            return tuple(row[0] for row in db.execute(
                f"SELECT provider_call_id FROM sessions WHERE provider_call_id IN ({marks}) AND (ended!=0 OR created<?)",
                (*call_ids,time.time()-4*3600)))

    def _waiting_status(self, call_id, sid, ticket, follow_up=False):
        if self.waiting_cue_state is None:
            return "eligible"
        try:
            current, reason = self.waiting_cue_state(call_id, sid, *([True] if follow_up else []))
            if current is None or ticket is None:
                return "speaker_state_unavailable"
            return reason if current == ticket else "caller_resumed"
        except Exception:
            return "speaker_state_unavailable"

    def _waiting_applies(self, timing):
        return (self.waiting_cue_assistant is None
                or timing.diagnostic_context.get("assistant_id") == self.waiting_cue_assistant)

    async def _coordinate_answer(self, timing):
        if self.before_answer is not None and self._waiting_applies(timing):
            await self.before_answer(timing.diagnostic_context["call_id"], timing.correlation)
            self._checkpoint(timing)

    async def _wait_for_caller_pause(self, primary, call_id, sid, ticket, follow_up=False):
        # Bounded, optional wait concurrent with the main answer. Caller events
        # arrive on the same event loop; no locks, provider calls or disk reads.
        deadline = time.monotonic()+1.25
        while True:
            if primary.done():
                primary.result()
                return "primary_ready"
            reason = self._waiting_status(call_id, sid, ticket, follow_up)
            if reason in {"eligible", "caller_resumed", "speaker_state_unavailable", "turn_already_acknowledged"}:
                return reason
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return reason
            await asyncio.wait((primary,), timeout=min(.04, remaining))

    async def _backchannel_stream(self,request,config,key,upstream,sid,call_id,started,timing):
        """One bounded acknowledgment in the existing owned Vapi stream.

        Main work starts immediately. No live-control URL, second speaker,
        speculative tool prose, dynamic name or booking authority is added.
        """
        streams=[]
        scoped_waiting = self._waiting_applies(timing)
        try:
            speaker_ticket = self.waiting_cue_state(call_id, sid)[0] if self.waiting_cue_state and scoped_waiting else None
        except Exception:
            speaker_ticket = None
        selection_context = None
        policy_messages=upstream["messages"]
        follow_up=False
        if policy_messages and policy_messages[-1].get("role")=="tool":
            # prepare_request already verified the signed tool group and its
            # authoritative receipt. Reuse only the caller context; tool data
            # and generated action prose never enter the optional classifier.
            # This request answers the same caller turn after a tool result, so
            # the budget may grant it one spaced follow-up cue.
            follow_up=True
            caller=next((index for index in range(len(policy_messages)-1,-1,-1)
                         if policy_messages[index].get("role")=="user"),None)
            policy_messages=policy_messages[:caller+1] if caller is not None else []
        async def read_context():
            # Shield only the physical read: timed-out thread work retains its
            # slot until done, but neither classifier nor main waits for it.
            async with asyncio.timeout(.08):
                if len(self.backchannel_reads)>=self.limits.concurrency:raise TimeoutError()
                candidates=tuple(self.backchannel_budget.calls) if len(self.backchannel_budget.calls)>=self.backchannel_budget.capacity else ()
                def inspect():
                    context=self._backchannel_context(sid,call_id)
                    return context,self._backchannel_retired(candidates) if candidates else ()
                read=asyncio.create_task(run_in_threadpool(inspect))
                self.backchannel_reads.add(read)
                def read_done(task):
                    self.backchannel_reads.discard(task)
                    if not task.cancelled():task.exception()
                read.add_done_callback(read_done)
                context,retired=await asyncio.shield(read)
                self.backchannel_budget.retire_confirmed(retired)
                if (self.waiting_cue_assistant is not None and scoped_waiting
                        and config.get("SERVICEVOICE_TWILIO_WAITING_FALLBACK") == "on"):
                    context={**context,"neutral_waiting":True}
                return context
        async def prime_answer():
            self._phase(timing,True)
            stream=await self._generate_upstream(request,key,upstream,sid,call_id,started,timing)
            streams.append(stream)
            first=await anext(stream.body_iterator)
            return stream,first
        async def choose():
            nonlocal selection_context
            from .backchannel_policy import BackchannelDecision
            try:
                async with asyncio.timeout(.48):
                    selection_context=await read_context()
                    def requested():timing.row["backchannel"]["requests"]+=1
                    decision=await self.backchannel_policy.choose(policy_messages,config.get("TYPESAFE_API_KEY"),
                        context=selection_context,on_request=requested,native_waiting=True)
                    if (scoped_waiting and not follow_up and self.waiting_content_finished is not None
                            and decision.reason=="eligible_classification" and decision.phrase_id in WAITING_PHRASE_IDS):
                        state,_=waiting_state(policy_messages,selection_context)
                        if state is not None:
                            self.waiting_content_finished(call_id,sid,state["utterance"],speaker_ticket)
                    return decision
            except Exception:
                return BackchannelDecision(reason="context_unavailable")
        timing.row["backchannel"]={"phrase_id":"silence","reason":"pending","emitted_ms":None,
                                    "classification_ms":None,"probability":None,"confidence":None,"requests":0,
                                    "policy_elapsed_ms":None,"language_choice":None,"language_probability":None,
                                    "language_confidence":None,"content_kind":None,"content_probability":None,
                                    "content_confidence":None,"follow_up":False,"audible_playback":"unverified"}
        timing.row["sse_response_id"]="chatcmpl-"+uuid4().hex
        primary=asyncio.create_task(prime_answer())
        policy=asyncio.create_task(choose())
        timing.backchannel_tasks=(primary,policy)
        timing.backchannel_streams=streams
        try:
            # Do not fill ordinary short pauses. Primary completion always wins.
            done,_=await asyncio.wait((primary,),timeout=ACK_DELAY_SECONDS)
            if primary in done:primary.result()  # Preserve genuine errors before a released owner is checked.
            self._checkpoint(timing)
            record=timing.row["backchannel"]
            # Preserve a completed parallel classification even if the primary
            # answer wins; unknown/in-flight probabilities remain unknown.
            decision=None
            if policy.done() and not policy.cancelled():
                try:decision=policy.result()
                except Exception:
                    from .backchannel_policy import BackchannelDecision
                    decision=BackchannelDecision(reason="classifier_unavailable")
                record.update(phrase_id=decision.phrase_id,reason=decision.reason,
                              classification_ms=decision.classification_ms,probability=decision.probability,
                              confidence=decision.confidence,policy_elapsed_ms=decision.elapsed_ms,
                              language_choice=decision.language_choice,language_probability=decision.language_probability,
                              language_confidence=decision.language_confidence,content_kind=decision.content_kind,
                              content_probability=decision.content_probability,content_confidence=decision.content_confidence)
            if primary in done:
                record["reason"]="primary_ready"
            elif decision is not None:
                self._backchannel_observation(timing,"backchannel.selected",None)
                fallback = (not follow_up and scoped_waiting and config.get("SERVICEVOICE_TWILIO_WAITING_FALLBACK") == "on"
                    and self.waiting_fallback_ready is not None
                    and guarded_waiting_fallback(decision, waiting_state(policy_messages, selection_context)[0], selection_context))
                if fallback or decision.phrase_id in WAITING_PHRASE_IDS and decision.reason=="eligible_classification":
                    speaker_reason = (await self._wait_for_caller_pause(primary, call_id, sid, speaker_ticket, follow_up)
                                      if scoped_waiting else "eligible")
                    # Never wait for this optional fresh read ahead of a ready
                    # main answer. A context change cannot grant new authority.
                    fresh=asyncio.create_task(read_context())
                    try:
                        ready,_=await asyncio.wait((primary,fresh),return_when=asyncio.FIRST_COMPLETED)
                        if primary in ready:
                            primary.result(); record["reason"]="primary_ready"
                            current=None
                        else:current=fresh.result()
                    except Exception:
                        current=None; record["reason"]="context_unavailable"
                    finally:
                        if not fresh.done():fresh.cancel()
                        await asyncio.gather(fresh,return_exceptions=True)
                    state,reason=waiting_state(policy_messages,current) if current is not None else (None,record["reason"])
                    # Recheck after the asynchronous context read. This check,
                    # budget reservation and SSE yield have no intervening await.
                    if speaker_reason == "eligible" and scoped_waiting:
                        speaker_reason = self._waiting_status(call_id, sid, speaker_ticket, follow_up)
                    if speaker_reason != "eligible":
                        state,reason = None,speaker_reason
                    if state is not None and fallback and not self.waiting_fallback_ready(call_id, sid, state["utterance"]):
                        state,reason = None,"fallback_turn_unverified"
                    if state is None:record["reason"]=reason
                    elif selection_context is None or current!=selection_context:record["reason"]="scheduler_changed"
                    elif not primary.done():
                        # A follow-up after a tool result is always a neutral
                        # hold and avoids the phrase the turn already heard.
                        phrase=waiting_phrase(decision,state["user_count"],
                            self.backchannel_budget.previous_phrase(call_id),neutral_only=fallback or state["neutral_confirmation"] or state.get("neutral_only",False) or follow_up
                            or (scoped_waiting and self.waiting_neutral_only is not None and self.waiting_neutral_only(call_id,sid)))
                        record["phrase_id"]=phrase
                        record["guarded_fallback"]=fallback
                        allowed,reason=self.backchannel_budget.consume(call_id,state["user_count"],phrase,follow_up=follow_up,
                            observed_spacing=scoped_waiting and self.waiting_cue_commit is not None)
                        record["reason"]=reason
                        if allowed:
                            if scoped_waiting and self.waiting_cue_commit is not None and not self.waiting_cue_commit(call_id, sid, *([True] if follow_up else [])):
                                allowed = False
                                self.backchannel_budget.refund(call_id,self.backchannel_budget.receipt(call_id))
                                record["reason"] = "turn_already_acknowledged"
                        if allowed:
                            timing.waiting_cue_committed = scoped_waiting and self.waiting_cue_commit is not None
                            record["follow_up"]=follow_up
                            timing.backchannel_budget_stamp=self.backchannel_budget.receipt(call_id)
                            if timing.waiting_cue_committed and self.waiting_cue_track is not None:
                                self.waiting_cue_track(call_id,sid,timing.backchannel_budget_stamp)
                            timing.before_finish=lambda:self._backchannel_refund(call_id,timing)
                    else:allowed=False; record["reason"]="primary_ready"
                    if state is not None and current==selection_context and not primary.done() and allowed:
                        self._checkpoint(timing)
                        elapsed=round((time.monotonic()-started)*1000,3)
                        record["emitted_ms"]=elapsed
                        self._payload(timing)
                        self._backchannel_observation(timing,"backchannel.emitted",elapsed)
                        # A trailing space after flush becomes a separate blank
                        # voice request when the next payload is a tool call.
                        yield sse({"id":timing.row["sse_response_id"],"object":"chat.completion.chunk",
                            "created":int(time.time()),"model":MODEL,"choices":[{"index":0,"delta":{
                                "role":"assistant","content":PHRASES[record["phrase_id"]]+" <flush />"},"finish_reason":None}]})
            else:record["reason"]="policy_not_ready"
            if record["emitted_ms"] is None:self._backchannel_observation(timing,"backchannel.suppressed",None)
            stream,first=await asyncio.shield(primary)
            self._checkpoint(timing)
            yield first
            async for part in stream.body_iterator:
                self._checkpoint(timing)
                yield part
        except GenerationSuperseded:
            if self._obsolete(timing):
                timing.outcome="superseded"
                async for part in self._neutral_stream(request,timing):yield part
            else:
                timing.outcome="upstream_unavailable"
                yield sse({"error":{"code":"generation_unavailable","message":"Voice generation could not complete. No appointment outcome is inferred."}})
        except asyncio.CancelledError:
            if self._obsolete(timing) and not timing.generation.disconnected:
                timing.outcome="superseded"
                async for part in self._neutral_stream(request,timing):yield part
            else:
                timing.outcome="cancelled"
                raise
        except Exception:
            timing.outcome="upstream_unavailable"
            yield sse({"error":{"code":"generation_unavailable","message":"Voice generation could not complete. No appointment outcome is inferred."}})
        finally:
            await self._cleanup_backchannel(call_id,timing)

    async def _cleanup_backchannel(self,call_id,timing):
        cleanup=getattr(timing,"backchannel_cleanup_task",None)
        if cleanup is None:
            async def finish():
                tasks=getattr(timing,"backchannel_tasks",())
                for task in tasks:
                    if not task.done() and not task.cancelling():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                for stream in getattr(timing,"backchannel_streams",()):
                    try:await stream.body_iterator.aclose()
                    finally:await stream.cleanup()
                await self._cleanup_narration(call_id,timing)
            cleanup=asyncio.create_task(finish())
            timing.backchannel_cleanup_task=cleanup
        # A departing HTTP waiter never abandons physical provider cleanup.
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(cleanup),timeout=self.limits.cleanup_seconds)

    def _backchannel_refund(self,call_id,timing):
        """Give an emitted-but-unplayed waiting cue back to the turn budget.

        Vapi discards the output of a request it cancels or replaces before the
        answer. Observed on 2026-10-21: speculative requests that Vapi started
        while the caller was still speaking consumed the one-per-turn cue, and
        the real post-turn request stayed silent for several seconds. Refund
        only when no answer text or tool command was released after the cue;
        completed answers and upstream failures keep their consumption.
        """
        record=timing.row.get("backchannel");stamp=timing.backchannel_budget_stamp
        if not isinstance(record,dict) or stamp is None or record.get("emitted_ms") is None or record.get("refunded"):return
        milestones=timing.row["milestones_ms"]
        if milestones.get("first_text") is not None or milestones.get("tool_emitted") is not None:return
        if self._obsolete(timing):cause="generation_superseded_before_answer"
        elif timing.outcome in ("cancelled","interrupted"):cause="generation_cancelled_before_answer"
        else:return
        # Do not refund the phone lane immediately. It reconciles only after
        # the caller resumed and stopped, a lateness grace elapsed, and no
        # assistant speech/transcript was observed since this reservation.
        if getattr(timing, "waiting_cue_committed", False):
            if self.waiting_cue_track is not None:
                self.waiting_cue_track(call_id,timing.correlation,stamp,abandoned=True)
            return
        timing.backchannel_budget_stamp=None
        refunded,_=self.backchannel_budget.refund(call_id,stamp)
        if not refunded:return
        record["refunded"]=True;record["refund_reason"]=cause
        self._backchannel_observation(timing,"backchannel.refunded",None)

    def _backchannel_observation(self,timing,event,elapsed):
        if self.diagnostics is None:return
        try:
            record=timing.row["backchannel"]
            self.diagnostics.record("gateway",event,"observed",
                call_id=timing.diagnostic_context["call_id"],turn_id=timing.diagnostic_context.get("turn_id"),
                request_id=timing.row["request_id"],
                start_unix=timing.row["started_at_unix"] if elapsed is not None else None,duration_ms=elapsed,
                attributes={"generation_id":timing.row["generation_id"],"backchannel_phrase":record["phrase_id"],
                    "backchannel_reason":record.get("refund_reason") if event=="backchannel.refunded" else record["reason"],
                    **({"backchannel_refunded":True,"backchannel_refund_reason":record.get("refund_reason")} if record.get("refunded") else {}),
                    "backchannel_follow_up":record.get("follow_up") is True,
                    "backchannel_guarded_fallback":record.get("guarded_fallback") is True,
                    "classifier_ms":record["classification_ms"],
                    "backchannel_probability":record["probability"],"backchannel_confidence":record["confidence"],
                    "classifier_requests":record["requests"],"policy_elapsed_ms":record["policy_elapsed_ms"],
                    "backchannel_language":record["language_choice"],"backchannel_language_probability":record["language_probability"],
                    "backchannel_language_confidence":record["language_confidence"],
                    "backchannel_content_kind":record.get("content_kind"),"backchannel_content_probability":record.get("content_probability"),
                    "backchannel_content_confidence":record.get("content_confidence"),
                    "evidence_source":"server_observed","observation_scope":"server_bytes_only_not_audible_playback"})
        except Exception:pass

    async def _cleanup_narration(self, call_id, timing):
        if timing.generation is not None and timing.generation.response is not None:
            await self._cleanup(timing.generation.response,call_id,timing)
            return
        self._release(call_id, timing)
        timing.finish()

    async def _close_phone(self,request,upstream,sid,call_id,timing,text):
        try:
            self._phase(timing,True)
            self._checkpoint(timing)
            if await request.is_disconnected() or not self.closing_ready(call_id,sid,text):return
            tool={"id":"call_"+uuid4().hex,"type":"function","function":{"name":END_CALL,"arguments":"{}"}}
            await run_in_threadpool(self.store.save_group,sid,call_id,[tool],
                context=user_context(upstream["messages"]),allow_end_call=True)
            self._checkpoint(timing)
            if not self.closing_ready(call_id,sid,text):return
            timing.row["counts"]["tool_calls"]=1
            timing.mark("tool_emitted")
            self._payload(timing)
            response_id="chatcmpl-"+uuid4().hex
            def chunk(delta,reason=None):
                return {"id":response_id,"object":"chat.completion.chunk","created":int(time.time()),
                    "model":"servicevoice-closing","choices":[{"index":0,"delta":delta,"finish_reason":reason}]}
            yield sse(chunk({"role":"assistant","tool_calls":[{"index":0,**tool}]}))
            yield sse(chunk({},"tool_calls"))
            timing.outcome="completed"
            yield b"data: [DONE]\n\n"
        finally:
            self._phase(timing,False)

    async def _narrate(self, request, text, timing, source="servicevoice-receipt"):
        response_id = timing.row.get("sse_response_id") or "chatcmpl-" + uuid4().hex
        timing.row["sse_response_id"]=response_id
        def chunk(delta, reason=None):
            return {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": source, "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
        try:
            self._phase(timing,True)
            if await request.is_disconnected():return
            self._checkpoint(timing)
            await self._coordinate_answer(timing)
            timing.mark("first_text")
            timing.row["counts"]["text_chunks"] = 1
            timing.row["counts"]["text_utf8_bytes"] = len(text.encode("utf-8"))
            # Exact deterministic text offered to the downstream stream, not
            # proof of playback, pronunciation or assistant-channel captions.
            timing.row["output_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            timing.row["output_text_source"] = "static_faq" if source == "servicevoice-jev-faq" else "verified_receipt"
            self._payload(timing)
            yield sse(chunk({"role": "assistant", "content": text}))
            yield sse(chunk({}, "stop"))
            timing.outcome = "superseded_after_release" if self._obsolete(timing) else "completed"
            yield b"data: [DONE]\n\n"
        except (asyncio.CancelledError,GenerationSuperseded):
            if self._obsolete(timing) and not timing.generation.disconnected:
                timing.outcome="superseded"
                self._phase(timing,False)
                async for part in self._neutral_stream(request,timing):yield part
                return
            timing.outcome = "cancelled"
            raise
        finally:
            self._phase(timing,False)

    async def _relay(self, request, response, sid, call_id, started, timing=None, *, issuance_context=None, allow_end_call=False,
                     serial_tools=False, retry_single=None, owns_generation=True):
        timing = timing or RequestTiming(call_id, started, self.timings)
        transport = {"iterator": response.aiter_bytes(), "buffer_bytes": 0, "received_bytes": 0}
        assembler = ToolAssembler(self.limits,allow_end_call=allow_end_call)
        finished, completed, pending_calls, finish_reason = False, False, None, None
        # A model may put an unsupported success claim before its tool call.
        # Keep that generation's prose private until its type is known. A tool
        # generation emits commands only; the following verified result owns
        # the outcome speech. Text-only generations retain their exact words.
        text_parts, text_bytes = [], 0
        response_id = timing.row.get("sse_response_id") or "chatcmpl-" + uuid4().hex
        timing.row["sse_response_id"]=response_id
        watch_stop = asyncio.Event()
        watcher = asyncio.create_task(self._watch_disconnect(request, asyncio.current_task(), watch_stop,timing.generation))
        def chunk(delta, reason=None):
            return {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
        try:
            self._phase(timing,True)
            remaining = self.limits.total_seconds - (time.monotonic() - started)
            async with asyncio.timeout(max(0.001, remaining)):
                async for data in sse_events(response, self.limits, transport):
                    timing.mark("first_upstream_event")
                    timing.row["counts"]["upstream_events"] += 1
                    if data == "[DONE]":
                        timing.mark("upstream_done")
                        if not finished:
                            raise GatewayError("incomplete_upstream_response")
                        await drain_sse_tail(transport, self.limits, timing,
                                             self.limits.total_seconds - (time.monotonic() - started))
                        if time.monotonic() - started >= self.limits.total_seconds:
                            raise TimeoutError()
                        self._checkpoint(timing)
                        if serial_tools and pending_calls and len(pending_calls) > 1:
                            # Nothing from this candidate has crossed the tool
                            # boundary. Replan, never trim or forge a signed group.
                            if retry_single is not None:
                                prior_usage = dict(timing.row["usage"])
                                timing.row["usage"] = {key: None for key in prior_usage}
                                timing.row["counts"]["serial_tool_replans"] = 1
                                await response.aclose()
                                try:
                                    fresh = await retry_single()
                                    watch_stop.set(); watcher.cancel()
                                    with suppress(asyncio.CancelledError):await watcher
                                    async for part in self._relay(request, fresh, sid, call_id, started, timing,
                                            issuance_context=issuance_context, allow_end_call=allow_end_call,
                                            serial_tools=True, owns_generation=False):
                                        yield part
                                finally:
                                    for key, value in prior_usage.items():
                                        if value is not None:
                                            timing.row["usage"][key] = value+(timing.row["usage"].get(key) or 0)
                                return
                            # A model that ignores the serial rule must not end
                            # the phone call or authorize part of a hidden batch.
                            pending_calls = None
                            text_parts = [SERIAL_TOOL_CLARIFICATION]
                            text_bytes = len(SERIAL_TOOL_CLARIFICATION.encode())
                            finish_reason = "stop"
                            timing.row["counts"]["serial_tool_clarifications"] = 1
                            timing.row["output_text_source"] = "application_clarification"
                            timing.row["output_text_sha256"] = hashlib.sha256(SERIAL_TOOL_CLARIFICATION.encode()).hexdigest()
                        if pending_calls:
                            if pending_calls[0]["function"]["name"]==END_CALL:
                                if not await run_in_threadpool(self.store.end_call_ready,sid):
                                    raise GatewayError("platform_operation_unresolved",409)
                                self._checkpoint(timing)
                            persistence_started = time.monotonic()
                            await run_in_threadpool(self.store.save_group, sid, call_id, pending_calls, context=issuance_context,
                                                    allow_end_call=allow_end_call)
                            self._checkpoint(timing)
                            timing.row["durations_ms"]["tool_signature_persist"] = round((time.monotonic() - persistence_started) * 1000, 3)
                            timing.row["counts"]["tool_calls"] = len(pending_calls)
                            public_calls = [{"index": index, "id": tool["id"], "type": "function", "function": tool["function"]} for index, tool in enumerate(pending_calls)]
                            timing.mark("tool_emitted")
                            self._payload(timing)
                            yield sse(chunk({"role": "assistant", "tool_calls": public_calls}))
                        elif text_parts:
                            text = "".join(text_parts)
                            await self._coordinate_answer(timing)
                            timing.mark("first_text")
                            if timing.row["milestones_ms"]["first_upstream_text"] is not None:
                                timing.row["durations_ms"]["validated_text_hold"] = round(max(0,
                                    timing.row["milestones_ms"]["first_text"] - timing.row["milestones_ms"]["first_upstream_text"]), 3)
                            timing.row["counts"]["text_chunks"] = 1
                            timing.row["counts"]["text_utf8_bytes"] = text_bytes
                            self._payload(timing)
                            yield sse(chunk({"role": "assistant", "content": text}))
                        else:
                            # A successful HTTP response with no answer and no
                            # command is not a successful conversational turn.
                            raise GatewayError("upstream_empty_response")
                        self._phase(timing,False)
                        yield sse(chunk({}, finish_reason))
                        completed = True
                        timing.outcome = "superseded_after_release" if self._obsolete(timing) else "completed"
                        yield b"data: [DONE]\n\n"
                        break
                    event = parse_json(data)
                    if not isinstance(event, dict) or "error" in event:
                        raise GatewayError("upstream_stream_failed")
                    upstream_model = event.get("model")
                    expected_model = timing.row.get("model", MODEL)
                    if upstream_model is not None and (not isinstance(upstream_model, str) or not (upstream_model == expected_model or upstream_model.startswith(expected_model + "-"))):
                        raise GatewayError("upstream_model_mismatch")
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        observed = {"input_tokens": usage.get("prompt_tokens"),
                                    "output_tokens": usage.get("completion_tokens"),
                                    "total_tokens": usage.get("total_tokens")}
                        for source, target, field in (("prompt_tokens_details", "cached_input_tokens", "cached_tokens"),
                                                      ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens")):
                            details = usage.get(source)
                            if isinstance(details, dict):observed[target] = details.get(field)
                        for key, value in observed.items():
                            if type(value) is int and 0 <= value <= 100_000_000:
                                timing.row["usage"][key] = value
                    choices = event.get("choices", [])
                    if choices == []:
                        continue  # Usage-only frames are not speech.
                    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or choices[0].get("index", 0) != 0 or finished:
                        raise GatewayError("invalid_upstream_choices")
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise GatewayError("invalid_upstream_delta")
                    if "tool_calls" in delta:
                        timing.mark("first_tool_fragment")
                        assembler.add(delta["tool_calls"])
                    content = delta.get("content")
                    if content is not None:
                        if not isinstance(content, str):
                            raise GatewayError("invalid_upstream_content")
                        if content:
                            timing.mark("first_upstream_text")
                            text_bytes += len(content.encode("utf-8"))
                            if text_bytes > self.limits.response_bytes:
                                raise GatewayError("upstream_response_size")
                            text_parts.append(content)
                    reason = choice.get("finish_reason")
                    if reason is None:
                        continue
                    if reason in ("tool_calls", "stop") and assembler.parts:
                        pending_calls = assembler.finish()
                        reason = "tool_calls"  # Normalize Google's signed-call + stop contract for Vapi.
                    elif reason != "stop" or assembler.parts:
                        raise GatewayError("upstream_incomplete_or_blocked")
                    finished = True
                    finish_reason = reason
                if not completed:
                    raise GatewayError("incomplete_upstream_response")
        except (asyncio.CancelledError,GenerationSuperseded):
            if self._obsolete(timing) and not timing.generation.disconnected:
                timing.outcome="superseded"
                self._phase(timing,False)
                async for part in self._neutral_stream(request,timing):yield part
                return
            timing.outcome = "cancelled"
            raise
        except (GatewayError, httpx.HTTPError, TimeoutError, sqlite3.Error, ValueError, TypeError):
            if self._obsolete(timing) and not timing.generation.disconnected:
                timing.outcome="superseded"
                self._phase(timing,False)
                async for part in self._neutral_stream(request,timing):yield part
                return
            timing.outcome = "stream_unavailable"
            # The status is already streaming. Do not emit a success finish/DONE
            # after an error, and never expose the provider's error or request.
            self._phase(timing,False)
            yield sse({"error": {"code": "generation_unavailable", "message": "Voice generation could not complete. No appointment outcome is inferred."}})
        finally:
            self._phase(timing,False)
            watch_stop.set()
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            if owns_generation:
                await self._cleanup(response, call_id, timing)
            else:
                await response.aclose()


class OwnedStream(StreamingResponse):
    """Own upstream cleanup even if downstream fails before iteration starts."""
    def __init__(self, iterator, cleanup):
        super().__init__(iterator, media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                await self.cleanup()


def install_gateway(app, config_loader, bind_call, **kwargs):
    gateway = GeminiGateway(app, config_loader, bind_call, **kwargs)
    app.state.gemini_gateway = gateway
    app.add_api_route("/llm/chat/completions", gateway.handle, methods=["POST"])
    return gateway

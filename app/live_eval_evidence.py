"""Read-only evidence and conservative checks for post-call review.

Nothing in this module dispatches a tool, modifies the scheduler, trusts a Vapi
summary as ground truth, or equates saved text with audio heard by a caller.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .domain import dealership_facts, TZ, DomainError, normalize_services

VERSION = "live-call-review-v5"
DIMENSIONS = ("hallucination_free", "booking_accuracy", "confirmation_compliance", "action_truthfulness",
              "task_completion", "conversation_quality")
MAX_MESSAGES = 500
MAX_EVIDENCE_BYTES = 220_000
SECRET = re.compile(r"Bearer\s+\S+|AIza[\w-]{20,}|(?:sk-|lsv2_)[\w-]{12,}|-----BEGIN[^\n]+-----", re.I)
EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
PHONE = re.compile(r"(?<!\w)\+?\d[\d ()\-.]{7,}\d(?!\w)")
URL = re.compile(r"(?:https?://|file://)\S+", re.I)
REFERENCE = re.compile(r"\bCSV-[A-F0-9]{8}\b", re.I)


class EvidenceError(ValueError):
    """Fixed diagnostic codes only; never interpolate provider content."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def numeric(value):
    return float(value) if type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 86_400_000 else None


def percentile(values, fraction=.95):
    values = sorted(v for v in values if numeric(v) is not None)
    return round(values[max(0, math.ceil(len(values)*fraction)-1)], 3) if values else None


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=.2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def read_snapshot(path, call_id):
    """One consistent snapshot; no Store constructor, seeding or business write."""
    with closing(readonly(path)) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT id,tenant,created,ended FROM sessions WHERE provider_call_id=?", (call_id,)).fetchone()
        if not row or row["tenant"] != "cedar-demo":
            raise EvidenceError("call_not_bound_to_this_scheduler")
        sid = row["id"]
        def rows(table, fields="*", order="rowid"):
            # Table/column expressions are literals supplied only by this function.
            return [dict(r) for r in db.execute(f"SELECT {fields} FROM {table} WHERE session_id=? ORDER BY {order} LIMIT 501", (sid,))]
        result = {"session": dict(row), "appointments": rows("appointments"), "proposals": rows("proposals"),
                  "results": rows("tool_results"), "operations": rows("tool_invocations", "call_id,status"),
                  "events": rows("events", "name,status,elapsed_ms,at"), "handoffs": rows("handoffs", "status,reason")}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Read the ordered service scope from the authoritative child rows.
        # Older databases legitimately have no child table or singleton rows.
        # Never seed/migrate the scheduler from a post-call reviewer.
        if "appointment_services" in tables:
            for appointment in result["appointments"]:
                selected = list(db.execute("SELECT position,service FROM appointment_services WHERE appointment_id=? ORDER BY position LIMIT 5", (appointment["id"],)))
                if selected:
                    values = [r["service"] for r in selected]
                    try:
                        valid = normalize_services(appointment["service"], values)
                    except DomainError:
                        raise EvidenceError("invalid_appointment_services") from None
                    if [r["position"] for r in selected] != list(range(len(selected))):
                        raise EvidenceError("invalid_appointment_services")
                    if len(valid) > 1:
                        appointment["services"] = valid
        result["signatures"] = []
        if "gemini_tool_signatures" in tables:
            for r in db.execute("SELECT tool_call_id,fingerprint,extras FROM gemini_tool_signatures WHERE session_id=? AND provider_call_id=? LIMIT 501", (sid,call_id)):
                # Explicitly discard thought signatures and opaque provider extras.
                extras = json.loads(r["extras"])
                context = extras.get("__servicevoice_issuance_context", {})
                count = context.get("user_count")
                result["signatures"].append({"tool_call_id":r["tool_call_id"],"fingerprint":r["fingerprint"],
                    "user_count":count if type(count) is int and count > 0 else None})
        if any(len(v) > 500 for v in result.values() if isinstance(v,list)):
            raise EvidenceError("scheduler_evidence_too_large")
        for table, fields in (("appointments",("customer",)),("proposals",("payload","result")),("results",("result",))):
            for r in result[table]:
                for field in fields:
                    r[field] = json.loads(r[field]) if r[field] else None
        return result


def missing_snapshot(call):
    """Explicitly unavailable scheduler evidence; empty lists do not mean zero."""
    try:created=datetime.fromisoformat(call["createdAt"].replace("Z","+00:00")).timestamp()
    except (KeyError,ValueError,TypeError):raise EvidenceError("missing_call_creation_timestamp") from None
    return {"source_available":False,"session":{"id":"","created":created,"ended":1},
        "appointments":[],"proposals":[],"results":[],"operations":[],"events":[],"handoffs":[],"signatures":[]}


def check(verdict, reason, refs=()):
    return {"verdict":verdict,"reason_code":reason,"evidence_refs":list(refs)}


def service_scope(value):
    """Compare the full ordered scope, retaining legacy singleton receipts."""
    try:
        return normalize_services(value.get("service"), value.get("services"))
    except (DomainError, TypeError, AttributeError):
        return None


def integrity(snapshot):
    """Check every persisted appointment against its latest committed receipt.

    A caller's intended details and spoken assent require separate semantic
    review. No-booking calls are not counted as successful booking tests.
    """
    appointments = snapshot["appointments"]
    commits = [p for p in snapshot["proposals"] if p["state"] == "committed"]
    pending = [r for r in snapshot["operations"] if r["status"] in {"queued","running","unknown"}]
    if pending:
        return check("uncertain","unresolved_operations",["state"])
    if not appointments and not commits:
        return check("not_applicable","no_saved_booking",["state"])
    receipts = {}
    for p in commits:
        receipt = p["result"] or {}
        a = receipt.get("appointment")
        if receipt.get("ok") is not True or not isinstance(a,dict) or not a.get("id"):
            return check("fail","committed_action_missing_receipt",["state"])
        selected = service_scope(a)
        if selected is None:
            return check("fail","proposal_receipt_mismatch",["state"])
        if receipt.get("action") != p["kind"] or a.get("status") != ("cancelled" if p["kind"] == "cancel" else "confirmed"):
            return check("fail","proposal_receipt_mismatch",["state"])
        if p["kind"] == "book":
            payload = p["payload"]
            if selected != service_scope(payload) or any(a.get(k) != payload.get(k) for k in ("service","slot_id")) or a.get("customer_name") != payload.get("customer",{}).get("name") or a.get("vehicle") != payload.get("customer",{}).get("vehicle"):
                return check("fail","proposal_receipt_mismatch",["state"])
        elif p["kind"] in {"reschedule", "cancel"}:
            payload = p["payload"]
            previous = receipts.get(a["id"])
            if (previous is None or payload.get("appointment_id") != a["id"]
                    or any(a.get(k) != previous.get(k) for k in ("id", "reference", "customer_name", "vehicle"))
                    or a.get("slot_id") != (payload.get("slot_id") if p["kind"] == "reschedule" else previous.get("slot_id"))):
                return check("fail","proposal_receipt_mismatch",["state"])
            expected = payload.get("services") if "services" in payload else service_scope(previous)
            if expected is None or selected != expected:
                return check("fail","proposal_receipt_mismatch",["state"])
            if "previous_services" in payload and (previous is None or payload["previous_services"] != service_scope(previous)):
                return check("fail","proposal_receipt_mismatch",["state"])
        receipts[a["id"]] = a
    if set(receipts) != {a["id"] for a in appointments}:
        return check("fail","appointment_commit_mismatch",["state"])
    for a in appointments:
        receipt = receipts[a["id"]]
        if service_scope(a) is None or service_scope(a) != service_scope(receipt) or any(a[k] != receipt.get(k) for k in ("id","reference","service","slot_id","status")) or a["customer"].get("name") != receipt.get("customer_name") or a["customer"].get("vehicle") != receipt.get("vehicle"):
            return check("fail","saved_booking_receipt_mismatch",["state"])
        initial = next((p for p in commits if p["kind"] == "book" and (p["result"] or {}).get("appointment",{}).get("id") == a["id"]),None)
        if initial is None or initial["payload"].get("customer") != a["customer"]:
            return check("fail","saved_customer_proposal_mismatch",["state"])
    return check("pass","saved_records_match_committed_receipts",["state"])


class Redactor:
    """Minimise judge inputs; this is not a general-purpose DLP guarantee.

    Customer names found in structured evidence receive consistent aliases.
    Unknown free-text personal information can remain in the judge input. Raw
    transcripts and judge prose are never exported to LangSmith by this path.
    """
    def __init__(self, values):
        names = set()
        def visit(value):
            if isinstance(value,dict):
                for k,v in value.items():
                    if (k == "customer_name" or k == "name" and ("phone" in value or "vehicle" in value)) and isinstance(v,str) and len(v.strip()) >= 2:
                        names.add(v.strip())
                    visit(v)
            elif isinstance(value,list):
                for v in value:visit(v)
        visit(values)
        self.names = {v:f"[customer_{i}]" for i,v in enumerate(sorted(names),1)}
        self.phones = {}

    def text(self, text):
        if not isinstance(text,str) or len(text) > 24_000:
            raise EvidenceError("oversized_evidence_text")
        text = SECRET.sub("[credential]", URL.sub("[url]", EMAIL.sub("[email]",text)))
        for name,alias in sorted(self.names.items(),key=lambda item:-len(item[0])):
            text = re.sub(re.escape(name),lambda _:alias,text,flags=re.I)
        def phone(m):
            digits = "".join(c for c in m[0] if c.isdigit())
            if len(digits) < 10:return m[0]
            if len(digits) == 11 and digits.startswith("1"):digits=digits[1:]
            return self.phones.setdefault(digits,f"[phone_{len(self.phones)+1}]")
        return PHONE.sub(phone,text)

    def value(self,value,depth=0):
        if depth > 16:raise EvidenceError("evidence_depth_exceeded")
        if isinstance(value,str):return self.text(value)
        if isinstance(value,dict):return {k:self.value(v,depth+1) for k,v in value.items()}
        if isinstance(value,list):return [self.value(v,depth+1) for v in value]
        return value


def build_evidence(call, snapshot):
    snapshot = deepcopy(snapshot)
    artifact = call.get("artifact") or {}
    messages = artifact.get("messages")
    if messages is None:messages = call.get("messages",[])
    if not isinstance(messages,list) or len(messages) > MAX_MESSAGES:
        raise EvidenceError("missing_or_oversized_messages")
    local = {r["call_id"]:r["result"] for r in snapshot["results"]}
    signatures = {r["tool_call_id"]:r for r in snapshot["signatures"]}
    evidence, tools, transcript = {}, [], []
    platform_reports={}
    for message in messages:
        if isinstance(message,dict) and message.get("role")=="tool_call_result" and isinstance(message.get("toolCallId"),str):
            platform_reports.setdefault(message["toolCallId"],[]).append({"provenance":"untrusted_provider_result",
                "reported_result":message.get("result"),"authoritative_scheduler_receipt":False})
    caller_count = 0
    seen = {}
    issues = []
    for index,m in enumerate(messages):
        if not isinstance(m,dict):raise EvidenceError("invalid_message")
        role = m.get("role")
        if role in {"user","assistant","bot"} and isinstance(m.get("message"),str) and m["message"].strip():
            caller_count += int(role == "user")
            row = {"role":"user" if role == "user" else "assistant", "text":m["message"], "sequence":index,
                   "seconds_from_start":numeric(m.get("secondsFromStart"))}
            key = f"turn:{len(transcript)+1}"
            evidence[key] = row
            transcript.append(row)
        batch=m.get("toolCalls",[]) if isinstance(m.get("toolCalls"),list) else []
        for tool in batch:
            if not isinstance(tool,dict) or not isinstance(tool.get("function"),dict):
                issues.append("malformed_tool_evidence");continue
            tid,fn = tool.get("id"),tool["function"]
            args = fn.get("arguments")
            try:args = json.loads(args) if isinstance(args,str) else args
            except ValueError:args=None
            if not isinstance(tid,str) or not isinstance(args,dict):
                issues.append("malformed_tool_evidence");continue
            identity = canonical({"name":fn.get("name"),"arguments":args})
            if tid in seen:
                if seen[tid] != identity:issues.append("conflicting_tool_identity")
                if fn.get("name")=="endCall":issues.append("repeated_native_platform_attempt")
                continue
            seen[tid] = identity
            signature = signatures.get(tid,{})
            verified = signature.get("fingerprint") == hashlib.sha256(identity.encode()).hexdigest()
            entry = {"name":fn.get("name"),"arguments":args,"sequence":index,"user_count":signature.get("user_count") if verified else None,
                     "gateway_issuance_verified":verified,"server_result":local.get(tid),"provider_user_count":caller_count}
            if fn.get("name")=="endCall":
                # This is the one supported Vapi-owned action, never a booking
                # operation. The DB reader already scopes signatures to both
                # this scheduler session and provider call. A matching native
                # signature and current caller anchor establish only issuance.
                native_verified=(len(batch)==1 and args=={} and verified
                    and type(signature.get("user_count")) is int
                    and signature["user_count"]>0 and signature["user_count"]==caller_count)
                if native_verified:
                    reason=call.get("endedReason")
                    end_by_assistant=(True if reason in {"assistant-ended-call","assistant-said-end-call-phrase"}
                        else False if reason=="customer-ended-call" else None) if isinstance(reason,str) else None
                    entry.pop("server_result",None)
                    entry["evidence_scope"]="platform_attempt"
                    entry["platform_evidence"]={"action":"endCall","issuance_verified":True,
                        "outcome":"unknown","authoritative_scheduler_receipt":False,
                        "provider_ended_by_assistant":end_by_assistant,
                        "provider_results":platform_reports.get(tid,[]),
                        "limitations":"A native attempt is not verified execution, caller intent, farewell playback or a scheduler mutation. Provider termination is coverage only."}
                else:
                    entry["evidence_scope"]="unverified_platform_attempt"
                    issues.append("unverified_native_platform_attempt")
            else:
                entry["evidence_scope"]="business_operation"
                if tid not in local:issues.append("tool_without_terminal_receipt")
            key = f"tool:{len(tools)+1}"
            tools.append(entry);evidence[key] = entry
    # Reconcile in both directions. A provider artifact can retain plausible
    # dialogue while omitting the tool messages that actually reached this
    # scheduler. The final saved state cannot reconstruct their chronology.
    # Only persisted attempts/results require an observed tool identity: an
    # issued-but-never-executed model signature is not a business operation.
    persisted_attempts = set(local) | {r["call_id"] for r in snapshot["operations"]}
    if persisted_attempts - seen.keys():
        issues.append("persisted_tool_chronology_missing")
    committed_actions = any(p["state"] == "committed" for p in snapshot["proposals"])
    if committed_actions and not any(t["name"] == "confirm_action" for t in tools):
        issues.append("committed_action_without_observed_confirmation")
    if not transcript or not any(r["role"] == "user" for r in transcript):
        issues.append("no_caller_transcript")
    created = datetime.fromtimestamp(snapshot["session"]["created"],timezone.utc)
    evidence["facts"] = dealership_facts(created.astimezone(TZ).date())
    evidence["state"] = {"scheduler_evidence_available":snapshot.get("source_available",True),"appointments":snapshot["appointments"],"proposals":snapshot["proposals"],
        "handoffs":snapshot["handoffs"],"pending_operations":[r for r in snapshot["operations"] if r["status"] in {"queued","running","unknown"}]}
    # Do not send DB session ids, fingerprints, tokens or provider signatures.
    for a in evidence["state"]["appointments"]:
        a.pop("session_id",None);a.pop("tenant",None)
    for p in evidence["state"]["proposals"]:
        p.pop("session_id",None);p.pop("tenant",None)
    checks = {"booking_integrity":integrity(snapshot)}
    prepares = {t["server_result"].get("proposal_id"):t for t in tools if t["name"] in {"prepare_booking","prepare_first_available_booking","prepare_reschedule","prepare_cancellation"} and isinstance(t["server_result"],dict) and t["server_result"].get("ok")}
    confirmations = [t for t in tools if t["name"] == "confirm_action"]
    order = []
    for t in confirmations:
        p = prepares.get(t["arguments"].get("proposal_id"))
        if not p or t["user_count"] is None or p["user_count"] is None:
            order.append("uncertain")
        elif t["user_count"] <= p["user_count"] or t["sequence"] <= p["sequence"]:
            order.append("fail")
        else:order.append("pass")
    verdict = "fail" if "fail" in order else "uncertain" if "uncertain" in order or committed_actions and not order else "pass" if order else "not_applicable"
    checks["confirmation_order"] = check(verdict,"later_user_turn_required_not_proof_of_assent",["state"])
    checks["evidence_complete"] = check("uncertain" if issues else "pass","incomplete_provider_or_receipt_evidence" if issues else "text_and_tool_receipts_present",["state"])
    if snapshot.get("source_available") is False:
        issues.append("scheduler_session_not_bound")
        checks["booking_integrity"]=check("uncertain","scheduler_evidence_unavailable",["state"])
        checks["confirmation_order"]=check("uncertain","scheduler_evidence_unavailable",["state"])
        checks["evidence_complete"]=check("uncertain","scheduler_evidence_unavailable",["state"])
    returned = set()
    for tool in tools:
        result = tool.get("server_result") or {}
        appointment = result.get("appointment") or result.get("current_appointment") or {}
        if isinstance(appointment,dict) and isinstance(appointment.get("reference"),str):returned.add(appointment["reference"].upper())
    spoken = {m.group(0).upper() for row in transcript if row["role"] == "assistant" for m in REFERENCE.finditer(row["text"])}
    reference_unknown=bool(spoken-returned or returned and not spoken)
    checks["reference_consistency"] = check("uncertain" if reference_unknown else "pass" if spoken else "not_applicable",
        "unverified_literal_reference_requires_review" if spoken-returned else
        "returned_reference_missing_complete_literal_requires_review" if returned and not spoken else
        "literal_reference_tokens_only_not_audio",["state"])
    safe = Redactor(evidence).value(evidence)
    package = {"rubric_version":VERSION,"dimensions":list(DIMENSIONS),"evidence":safe,"mechanical_checks":checks,
               "evidence_issues":sorted(set(issues)),"limitations":["Stored transcript, not verified audio.","Same-model advisory judge; no guarantee of zero hallucinations."]}
    if len(canonical(package).encode()) > MAX_EVIDENCE_BYTES:raise EvidenceError("evidence_too_large")
    return package


def latency(call, timings=()):
    metrics = (call.get("artifact") or {}).get("performanceMetrics") or {}
    turns = metrics.get("turnLatencies") or []
    values = [r.get("turnLatency") for r in turns if isinstance(r,dict) and numeric(r.get("turnLatency")) is not None and r["turnLatency"]>0] if isinstance(turns,list) else []
    # Vapi can fill summary averages with zero when a call contains only a
    # greeting. No measured response is unknown, never an instant reply.
    has_response = bool(values) or (numeric(metrics.get("turnLatencyAverage")) or 0)>0
    names = {"turnLatency":"turn","modelLatency":"model","voiceLatency":"voice",
             "endpointingLatency":"endpointing","transcriberLatency":"transcriber"}
    result = {}
    for source,dest in names.items():
        value = numeric(metrics.get(source+"Average"))
        if value is not None and has_response:
            if dest=="turn" and value==0:
                value=round(sum(values)/len(values),3) if values else None
            if value is not None:result[dest+"_avg_ms"] = value
    if values:result["turn_p95_ms"] = percentile(values);result["measured_turns"] = len(values)
    if metrics and not has_response:result["measurement_status"]="no_measured_response_turns"
    for source,dest in (("fromTransportLatencyAverage","transport_in_avg_ms"),("toTransportLatencyAverage","transport_out_avg_ms")):
        value = numeric(metrics.get(source))
        if value is not None:result[dest] = value
    values = [r.get("milestones_ms",{}).get("first_text") for r in timings if r.get("outcome") == "completed"]
    values = [v for v in values if numeric(v) is not None]
    if values:result["server_first_text_p95_ms"] = percentile(values)
    acknowledged=[r for r in timings if isinstance(r,dict) and isinstance(r.get("backchannel"),dict)
                  and numeric(r["backchannel"].get("emitted_ms")) is not None]
    if acknowledged:
        ack_times=[r["backchannel"]["emitted_ms"] for r in acknowledged]
        result["acknowledgment_observed"]=True
        result["server_first_ack_p95_ms"]=percentile(ack_times)
        result["server_acknowledgments_emitted"]=len(acknowledged)
        if values:result["server_substantive_text_p95_ms"]=percentile(values)
        result["provider_latency_scope"]="may_include_acknowledgments_not_substantive_answers"
    result["interpretation"] = "Vapi-reported milliseconds; server first text excludes speech and transport. No acoustic calibration."
    return result


def read_timings(path,call_id):
    if not Path(path).is_file():return []
    try:
        with closing(readonly(path)) as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM gateway_timings WHERE call_key=? ORDER BY started LIMIT 500",(hashlib.sha256(call_id.encode()).hexdigest()[:16],))]
    except sqlite3.Error:return []

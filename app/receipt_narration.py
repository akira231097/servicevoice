"""Read-only presentation of fresh, observed results; never consent or effects.

The caller must first authenticate and restore the complete signed/observed
history. This optional optimization uses the issuing model's typed language
and timezone preference, not a language/assent classifier. Missing context
returns to the ordinary model path.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool

from .domain import DomainError, SERVICES, TENANT, TZ, normalize_services, service_details, service_names
from .tools import DEFINITIONS, Presentation
from .issuance_context import user_context, decode_extras

NARRATOR_VERSION = "receipt-en-2026-10-25-9"
SUPPORTED = {"begin_service_request", "search_availability", "prepare_first_available_booking", "prepare_booking", "prepare_reschedule", "prepare_cancellation", "confirm_action", "request_handoff", "withdraw_handoff"}
_DIGIT_WORDS = dict(zip("0123456789", ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Decision:
    reason: str
    text: str | None = None
    result_sha256: str | None = None


def spoken_date(start, zone):
    value = datetime.fromisoformat(start).astimezone(ZoneInfo(zone))
    hour = value.hour % 12 or 12
    clock = str(hour) + (f":{value.minute:02d}" if value.minute else "")
    regions = {"America/Chicago": "Central", "America/Los_Angeles": "Pacific",
               "America/New_York": "Eastern", "America/Denver": "Mountain"}
    if zone in regions:
        name = regions[zone] + (" Daylight Time" if value.dst() else " Standard Time")
    else:
        offset = value.utcoffset().total_seconds() / 3600
        name = zone.replace("/", " ").replace("_", " ") + f" time, UTC {offset:+g}"
    return f"{value.strftime('%A, %B')} {value.day}, {value.year} at {clock} {value.strftime('%p')} {name}"


def times(start, presentation):
    text = spoken_date(start, TZ.key)
    zone = presentation["caller_timezone"]
    if zone not in ("dealership", TZ.key):
        text += ", which is " + spoken_date(start, zone)
    return text


def spoken_phone(value):
    """Speak each stored digit; never strip, repair or change the phone value.

    A North American number is read the way people say one: three, three and
    four digits, each group as one run, with a pause only between groups and
    without the implied +1 country code. Other international numbers keep
    "plus" and every digit, without guessing their group lengths. Unsupported
    storage shapes use ordinary model narration rather than an incomplete
    deterministic readback.
    """
    if not isinstance(value, str) or not re.fullmatch(r"\+[1-9][0-9]{1,14}", value):
        return None
    digits = value[1:]
    if len(digits) == 11 and digits.startswith("1"):
        groups = (digits[1:4], digits[4:7], digits[7:])
        return ", ".join(" ".join(_DIGIT_WORDS[digit] for digit in group) for group in groups)
    return "plus " + " ".join(_DIGIT_WORDS[digit] for digit in digits)


def candidate(messages):
    if len(messages) < 2 or messages[-1].get("role") != "tool":
        return Decision("not_immediate_tool_result")
    prior, last = messages[-2:]
    calls = prior.get("tool_calls")
    if prior.get("role") != "assistant" or not isinstance(calls, list) or len(calls) != 1:
        return Decision("tool_batch_or_history_shape")
    tool = calls[0]
    if tool["id"] != last.get("tool_call_id"):
        return Decision("tool_batch_or_history_shape")
    name = tool["function"]["name"]
    if name not in SUPPORTED:
        return Decision("unsupported_tool")
    try:
        arguments = DEFINITIONS[name][0].model_validate(json.loads(tool["function"]["arguments"])).model_dump()
        if name == "confirm_action" and arguments["confirmed"] is not True:
            return Decision("confirmation_not_asserted")
        result = json.loads(last["content"])
        if result.get("ok") is not True or "error" in result:
            return Decision("unsuccessful_result")
        context = arguments.get("presentation")
        if context is None or result.get("presentation") != context:
            return Decision("presentation_missing_or_changed")
        context = Presentation.model_validate(context).model_dump()
        # The fixed US-dealership copy supports these exact validated tags.
        # Preserve the original tag in signatures, fingerprints and receipts.
        if context["response_language"] not in {"en", "en-US"}:
            return Decision("unsupported_language")
        receipt = result.get("_receipt")
        if not isinstance(receipt, dict) or set(receipt) != {"operation_id", "result_sha256"}:
            return Decision("receipt_missing")
        raw = {key: value for key, value in result.items() if key != "_receipt"}
        digest = hashlib.sha256(canonical(raw).encode()).hexdigest()
        if receipt["result_sha256"] != digest:
            return Decision("receipt_mismatch")
        return name, tool["id"], arguments, raw, receipt, context, digest, user_context(messages[:-2])
    except (ValueError, TypeError, KeyError, AttributeError):
        return Decision("presentation_invalid")


def select(store, sid, provider_call_id, prepared, *, phone=False):
    """Read one consistent snapshot and fall back on any uncertain state."""
    if isinstance(prepared, Decision):
        return prepared
    name, tool_id, args, result, receipt, context, digest, originating_user_context = prepared
    expected_op = "op_" + hashlib.sha256((sid + "\0" + tool_id).encode()).hexdigest()[:24]
    if receipt["operation_id"] != expected_op:
        return Decision("receipt_mismatch")
    try:
        with store.read_connection(0.1) as db:
            db.execute("BEGIN")
            session = db.execute("SELECT * FROM sessions WHERE id=? AND tenant=?", (sid, TENANT)).fetchone()
            if not session or session["ended"] or session["provider_call_id"] != provider_call_id or time.time() - session["created"] > 4 * 3600:
                return Decision("inactive_session")
            signature = db.execute("SELECT extras,expires FROM gemini_tool_signatures WHERE session_id=? AND provider_call_id=? AND tool_call_id=?", (sid, provider_call_id, tool_id)).fetchone()
            if not signature or signature["expires"] < time.time():
                return Decision("issuance_context_missing")
            _, issued_context = decode_extras(json.loads(signature["extras"]))
            if issued_context is None:
                return Decision("issuance_context_missing")
            if issued_context != originating_user_context:
                return Decision("issuance_context_changed")
            cached = db.execute("SELECT rowid,* FROM tool_results WHERE session_id=? AND call_id=?", (sid, tool_id)).fetchone()
            expected_fp = hashlib.sha256(json.dumps({"name": name, "args": args}, sort_keys=True).encode()).hexdigest()
            if not cached or cached["fingerprint"] != expected_fp or json.loads(cached["result"]) != result:
                return Decision("durable_result_mismatch")
            if db.execute("SELECT 1 FROM tool_results WHERE session_id=? AND rowid>? LIMIT 1", (sid, cached["rowid"])).fetchone():
                return Decision("newer_result")
            if db.execute("""SELECT 1 FROM tool_invocations i WHERE i.session_id=? AND i.call_id!=?
                AND NOT EXISTS (SELECT 1 FROM tool_results r WHERE r.session_id=i.session_id AND r.call_id=i.call_id) LIMIT 1""", (sid, tool_id)).fetchone():
                return Decision("unresolved_operation")
            state = json.loads(session["state"])
            if name in ("begin_service_request", "search_availability"):
                # A bounded list is not a statement about every time that day.
                # Recheck the same query against current capacity before speaking;
                # a changed result falls back to normal reconciliation.
                selected = normalize_services(args["service"], args.get("services"))
                fresh = store.availability_result(db, args["service"], args.get("date"), args.get("time_filter"), services=selected)
                if (any(result.get(key) != value for key, value in fresh.items())
                        or state.get("service") != args["service"]
                        or normalize_services(state["service"], state.get("services")) != selected
                        or state.get("availability_query") != fresh["query"]
                        or state.get("slots") != fresh["slots"]
                        or state.get("proposal_id")
                        or state.get("stage") != ("slot" if fresh["slots"] else "date")):
                    return Decision("availability_changed")
                if fresh["slots"]:
                    offers = fresh["slots"][:2]
                    scope = service_names(selected)
                    text = f"Available {scope} intake times are " + " or ".join(times(slot["start"], context) for slot in offers) + ". "
                    if len(selected) > 1:
                        text += "Each offer is one 60-minute intake visit covering all those services; repair completion is not promised. "
                    if fresh["total_matching"] > len(offers):
                        text += "There are more matching times. "
                    text += "These are offers, not reservations. Which works for you?"
                    if phone:
                        text = phone_offers(offers, selected, fresh, context)
                else:
                    if fresh["complete"] is not True or fresh["total_matching"] != 0:
                        return Decision("availability_incomplete")
                    text = "No openings match your requested search within our current scheduling window. Would you like to check a different date or time?"
            elif name.startswith("prepare_"):
                proposal = db.execute("SELECT * FROM proposals WHERE id=? AND session_id=? AND tenant=?", (result.get("proposal_id"), sid, TENANT)).fetchone()
                if not proposal or proposal["state"] != "pending" or proposal["expires"] <= time.time() or state.get("proposal_id") != proposal["id"]:
                    return Decision("stale_proposal")
                payload = json.loads(proposal["payload"])
                if payload.get("presentation") != context or payload["summary"] != result.get("summary"):
                    return Decision("proposal_mismatch")
                if proposal["kind"] == "book":
                    selected = normalize_services(payload["service"], payload.get("services"))
                    if any(result.get(key) != value for key, value in service_details(selected).items()):
                        return Decision("proposal_mismatch")
                    slot = db.execute("SELECT start FROM slots WHERE id=? AND tenant=?", (payload["slot_id"], TENANT)).fetchone()
                    customer = payload["customer"]
                    # Decline markup/audio-tag syntax rather than alter customer data.
                    if any(re.search(r"[<>\[\]\x00-\x1f]", customer[key]) for key in ("name", "vehicle", "phone")):
                        return Decision("unsafe_display_text")
                    if not slot:return Decision("proposal_mismatch")
                    callback = spoken_phone(customer["phone"])
                    if callback is None:return Decision("unsupported_phone_format")
                    scope = service_names(selected)
                    if len(selected) > 1:
                        scope = f"One 60-minute intake visit covering {scope}"
                    text = f"{scope} for {customer['name']}, {customer['vehicle']}, on {times(slot['start'], context)}. Callback {callback}. "
                    if len(selected) > 1:
                        text += "Repair completion is not promised. "
                    text += "Would you like me to confirm this booking?"
                else:
                    appointment = store.appointment(db, sid)
                    if not appointment or appointment["id"] != payload["appointment_id"] or appointment["status"] != "confirmed":
                        return Decision("appointment_changed")
                    current = normalize_services(appointment["service"], appointment.get("services"))
                    if "previous_services" in payload and payload["previous_services"] != current:
                        return Decision("appointment_changed")
                    if proposal["kind"] == "cancel":
                        if any(result.get(key) != value for key, value in service_details(current).items()):
                            return Decision("proposal_mismatch")
                        text = f"Cancel appointment {appointment['reference']} for {appointment['service_name']} on {times(appointment['start'], context)}. Would you like me to confirm this cancellation?"
                    elif proposal["kind"] == "reschedule":
                        slot = db.execute("SELECT start FROM slots WHERE id=? AND tenant=?", (payload["slot_id"], TENANT)).fetchone()
                        if not slot:return Decision("proposal_mismatch")
                        selected = normalize_services(payload["services"][0], payload["services"]) if "services" in payload else current
                        if any(result.get(key) != value for key, value in service_details(selected).items()):
                            return Decision("proposal_mismatch")
                        if selected != current or len(selected) > 1:
                            text = f"Update appointment {appointment['reference']} to one 60-minute intake visit covering {service_names(selected)} on {times(slot['start'], context)}, for {appointment['customer_name']}, {appointment['vehicle']}. Repair completion is not promised. Would you like me to confirm this change?"
                        else:
                            text = f"Move appointment {appointment['reference']} from {times(appointment['start'], context)} to {times(slot['start'], context)}. Service and vehicle stay the same. Would you like me to confirm this change?"
                    else:return Decision("proposal_mismatch")
            elif name == "confirm_action":
                appointment = store.appointment(db, sid)
                if state.get("proposal_id") or state.get("stage") != "complete" or appointment != result.get("appointment"):
                    return Decision("appointment_changed")
                action = result.get("action")
                if action not in ("book", "reschedule", "cancel") or appointment["status"] != ("cancelled" if action == "cancel" else "confirmed"):
                    return Decision("appointment_changed")
                text = f"Your appointment for {appointment['service_name']} is {appointment['status']} for {times(appointment['start'], context)}. Your appointment reference is {appointment['reference']}."
            elif name in ("request_handoff", "withdraw_handoff"):
                saved = db.execute("SELECT * FROM handoffs WHERE session_id=? AND id=?", (sid, result.get("id"))).fetchone()
                if not saved or any(result.get(key) != saved[key] for key in saved.keys()):
                    return Decision("handoff_changed")
                if name == "request_handoff":
                    if saved["status"] != "callback_requested" or state.get("stage") != "handoff" or state.get("proposal_id"):
                        return Decision("handoff_changed")
                    text = "Your request was saved in the local demo advisor queue. No one was contacted, and this demo cannot transfer you or arrange a callback."
                else:
                    if saved["status"] != "withdrawn" or state.get("stage") == "handoff":
                        return Decision("handoff_changed")
                    text = "Your local advisor request is withdrawn. No appointment was changed."
            else:
                return Decision("unsupported_tool")
            return Decision("verified_receipt", text, digest)
    except (sqlite3.Error, DomainError, ValueError, TypeError, KeyError, AttributeError):
        return Decision("state_unavailable")


def phone_offers(offers, selected, fresh, context):
    """Shorter wording over the same verified offers and timezone metadata."""
    local=[datetime.fromisoformat(slot['start']).astimezone(TZ) for slot in offers]
    if (len(local)==2 and local[0].date()==local[1].date()
            and context['caller_timezone'] in {'dealership',TZ.key}):
        # State the date and timezone once, retaining both exact local times.
        first=times(offers[0]['start'],context)
        second=local[1]
        clock=str(second.hour%12 or 12)+(f':{second.minute:02d}' if second.minute else '')+' '+second.strftime('%p')
        text=f"I can offer {service_names(selected)} on {first}, or {clock} that day. "
    else:
        text=f"I can offer {service_names(selected)} on "+' or '.join(times(slot['start'],context) for slot in offers)+'. '
    if len(selected)>1:text+='One 60-minute intake covers those services; repair completion is not promised. '
    if fresh['total_matching']>len(offers):text+='Other times are available. '
    return text+'Which works for you?'


class BoundedSelector:
    """Timed-out/cancelled readers retain their worker slot until completion."""
    def __init__(self, store, timeout_seconds=0.25, capacity=4):
        self.store, self.timeout, self.capacity = store, timeout_seconds, capacity
        self.pending = set()

    async def choose(self, sid, call_id, messages, *, phone=False):
        prepared = candidate(messages)
        if isinstance(prepared, Decision):return prepared
        if len(self.pending) >= self.capacity:return Decision("read_capacity")
        task = asyncio.create_task(run_in_threadpool(select, self.store, sid, call_id, prepared, **({"phone":True} if phone else {})))
        self.pending.add(task)
        def done(value):
            self.pending.discard(value)
            if not value.cancelled():value.exception()
        task.add_done_callback(done)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except TimeoutError:
            return Decision("read_deadline")
        except Exception:
            return Decision("state_unavailable")

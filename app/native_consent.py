"""Server-side readback and assent check for provider-native phone confirmations.

A Vapi-hosted voice model (GPT-Live) issues its own tool calls, so the gateway
issuance record that protects the Gemini path does not exist. Before a pending
proposal is committed, this check reads the live call artifact that Vapi sends
with the tool call: assistant speech after the proposal was prepared must cover
its key facts, and the caller must then have said an explicit yes that no later
caller turn withdraws.

This is a transcript check. It does not prove what the caller heard. A
transcription error can block a genuine confirmation (the model is told to read
back again) but cannot turn a refusal into consent.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re

from app.domain import TENANT, TZ

PREPARE_TOOLS = frozenset({"prepare_booking", "prepare_first_available_booking",
                           "prepare_reschedule", "prepare_cancellation"})
# Provider timestamps for the end of assistant speech and the start of caller
# speech are approximate; a yes that starts this close to the end still counts.
OVERLAP_TOLERANCE_MS = 600

SERVICE_PATTERNS = {
    "oil_change": r"\boil\b",
    "tires": r"\btires?\b|\brotation\b",
    "brakes": r"\bbrakes?\b",
    "diagnostic": r"\bdiagnos\w*",
}
DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9"}
CLOCK_WORDS = {"forty-five": "45", "forty five": "45", "thirty": "30", "fifteen": "15", "twelve": "12",
               "eleven": "11", "ten": "10", **{k: v for k, v in DIGIT_WORDS.items() if k != "oh"}}
NEGATIVE = re.compile(r"\b(no|nope|nah|not|don't|dont|do not|wait|hold on|hang on|actually|change|instead|"
                      r"wrong|incorrect|never ?mind|but|cancel that|scratch that)\b")
QUESTION_START = re.compile(r"^(what|when|where|which|who|why|how|can|could|would|will|is|are|do|does|did|should)\b")
BACKCHANNEL_ONLY = re.compile(r"^(m+h?m+|mm+-?hmm+|mhm+|uh-?huh|uh+|um+|hmm+|ah+|oh+)$")
ASSENT = re.compile(r"\b(yes|yeah|yep|yup|ya|sure|correct|right|sounds good|go ahead|book it|do it|please do|"
                    r"confirm|confirmed|perfect|absolutely|definitely|that works|works for me|okay|ok|alright|"
                    r"all right|great|exactly|affirmative)\b")


def _text(value):
    return value if isinstance(value, str) else ""


def _normalize(text):
    text = _text(text).lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", text)
    text = text.replace("o'clock", " ")
    return re.sub(r"\s+", " ", text).strip()


def _digits(text):
    value = re.sub(r"\bdouble (zero|oh|one|two|three|four|five|six|seven|eight|nine)\b",
                   lambda m: f"{m.group(1)} {m.group(1)}", _normalize(text))
    return "".join(token if token.isdigit() else DIGIT_WORDS.get(token, "")
                   for token in re.findall(r"[a-z]+|\d+", value))


def _clock_mentions(text):
    """Spoken clock mentions as (hour, minute, meridiem or None)."""
    value = _normalize(text)
    for word, number in sorted(CLOCK_WORDS.items(), key=lambda item: -len(item[0])):
        value = re.sub(rf"\b{word}\b", number, value)
    # Digit-by-digit phone numbers must not count as clock times.
    value = re.sub(r"\b(?:\d[\s,-]+){3,}\d\b", " ", value)
    value = re.sub(r"\bhalf past (\d{1,2})\b", r"\1:30", value)
    value = re.sub(r"\b(\d{1,2}) (15|30|45)\b", r"\1:\2", value)
    found = []
    for match in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|in the morning|in the afternoon|in the evening)?\b", value):
        hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if 1 <= hour <= 12 and minute < 60:
            meridiem = {"in the morning": "am", "in the afternoon": "pm", "in the evening": "pm"}.get(meridiem, meridiem)
            found.append((hour, minute, meridiem))
    return found


def mentions_time(text, start):
    hour, meridiem = start.hour % 12 or 12, "am" if start.hour < 12 else "pm"
    said = [m for h, minute, m in _clock_mentions(text) if (h, minute) == (hour, start.minute)]
    # The right hour in the wrong half of the day is a wrong readback.
    return bool(said) and all(m in (None, meridiem) for m in said)


def mentions_day(text, start, today):
    value = _normalize(text)
    weekday, month, day = start.strftime("%A").lower(), start.strftime("%B").lower(), start.day
    if re.search(rf"\b{weekday}\b", value):
        return True
    if re.search(rf"\b{month}(?: the)? {day}(?:st|nd|rd|th)?\b", value) or re.search(rf"\bthe {day}(?:st|nd|rd|th)\b", value):
        return True
    if start.date() == today and re.search(r"\btoday\b", value):
        return True
    return start.date() == today + timedelta(days=1) and bool(re.search(r"\btomorrow\b", value))


def classify_answer(text):
    reply = _normalize(text).strip(" .!,")
    reply = re.sub(r"^((um+|uh+|well|oh|so)[ ,]+)+", "", reply)
    if not reply:
        return "empty"
    if BACKCHANNEL_ONLY.match(reply):
        return "backchannel"
    if NEGATIVE.search(reply):
        return "negated_or_changed"
    if reply.endswith("?") or QUESTION_START.match(reply):
        return "question"
    return "explicit_yes" if ASSENT.search(reply) else "not_assent"


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _messages(artifact):
    """Ordered messages from Vapi's live artifact, or None if unusable."""
    if not isinstance(artifact, dict) or not isinstance(artifact.get("messages"), list):
        return None
    rows = []
    for index, item in enumerate(artifact["messages"]):
        if isinstance(item, dict) and isinstance(item.get("role"), str):
            rows.append({"role": item["role"], "text": _text(item.get("message")), "time": _number(item.get("time")),
                         "end": _number(item.get("endTime")), "name": item.get("name"), "result": item.get("result"),
                         "index": index})
    return rows


def _anchor(rows, proposal_id, created_ms):
    """Index after which a readback of this proposal can occur."""
    for row in reversed(rows):
        if row["role"] == "tool_call_result" and row["name"] in PREPARE_TOOLS:
            try:
                result = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(result, dict) and result.get("proposal_id") == proposal_id:
                return row["index"], "tool_result"
    if created_ms is not None:
        later = [row for row in rows if row["time"] is not None and row["time"] >= created_ms - 1000]
        if later:
            return later[0]["index"] - 1, "proposal_time"
    return None, "missing"


def assess(artifact, proposal, facts, *, today=None):
    """Return (verified, evidence). Evidence holds codes only, never caller words.

    ``proposal``: the pending row (id, kind, expires). ``facts``: ``start`` (slot
    ISO time) plus ``services`` and, for bookings, ``customer`` name and phone.
    """
    evidence = {"source": "vapi_live_artifact", "anchor": "missing", "assent": "missing", "facts": {}, "reasons": []}
    if facts is None:
        evidence["reasons"].append("proposal_facts_unavailable")
        return False, evidence
    rows = _messages(artifact)
    if not rows:
        evidence["reasons"].append("transcript_unavailable")
        return False, evidence
    created_ms = (float(proposal["expires"]) - 300) * 1000 if _number(proposal.get("expires")) is not None else None
    anchor, evidence["anchor"] = _anchor(rows, proposal["id"], created_ms)
    if anchor is None:
        evidence["reasons"].append("readback_anchor_missing")
        return False, evidence
    spoken_rows = [row for row in rows if row["index"] > anchor and row["role"] in ("bot", "assistant", "user")]
    answers = [(row, classify_answer(row["text"])) for row in spoken_rows if row["role"] == "user" and row["text"].strip()]
    yes = [pos for pos, (_, kind) in enumerate(answers) if kind == "explicit_yes"]
    if not yes:
        evidence["assent"] = answers[-1][1] if answers else "missing"
        evidence["reasons"].append("assent_not_explicit")
        return False, evidence
    answer = answers[yes[-1]][0]
    withdrawn = [kind for _, kind in answers[yes[-1] + 1:] if kind in ("negated_or_changed", "question")]
    evidence["assent"] = "withdrawn_later" if withdrawn else "explicit_yes"
    if withdrawn:
        evidence["reasons"].append("later_caller_change")
    readback_rows = [row for row in spoken_rows if row["role"] in ("bot", "assistant") and row["index"] < answer["index"]]
    readback = " ".join(row["text"] for row in readback_rows)
    if not readback.strip():
        evidence["reasons"].append("readback_missing")
        return False, evidence
    if (answer["time"] is not None and readback_rows[-1]["end"] is not None
            and answer["time"] < readback_rows[-1]["end"] - OVERLAP_TOLERANCE_MS):
        evidence["assent"] = "during_readback"
        evidence["reasons"].append("answer_during_readback")
    start = datetime.fromisoformat(facts["start"]).astimezone(TZ)
    checks = {"day": mentions_day(readback, start, today or datetime.now(TZ).date()),
              "time": mentions_time(readback, start)}
    kind, normalized = proposal.get("kind"), _normalize(readback)
    services = [item for item in facts.get("services") or [] if item in SERVICE_PATTERNS]
    if kind in ("book", "reschedule") and services:
        checks["services"] = all(re.search(SERVICE_PATTERNS[item], normalized) for item in services)
    if kind == "book":
        customer = facts.get("customer") or {}
        first = (_text(customer.get("name")).strip().split() or [""])[0].lower()
        checks["name"] = len(first) >= 2 and bool(re.search(rf"\b{re.escape(first)}\b", normalized))
        phone = re.sub(r"\D", "", _text(customer.get("phone")))
        checks["callback_number"] = len(phone) >= 4 and phone[-4:] in _digits(readback)
    if kind == "cancel":
        checks["cancel_named"] = bool(re.search(r"\bcancel", normalized))
    evidence["facts"] = checks
    missing = [name for name, ok in checks.items() if not ok]
    if missing:
        evidence["missing_facts"] = missing
        evidence["reasons"].append("readback_incomplete")
    return not evidence["reasons"], evidence


def pending_facts(db, sid, proposal_id):
    """(proposal, facts) for this session's pending proposal, else None.

    None means the domain decides alone: a committed proposal replays its
    receipt and a missing/stale one is refused there without any effect.
    """
    if not isinstance(proposal_id, str):
        return None
    row = db.execute("SELECT id,kind,payload,expires,state FROM proposals WHERE id=? AND session_id=? AND tenant=?",
                     (proposal_id, sid, TENANT)).fetchone()
    if not row or row["state"] != "pending":
        return None
    payload = json.loads(row["payload"])
    if row["kind"] in ("book", "reschedule"):
        slot = db.execute("SELECT start FROM slots WHERE id=? AND tenant=?", (payload.get("slot_id"), TENANT)).fetchone()
        services = payload.get("services") or ([payload["service"]] if row["kind"] == "book" and payload.get("service") else [])
    elif row["kind"] == "cancel":
        slot = db.execute("SELECT s.start FROM appointments a JOIN slots s ON s.id=a.slot_id WHERE a.id=? AND a.tenant=?",
                          (payload.get("appointment_id"), TENANT)).fetchone()
        services = []
    else:
        slot = None
    if not slot:
        # Unknown kinds and vanished slots cannot be checked; refuse below.
        return {"id": row["id"], "kind": row["kind"], "expires": row["expires"]}, None
    return ({"id": row["id"], "kind": row["kind"], "expires": row["expires"]},
            {"start": slot["start"], "services": services, "customer": payload.get("customer")})


def refusal(evidence):
    """Tool result for the reasoner; names missing facts, never caller words."""
    labels = {"day": "the day", "time": "the time", "services": "the service", "name": "the caller's name",
              "callback_number": "the callback number", "cancel_named": "that it will be cancelled"}
    missing = evidence.get("missing_facts") or []
    detail = (" Missing from the readback: " + ", ".join(labels.get(item, item) for item in missing) + ".") if missing else ""
    return {"ok": False, "error": "confirmation_not_verified",
            "message": ("Nothing was saved. Read the pending details back to the caller (service, day and time; for a new "
                        "booking also their name and callback number), ask whether to go ahead, and call confirm_action "
                        "only after the caller clearly says yes." + detail)}

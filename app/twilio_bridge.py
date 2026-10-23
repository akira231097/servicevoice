"""Isolated Twilio conference primitives. Disabled until explicitly configured.

Provider acceptance is not audio playback. Non-idempotent REST operations are
never retried: an ambiguous response is reconciled by ending the owned room.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit
from uuid import UUID
import xml.etree.ElementTree as ET

import httpx


class BridgeError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def sid(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"[a-fA-F0-9]{32}", value) is not None


def flag(values, name):
    value = values.get(name, "off")
    if value not in (None, "", "off", "on"):
        raise ValueError("Invalid Twilio feature flag.")
    return value == "on"


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str = field(repr=False)
    api_key_sid: str
    api_key_secret: str = field(repr=False)
    phone_number: str
    public_base: str
    sip_uri: str = field(repr=False)
    bridge_secret: str = field(repr=False)
    topology: str = "conference"
    jitter: str = "small"
    listening: bool = False
    # P0 must pin this JSON object path from authenticated provider evidence.
    # An absent path deliberately refuses admission; never search arbitrary fields.
    sip_headers_path: str = ""

    def __post_init__(self):
        parsed = urlsplit(self.public_base)
        if (not sid(self.account_sid, "AC") or not sid(self.api_key_sid, "SK")
                or not re.fullmatch(r"\+1[2-9]\d{9}", self.phone_number or "")
                or any(not isinstance(v, str) or not 32 <= len(v) <= 512
                       or any(ord(c) < 33 or ord(c) > 126 for c in v)
                       for v in (self.auth_token, self.api_key_secret, self.bridge_secret))
                or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.port is not None
                or self.public_base.endswith("/") or "%" in self.public_base
                or not re.fullmatch(r"sip:[a-z0-9]{32}@sip\.vapi\.ai", self.sip_uri or "")
                or self.topology not in {"conference", "direct_sip"}
                or self.jitter not in {"small", "off"}
                or self.sip_headers_path and not re.fullmatch(r"(?:call|sip)[.A-Za-z0-9_]{0,100}", self.sip_headers_path)):
            raise ValueError("Invalid isolated Twilio configuration.")

    @classmethod
    def from_mapping(cls, values):
        if not flag(values, "SERVICEVOICE_TWILIO_LANE"):
            return None
        # This first release cannot accidentally enable a production deployment.
        if values.get("SERVICEVOICE_ENVIRONMENT") not in {"local", "development", "staging"}:
            raise ValueError("Twilio listening lane is experimental; isolated environments only.")
        return cls(*(values.get(k, "") for k in (
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID",
            "TWILIO_API_KEY_SECRET", "TWILIO_PHONE_NUMBER", "SERVICEVOICE_TWILIO_PUBLIC_BASE_URL",
            "SERVICEVOICE_TWILIO_SIP_URI", "SERVICEVOICE_TWILIO_BRIDGE_SECRET")),
            topology=values.get("SERVICEVOICE_TWILIO_TOPOLOGY", "conference"),
            jitter=values.get("SERVICEVOICE_TWILIO_JITTER", "small"),
            listening=flag(values, "SERVICEVOICE_LISTENING_CUES"),
            sip_headers_path=values.get("SERVICEVOICE_TWILIO_SIP_HEADERS_PATH", ""))

    def url(self, suffix):
        return self.public_base + "/twilio/" + suffix


def signature(token, url, pairs):
    """Twilio form algorithm, including sorted unique repeated field values.

Unknown fields participate in validation. Domain/URL come from trusted config,
never a Host or forwarded header. JSON and duplicate control fields are refused
by the HTTP boundary, rather than ambiguously reparsed after verification.
"""
    groups = {}
    for key, value in pairs:
        groups.setdefault(key, set()).add(value)
    value = url + "".join(key + item for key in sorted(groups) for item in sorted(groups[key]))
    return base64.b64encode(hmac.new(token.encode(), value.encode(), hashlib.sha1).digest()).decode()


def verify_signature(config, url, pairs, supplied):
    if not isinstance(supplied, str) or len(supplied) > 128:
        return False
    return hmac.compare_digest(signature(config.auth_token, url, pairs).encode(), supplied.encode())


def xml_response(root):
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def conference_twiml(config, bridge_id):
    response = ET.Element("Response")
    dial = ET.SubElement(response, "Dial", timeLimit="600")
    node = ET.SubElement(dial, "Conference", beep="false", waitUrl="",
        startConferenceOnEnter="true", endConferenceOnExit="true", maxParticipants="2",
        jitterBufferSize=config.jitter, participantLabel="caller",
        statusCallback=config.url("conference-status"), statusCallbackMethod="POST",
        statusCallbackEvent="start end join leave announcement")
    node.text = "sv-" + bridge_id
    ET.SubElement(response, "Hangup")
    return xml_response(response)


def direct_twiml(config, bridge_id, sip_target):
    response = ET.Element("Response")
    dial = ET.SubElement(response, "Dial", timeLimit="600", timeout="15", answerOnBridge="true",
        action=config.url("dial-ended") + "?bridge=" + bridge_id, method="POST")
    ET.SubElement(dial, "Sip").text = sip_target
    ET.SubElement(response, "Hangup")
    return xml_response(response)


def unavailable_twiml():
    response = ET.Element("Response")
    ET.SubElement(response, "Say").text = "Sorry, the test assistant is unavailable. Please try again later."
    ET.SubElement(response, "Hangup")
    return xml_response(response)


class BridgeStore:
    """Durable ownership, one dial claim, replay protection, bounded admission.

Unreconciled rows continue to occupy capacity across restarts. They cannot be
silently retired while provider legs might still be billable.
"""
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = str(path), clock
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS twilio_bridges (
                id TEXT PRIMARY KEY, caller_sid TEXT NOT NULL UNIQUE,
                conference_sid TEXT UNIQUE, vapi_leg_sid TEXT UNIQUE,
                provider_call_id TEXT UNIQUE, session_id TEXT,
                state TEXT NOT NULL, created REAL NOT NULL, ended REAL,
                proof_expiry INTEGER NOT NULL, proof_hash TEXT,
                cleanup_verified INTEGER NOT NULL DEFAULT 0,
                dial_pending INTEGER NOT NULL DEFAULT 0)""")
            if "dial_pending" not in {row[1] for row in db.execute("PRAGMA table_info(twilio_bridges)")}:
                db.execute("ALTER TABLE twilio_bridges ADD COLUMN dial_pending INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=1.0)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, bridge_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM twilio_bridges WHERE id=?", (bridge_id,)).fetchone()
            return dict(row) if row else None

    def active(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM twilio_bridges WHERE cleanup_verified=0")]

    def allocate(self, caller_sid):
        if not sid(caller_sid, "CA"):
            raise BridgeError("invalid_caller", 422)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM twilio_bridges WHERE caller_sid=?", (caller_sid,)).fetchone()
            if previous:
                return dict(previous)
            now = self.clock()
            if db.execute("SELECT COUNT(*) FROM twilio_bridges").fetchone()[0] >= 50:
                raise BridgeError("bridge_test_budget", 429)
            if db.execute("SELECT COUNT(*) FROM twilio_bridges WHERE cleanup_verified=0").fetchone()[0] >= 2:
                raise BridgeError("bridge_capacity", 429)
            if db.execute("SELECT COUNT(*) FROM twilio_bridges WHERE created>?", (now-60,)).fetchone()[0] >= 10:
                raise BridgeError("bridge_rate_limit", 429)
            bridge_id = secrets.token_hex(16)
            db.execute("INSERT INTO twilio_bridges(id,caller_sid,state,created,proof_expiry) VALUES (?,?,?,?,?)",
                       (bridge_id, caller_sid, "waiting", now, int(now)+60))
        return self.get(bridge_id)

    def claim_dial(self, bridge_id, conference_sid, caller_sid):
        if not sid(conference_sid, "CF"):
            raise BridgeError("invalid_conference", 422)
        with self.db() as db:
            changed = db.execute("""UPDATE twilio_bridges SET state='dialing', conference_sid=?,dial_pending=1
                WHERE id=? AND caller_sid=? AND state='waiting' AND conference_sid IS NULL""",
                (conference_sid, bridge_id, caller_sid)).rowcount
        return bool(changed)

    def set_leg(self, bridge_id, call_sid):
        if not sid(call_sid, "CA"):
            raise BridgeError("invalid_participant", 502)
        with self.db() as db:
            row = db.execute("SELECT vapi_leg_sid FROM twilio_bridges WHERE id=?", (bridge_id,)).fetchone()
            if not row or row[0] not in (None, call_sid):
                raise BridgeError("participant_conflict")
            db.execute("UPDATE twilio_bridges SET vapi_leg_sid=?,dial_pending=0,cleanup_verified=0 WHERE id=?", (call_sid, bridge_id))

    def joined(self, bridge_id):
        with self.db() as db:
            db.execute("UPDATE twilio_bridges SET state='joined' WHERE id=? AND state='dialing'", (bridge_id,))

    def end(self, bridge_id, *, verified=False):
        with self.db() as db:
            db.execute("""UPDATE twilio_bridges SET state='ended',ended=COALESCE(ended,?),
                cleanup_verified=CASE WHEN dial_pending=0 THEN MAX(cleanup_verified,?) ELSE 0 END WHERE id=?""", (self.clock(), int(verified), bridge_id))

    def proof(self, config, row):
        payload = f"{row['id']}:{row['caller_sid']}:{row['proof_expiry']}"
        digest = hmac.new(config.bridge_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return str(row["proof_expiry"]) + "." + digest

    def target(self, config, row):
        return config.sip_uri + "?" + urlencode({"x-sv_bridge": row["id"], "x-sv_proof": self.proof(config, row)})

    def accept_proof(self, config, message):
        value = message
        for key in config.sip_headers_path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if not config.sip_headers_path or not isinstance(value, dict):
            raise BridgeError("sip_header_contract_unverified", 403)
        headers = {}
        for key, item in value.items():
            lower = key.lower().removeprefix("x-") if isinstance(key, str) else ""
            if lower in headers:
                raise BridgeError("ambiguous_sip_proof", 403)
            headers[lower] = item
        bridge_id, supplied = headers.get("sv_bridge"), headers.get("sv_proof")
        call_id = message.get("call", {}).get("id")
        try:
            valid_call = str(UUID(call_id)) == call_id
        except (ValueError, TypeError, AttributeError):
            valid_call = False
        if not valid_call or not isinstance(bridge_id, str) or not isinstance(supplied, str):
            raise BridgeError("invalid_bridge_proof", 403)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM twilio_bridges WHERE id=?", (bridge_id,)).fetchone()
            if not row or row["state"] == "ended" or not hmac.compare_digest(self.proof(config, row).encode(), supplied.encode()):
                raise BridgeError("invalid_bridge_proof", 403)
            if row["provider_call_id"] not in (None, call_id):
                raise BridgeError("replayed_bridge_proof", 403)
            if row["provider_call_id"] is None and self.clock() >= row["proof_expiry"]:
                raise BridgeError("expired_bridge_proof", 403)
            db.execute("UPDATE twilio_bridges SET provider_call_id=?,proof_hash=? WHERE id=?",
                       (call_id, hashlib.sha256(supplied.encode()).hexdigest(), bridge_id))
        return bridge_id

    def bind_session(self, bridge_id, call_id, session_id):
        with self.db() as db:
            if db.execute("UPDATE twilio_bridges SET session_id=? WHERE id=? AND provider_call_id=? AND state!='ended'",
                          (session_id, bridge_id, call_id)).rowcount != 1:
                raise BridgeError("bridge_binding_conflict")


def proof_header_paths(message):
    """Find only structural paths of the two known proof headers for P0.

    Never return values, arbitrary header names, transcript keys or full payload.
    Discovery is evidence for configuration, not permission to admit the call.
    """
    found, remaining = [], [256]
    def walk(value, path, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 6 or not isinstance(value, dict):
            return
        names = {k.lower().removeprefix("x-") for k in value if isinstance(k, str)}
        if {"sv_bridge", "sv_proof"} <= names:
            found.append(".".join(path))
        for key, child in value.items():
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", key):
                walk(child, path+[key], depth+1)
    walk(message.get("call") if isinstance(message, dict) else None, ["call"], 0)
    return tuple(sorted(set(found)))[:4]

class TwilioClient:
    def __init__(self, config, *, client=None):
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(3, connect=1),
            follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
        self.owns_client = client is None
        self.base = "https://api.twilio.com/2010-04-01/Accounts/" + config.account_sid

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def request(self, method, path, data=None):
        try:
            async with self.client.stream(method, self.base + path, data=data,
                    auth=(self.config.api_key_sid, self.config.api_key_secret), follow_redirects=False) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise BridgeError("twilio_response_bounds", 502)
                if response.status_code not in (200, 201):
                    raise BridgeError("twilio_request_failed", 502)
            import json
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError):
            raise BridgeError("twilio_unavailable", 502) from None

    async def add_vapi(self, row, target):
        return await self.request("POST", f"/Conferences/{row['conference_sid']}/Participants.json", {
            "From": self.config.phone_number, "To": target, "Label": "vapi", "Beep": "false",
            "EarlyMedia": "false", "EndConferenceOnExit": "true", "JitterBufferSize": self.config.jitter,
            "Timeout": "15", "TimeLimit": "600", "StatusCallbackMethod": "POST",
            "StatusCallback": self.config.url("participant-status") + "?bridge=" + row["id"],
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"]})

    async def announce(self, row, phrase_id="neutral_ack"):
        if phrase_id not in {"neutral_ack", "acknowledge"}:
            raise BridgeError("cue_not_approved")
        return await self.request("POST", f"/Conferences/{row['conference_sid']}/Participants/{row['caller_sid']}.json",
            {"AnnounceUrl": self.config.url("clip/neutral_ack.wav") + "?bridge=" + row["id"] + "&cue=" + phrase_id, "AnnounceMethod": "GET"})

    async def end_conference(self, conference_sid):
        return await self.request("POST", f"/Conferences/{conference_sid}.json", {"Status": "completed"})

    async def end_call(self, call_sid, *, apology=False):
        return await self.request("POST", f"/Calls/{call_sid}.json",
            {"Twiml": unavailable_twiml()} if apology else {"Status": "completed"})

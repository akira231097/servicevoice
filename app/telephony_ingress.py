"""Trusted inbound-phone admission, separate from browser capabilities.

The HTTP boundary must authenticate ordinary gateway traffic separately and
bound body reading/thread dispatch. These synchronous operations accept its
absolute monotonic deadline. SQLite busy waiting and VM work are bounded;
Python cannot impose a hard deadline on arbitrary operating-system disk I/O.

Provider contract: https://docs.vapi.ai/server-url/events (assistant-request
has a fixed 7.5-second end-to-end budget). No caller number establishes identity.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from uuid import UUID

from app.domain import TENANT, uid
from app.capacity import CapacityError


class IngressError(Exception):
    """Fixed safe code/message; no caller payload or configured identifiers."""

    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class IngressConfig:
    organization_id: str
    phone_number_id: str
    assistant_id: str
    webhook_secret: str = field(repr=False)

    def __post_init__(self):
        if not all(_uuid(value) for value in (self.organization_id, self.phone_number_id, self.assistant_id)):
            raise ValueError("Telephony identity configuration is invalid.")
        if not isinstance(self.webhook_secret, str) or not 32 <= len(self.webhook_secret) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in self.webhook_secret):
            raise ValueError("Telephony credential configuration is invalid.")

    @classmethod
    def from_mapping(cls, values):
        """Disabled unless explicitly enabled; invalid enabled config fails closed."""
        flag = values.get("SERVICEVOICE_TELEPHONY_ENABLED", "off")
        if flag in (None, "", "off"):
            return None
        if flag != "on":
            raise ValueError("Telephony enable flag must be on or off.")
        config = cls(values.get("VAPI_PHONE_ORG_ID"), values.get("VAPI_PHONE_NUMBER_ID"),
                     values.get("VAPI_PHONE_ASSISTANT_ID"), values.get("VAPI_PHONE_WEBHOOK_SECRET"))
        if config.assistant_id == values.get("VAPI_ASSISTANT_ID"):
            raise ValueError("Telephony requires a dedicated assistant.")
        for name in ("VAPI_WEBHOOK_SECRET", "GEMINI_GATEWAY_SECRET"):
            if config.webhook_secret == values.get(name):
                raise ValueError("Telephony requires a separate webhook credential.")
        return config


@dataclass(frozen=True)
class IngressLimits:
    deadline_seconds: float = 1.0
    busy_seconds: float = 0.1
    workers: int = 4
    active_calls: int = 4
    admissions_per_window: int = 20
    window_seconds: float = 60.0
    bindings_total: int = 1000
    session_ttl_seconds: float = 4 * 3600

    def __post_init__(self):
        for name in ("deadline_seconds", "busy_seconds", "window_seconds", "session_ttl_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid telephony time limit.")
        for name in ("workers", "active_calls", "admissions_per_window", "bindings_total"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError("Invalid telephony capacity limit.")
        if self.session_ttl_seconds > 4 * 3600 or self.deadline_seconds > 5 or self.busy_seconds > self.deadline_seconds:
            raise ValueError("Telephony limits exceed the supported request/session budget.")


class TelephonyIngress:
    def __init__(self, store, config: IngressConfig | None, *, limits=None, clock=time.time, monotonic=time.monotonic, live_admission=None):
        self.path = str(store.path)
        self.config = config
        self.limits = limits or IngressLimits()
        self.clock, self.monotonic = clock, monotonic
        self.live_admission=live_admission
        self._workers = threading.BoundedSemaphore(self.limits.workers)
        # Short admission/end writes queue inside the existing deadline instead
        # of ten connections competing through SQLite's 100 ms busy timeout.
        # Scheduler effects intentionally keep their separate transaction path.
        self._writer=live_admission.writer_lock if live_admission is not None else threading.Lock()
        if config is not None:
            if not isinstance(config, IngressConfig):
                raise ValueError("Invalid telephony configuration.")
            self._initialize()

    def _initialize(self):
        # Startup-only additive schema; Store/session seeding is never called here.
        with self._transaction(None) as (db, deadline):
            db.execute("""CREATE TABLE IF NOT EXISTS telephony_bindings (
                provider_call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                tenant TEXT NOT NULL, organization_id TEXT NOT NULL,
                phone_number_id TEXT NOT NULL, assistant_id TEXT NOT NULL,
                created REAL NOT NULL, ended REAL)""")
            db.execute("CREATE INDEX IF NOT EXISTS telephony_created ON telephony_bindings(tenant,created)")
            db.execute("""CREATE TABLE IF NOT EXISTS provider_tool_policies (
                provider_call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                assistant_id TEXT NOT NULL, policy TEXT NOT NULL)""")

    def _enabled(self):
        if self.config is None:
            raise IngressError("telephony_disabled", 503)

    def authenticate(self, authorization):
        self._enabled()
        if not isinstance(authorization, str) or not secrets.compare_digest(
                authorization.encode("utf-8"), ("Bearer " + self.config.webhook_secret).encode("utf-8")):
            raise IngressError("telephony_unauthorized", 401)

    def _check_deadline(self, deadline, abandoned=None):
        # An asyncio timer can fire before this absolute deadline on a coarse
        # platform clock. A waiter that already timed out/cancelled also closes
        # admission explicitly; a queued worker must not regain a small window.
        if (abandoned is not None and abandoned.is_set()) or self.monotonic() >= deadline:
            raise IngressError("telephony_deadline", 504)

    @contextmanager
    def _transaction(self, deadline, *, write=True, abandoned=None):
        self._enabled()
        start = self.monotonic()
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise IngressError("invalid_deadline", 422)
        deadline = min(start + self.limits.deadline_seconds, deadline) if deadline is not None else start + self.limits.deadline_seconds
        self._check_deadline(deadline, abandoned)
        if not self._workers.acquire(blocking=False):
            raise IngressError("telephony_worker_capacity", 429)
        db,writer_owned = None,False
        try:
            if write:
                writer_owned=self._writer.acquire(timeout=max(0,deadline-self.monotonic()))
                if not writer_owned:raise IngressError("telephony_deadline",504)
            remaining = deadline - self.monotonic()
            self._check_deadline(deadline, abandoned)
            uri = Path(self.path).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
            db = sqlite3.connect(uri, uri=True, timeout=min(self.limits.busy_seconds, remaining), isolation_level=None)
            db.row_factory = sqlite3.Row
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: int((abandoned is not None and abandoned.is_set()) or self.monotonic() >= deadline), 100)
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._check_deadline(deadline, abandoned)
            yield db, deadline
            self._check_deadline(deadline, abandoned)
            db.commit()
        except sqlite3.IntegrityError:
            if db is not None:
                db.rollback()
            raise IngressError("telephony_binding_conflict", 409) from None
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            code = getattr(exc, "sqlite_errorcode", 0) & 255
            if code == sqlite3.SQLITE_INTERRUPT or (abandoned is not None and abandoned.is_set()) or self.monotonic() >= deadline:
                raise IngressError("telephony_deadline", 504) from None
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise IngressError("telephony_busy", 503) from None
            raise IngressError("telephony_storage_unavailable", 503) from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()
            if writer_owned:self._writer.release()
            self._workers.release()

    def _identity(self, call, assistant=None, *, assistant_required):
        self._enabled()
        if not isinstance(call, dict) or not _uuid(call.get("id")):
            raise IngressError("invalid_telephony_call", 422)
        if call.get("type") != "inboundPhoneCall" or call.get("orgId") != self.config.organization_id or call.get("phoneNumberId") != self.config.phone_number_id:
            raise IngressError("telephony_identity_mismatch", 403)
        supplied = []
        if call.get("assistantId") is not None:
            supplied.append(call["assistantId"])
        if assistant is not None:
            if not isinstance(assistant, dict):
                raise IngressError("invalid_telephony_assistant", 422)
            # assistant-request may carry CreateAssistantDTO before selection.
            # Its lack of id is not a conflicting identity; its contents are
            # never returned or used as configuration for the saved assistant.
            if assistant.get("id") is not None:
                supplied.append(assistant["id"])
        if any(value != self.config.assistant_id for value in supplied) or (assistant_required and not supplied):
            raise IngressError("telephony_assistant_mismatch", 403)
        return call["id"]

    def _binding(self, db, call_id):
        binding = db.execute("SELECT * FROM telephony_bindings WHERE provider_call_id=?", (call_id,)).fetchone()
        if not binding:
            raise IngressError("telephony_not_admitted", 403)
        if (binding["tenant"], binding["organization_id"], binding["phone_number_id"], binding["assistant_id"]) != (
                TENANT, self.config.organization_id, self.config.phone_number_id, self.config.assistant_id):
            raise IngressError("telephony_binding_conflict", 409)
        session = db.execute("SELECT * FROM sessions WHERE id=? AND tenant=?", (binding["session_id"], TENANT)).fetchone()
        policy = db.execute("SELECT * FROM provider_tool_policies WHERE provider_call_id=?", (call_id,)).fetchone()
        if not session or session["provider_call_id"] != call_id or session["mode"] != "live" or not policy or (
                policy["session_id"], policy["assistant_id"], policy["policy"]) != (session["id"], self.config.assistant_id, "gemini-issued"):
            raise IngressError("telephony_binding_conflict", 409)
        return binding, session

    def _live(self, binding, session, *, allow_ended=False):
        if self.clock() - session["created"] >= self.limits.session_ttl_seconds:
            raise IngressError("telephony_call_expired", 409)
        if not allow_ended and (binding["ended"] is not None or session["ended"]):
            raise IngressError("telephony_call_ended", 409)

    def _response(self, sid):
        return {"assistantId": self.config.assistant_id,
                "assistantOverrides": {"variableValues": {"session_id": sid}}}

    def admit(self, message, authorization, *, deadline=None, abandoned=None):
        self.authenticate(authorization)
        if not isinstance(message, dict) or message.get("type") != "assistant-request":
            raise IngressError("invalid_admission_event", 422)
        call = message.get("call")
        call_id = self._identity(call, message.get("assistant"), assistant_required=False)
        if call.get("status") == "ended" or call.get("endedAt") is not None:
            raise IngressError("telephony_call_ended", 409)
        # Exact retries are reads. Reuse the original budget across both the
        # read fast path and a possible new atomic admission transaction.
        if deadline is None:deadline=self.monotonic()+self.limits.deadline_seconds
        with self._transaction(deadline,write=False,abandoned=abandoned) as (db,stop):
            deadline=stop
            if db.execute("SELECT 1 FROM telephony_bindings WHERE provider_call_id=?",(call_id,)).fetchone():
                binding,session=self._binding(db,call_id)
                self._live(binding,session)
                return self._response(session['id'])
        with self._transaction(deadline, abandoned=abandoned) as (db, stop):
            existing = db.execute("SELECT 1 FROM telephony_bindings WHERE provider_call_id=?", (call_id,)).fetchone()
            if existing:
                binding, session = self._binding(db, call_id)
                self._live(binding, session)
                return self._response(session["id"])
            if db.execute("SELECT 1 FROM sessions WHERE provider_call_id=?", (call_id,)).fetchone() or db.execute("SELECT 1 FROM provider_tool_policies WHERE provider_call_id=?", (call_id,)).fetchone():
                raise IngressError("telephony_binding_conflict", 409)
            if self.live_admission is not None:
                try:self.live_admission.require_room(db)
                except CapacityError as exc:raise IngressError(exc.code,exc.status) from None
            now = self.clock()
            if db.execute("SELECT COUNT(*) FROM telephony_bindings").fetchone()[0] >= self.limits.bindings_total:
                raise IngressError("telephony_retention_capacity", 429)
            active = db.execute("""SELECT COUNT(*) FROM telephony_bindings b JOIN sessions s ON s.id=b.session_id
                WHERE b.tenant=? AND b.ended IS NULL AND s.ended=0 AND s.created>?""", (TENANT, now-self.limits.session_ttl_seconds)).fetchone()[0]
            if active >= self.limits.active_calls:
                raise IngressError("telephony_active_capacity", 429)
            recent = db.execute("SELECT COUNT(*) FROM telephony_bindings WHERE tenant=? AND created>?", (TENANT, now-self.limits.window_seconds)).fetchone()[0]
            if recent >= self.limits.admissions_per_window:
                raise IngressError("telephony_admission_rate", 429)
            sid = uid("sess")
            # A deliberately undisclosed random browser capability. Only its hash
            # survives; session_id alone never authorizes console/session routes.
            token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            state = {"stage": "service", "slots": [], "customer": None, "service": None, "action": "book"}
            db.execute("INSERT INTO sessions(id,token_hash,tenant,mode,created,ended,state,provider_call_id) VALUES (?,?,?,?,?,0,?,?)",
                       (sid, token_hash, TENANT, "live", now, json.dumps(state), call_id))
            self._insert_binding(db, call_id, sid, now)
            db.execute("INSERT INTO provider_tool_policies VALUES (?,?,?,?)", (call_id, sid, self.config.assistant_id, "gemini-issued"))
            self._check_deadline(stop, abandoned)
            return self._response(sid)

    def _insert_binding(self, db, call_id, sid, now):
        db.execute("INSERT INTO telephony_bindings VALUES (?,?,?,?,?,?,?,NULL)",
                   (call_id, sid, TENANT, self.config.organization_id, self.config.phone_number_id, self.config.assistant_id, now))

    def resolve(self, call, assistant=None, *, allow_ended=False, deadline=None, abandoned=None):
        """Use only after gateway/webhook authentication; never admits a call."""
        call_id = self._identity(call, assistant, assistant_required=True)
        if not allow_ended and (call.get("status") == "ended" or call.get("endedAt") is not None):
            raise IngressError("telephony_call_ended", 409)
        with self._transaction(deadline, write=False, abandoned=abandoned) as (db, _):
            binding, session = self._binding(db, call_id)
            overrides = call.get("assistantOverrides")
            if not isinstance(overrides, dict) or overrides.get("variableValues") != {"session_id": session["id"]}:
                raise IngressError("telephony_metadata_mismatch", 403)
            self._live(binding, session, allow_ended=allow_ended)
            return session["id"]

    def end(self, message, authorization, *, deadline=None, abandoned=None):
        self.authenticate(authorization)
        if not isinstance(message, dict) or not (message.get("type") == "end-of-call-report" or (message.get("type") == "status-update" and message.get("status") == "ended")):
            raise IngressError("invalid_terminal_event", 422)
        call_id = self._identity(message.get("call"), message.get("assistant"), assistant_required=False)
        with self._transaction(deadline, abandoned=abandoned) as (db, _):
            binding, session = self._binding(db, call_id)
            # Repeated terminal reports are harmless and do not renew admission.
            if binding["ended"] is None:
                db.execute("UPDATE telephony_bindings SET ended=? WHERE provider_call_id=?", (self.clock(), call_id))
            db.execute("UPDATE sessions SET ended=1 WHERE id=? AND tenant=?", (session["id"], TENANT))
        return {"received": True}

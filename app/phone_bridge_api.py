"""Opt-in private phone control: one canonical AI/booking session in this Store.

The outer carrier leg is control metadata only. These routes never enter the
public app, perform booking actions, infer identity from caller ID, or allocate
a provider call. The trusted media owner marks one allocation before its POST.
Unknown outcomes retain their shared capacity reservation until exact readback.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time

import httpx
from fastapi import Header, Request
from starlette.concurrency import run_in_threadpool

from .capacity import CapacityError
from .domain import TENANT, uid
from .http_boundary import read_webhook_json
from .telephony_ingress import IngressError, _uuid


class PhoneBridgeAdmissions:
    """Uses TelephonyIngress's existing worker, writer, auth and rate limits."""

    def __init__(self, ingress, inner_assistant_id, secret):
        if ingress.config is None or not _uuid(inner_assistant_id):
            raise ValueError("Phone bridge requires configured phone and inner assistant identities.")
        if (not isinstance(secret, str) or not 32 <= len(secret) <= 512
                or any(ord(c) < 33 or ord(c) > 126 for c in secret)
                or secret == ingress.config.webhook_secret):
            raise ValueError("Phone bridge requires a separate private control credential.")
        self.ingress, self.inner_assistant_id, self.secret = ingress, inner_assistant_id, secret
        with ingress._transaction(None) as (db, _):
            db.execute("""CREATE TABLE IF NOT EXISTS phone_bridge_admissions (
                outer_call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                tenant TEXT NOT NULL, organization_id TEXT NOT NULL, phone_number_id TEXT NOT NULL,
                inner_assistant_id TEXT NOT NULL, created REAL NOT NULL, expires_at REAL NOT NULL,
                allocation_requested REAL, inner_call_id TEXT UNIQUE,
                outer_ended INTEGER NOT NULL DEFAULT 0, inner_ended INTEGER NOT NULL DEFAULT 0,
                closed REAL)""")
            db.execute("CREATE INDEX IF NOT EXISTS phone_bridge_created ON phone_bridge_admissions(tenant,created)")

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not secrets.compare_digest(authorization.encode("utf-8"), ("Bearer " + self.secret).encode("utf-8")):
            raise IngressError("phone_bridge_unauthorized", 401)

    def token(self, outer, sid):
        return hmac.new(self.secret.encode(), ("phone-bridge-session-v1:" + outer + ":" + sid).encode(), hashlib.sha256).hexdigest()

    def _row(self, db, outer):
        row = db.execute("SELECT * FROM phone_bridge_admissions WHERE outer_call_id=?", (outer,)).fetchone()
        cfg = self.ingress.config
        if not row:
            raise IngressError("phone_bridge_not_admitted", 403)
        if (row["tenant"], row["organization_id"], row["phone_number_id"], row["inner_assistant_id"]) != (
                TENANT, cfg.organization_id, cfg.phone_number_id, self.inner_assistant_id):
            raise IngressError("phone_bridge_binding_conflict", 409)
        return row

    @staticmethod
    def state(row):
        return "closed" if row["closed"] is not None else "bound" if row["inner_call_id"] else "requested" if row["allocation_requested"] is not None else "reserved"

    def admit(self, call, expires_at, *, deadline=None, abandoned=None):
        outer = self.ingress._identity(call, assistant_required=False)
        now = self.ingress.clock()
        if (call.get("status") == "ended" or call.get("endedAt") is not None
                or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at)
                or not now < expires_at <= now + 300):
            raise IngressError("phone_bridge_call_expired", 409)
        with self.ingress._transaction(deadline, abandoned=abandoned) as (db, stop):
            existing = db.execute("SELECT 1 FROM phone_bridge_admissions WHERE outer_call_id=?", (outer,)).fetchone()
            if existing:
                row = self._row(db, outer)
                session = db.execute("SELECT * FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
                if row["closed"] is not None or row["outer_ended"] or row["inner_ended"] or row["expires_at"] <= now or not session or session["ended"]:
                    raise IngressError("phone_bridge_call_ended", 409)
                sid = row["session_id"]
                if session["token_hash"] != hashlib.sha256(self.token(outer, sid).encode()).hexdigest():
                    raise IngressError("phone_bridge_capability_changed", 409)
            else:
                if db.execute("SELECT 1 FROM telephony_bindings WHERE provider_call_id=?", (outer,)).fetchone() or db.execute(
                        "SELECT 1 FROM sessions WHERE provider_call_id=?", (outer,)).fetchone():
                    raise IngressError("phone_bridge_binding_conflict", 409)
                self.ingress.require_phone_budget(db, now)
                if self.ingress.live_admission is None:
                    raise IngressError("phone_bridge_shared_capacity_required", 503)
                try:
                    self.ingress.live_admission.require_room(db)
                except CapacityError as exc:
                    raise IngressError(exc.code, exc.status) from None
                sid = uid("sess")
                state = {"stage": "service", "slots": [], "customer": None, "service": None, "action": "book"}
                token_hash = hashlib.sha256(self.token(outer, sid).encode()).hexdigest()
                db.execute("INSERT INTO sessions(id,token_hash,tenant,mode,created,state) VALUES (?,?,?,?,?,?)",
                           (sid, token_hash, TENANT, "live", now, json.dumps(state)))
                cfg = self.ingress.config
                db.execute("""INSERT INTO phone_bridge_admissions
                    (outer_call_id,session_id,tenant,organization_id,phone_number_id,inner_assistant_id,created,expires_at)
                    VALUES (?,?,?,?,?,?,?,?)""", (outer, sid, TENANT, cfg.organization_id, cfg.phone_number_id, self.inner_assistant_id, now, expires_at))
                row = self._row(db, outer)
            self.ingress._check_deadline(stop, abandoned)
            return {"id": sid, "token": self.token(outer, sid), "mode": "live", "outer_call_id": outer,
                    "allocation_state": self.state(row), "max_duration_seconds": 300,
                    "remaining_duration_ms": max(0, int((row["expires_at"] - now) * 1000))}

    def lookup(self, outer):
        with self.ingress._transaction(None, write=False) as (db, _):
            return dict(self._row(db, outer))

    def allocation(self, outer, *, deadline=None, abandoned=None):
        with self.ingress._transaction(deadline, abandoned=abandoned) as (db, stop):
            row = self._row(db, outer)
            session = db.execute("SELECT ended FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
            if row["closed"] is not None or row["outer_ended"] or row["expires_at"] <= self.ingress.clock() or not session or session["ended"]:
                raise IngressError("phone_bridge_call_ended", 409)
            if row["allocation_requested"] is not None:
                raise IngressError("phone_bridge_allocation_already_requested", 409)
            db.execute("UPDATE phone_bridge_admissions SET allocation_requested=? WHERE outer_call_id=?", (self.ingress.clock(), outer))
            self.ingress._check_deadline(stop, abandoned)
        return {"allocation_reserved": True}

    def bind(self, outer, inner, *, deadline=None, abandoned=None):
        if not _uuid(inner) or inner == outer:
            raise IngressError("phone_bridge_inner_identity", 422)
        try:
            with self.ingress._transaction(deadline, abandoned=abandoned) as (db, stop):
                row = self._row(db, outer)
                if row["closed"] is not None or row["allocation_requested"] is None:
                    raise IngressError("phone_bridge_allocation_not_owned", 409)
                if row["inner_call_id"] not in (None, inner):
                    raise IngressError("phone_bridge_inner_conflict", 409)
                session = db.execute("SELECT provider_call_id FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
                if not session or session["provider_call_id"] not in (None, inner):
                    raise IngressError("phone_bridge_inner_conflict", 409)
                db.execute("UPDATE phone_bridge_admissions SET inner_call_id=? WHERE outer_call_id=?", (inner, outer))
                self.ingress._check_deadline(stop, abandoned)
        except sqlite3.IntegrityError:
            raise IngressError("phone_bridge_inner_conflict", 409) from None
        return {"bound": True}

    def close(self, outer, *, outer_ended, inner_ended, deadline=None, abandoned=None):
        with self.ingress._transaction(deadline, abandoned=abandoned) as (db, stop):
            row = self._row(db, outer)
            outer_ended = row["outer_ended"] or outer_ended is True
            inner_ended = row["inner_ended"] or (inner_ended is True and row["inner_call_id"] is not None)
            no_allocation = row["allocation_requested"] is None
            closed = bool(outer_ended and (no_allocation or inner_ended))
            db.execute("""UPDATE phone_bridge_admissions SET outer_ended=?,inner_ended=?,closed=COALESCE(closed,?)
                WHERE outer_call_id=?""", (int(bool(outer_ended)), int(bool(inner_ended)), self.ingress.clock() if closed else None, outer))
            if outer_ended or inner_ended:
                db.execute("UPDATE sessions SET ended=1 WHERE id=? AND tenant=?", (row["session_id"], TENANT))
            self.ingress._check_deadline(stop, abandoned)
        return {"closed": closed, "outer_ended": bool(outer_ended), "inner_ended": bool(inner_ended)}

    def require_live(self, sid, call_id=None):
        with self.ingress._transaction(None, write=False) as (db, _):
            row = db.execute("SELECT * FROM phone_bridge_admissions WHERE session_id=?", (sid,)).fetchone()
            if row is None:
                return
            if row["closed"] is not None or row["outer_ended"] or row["inner_ended"] or row["expires_at"] <= self.ingress.clock():
                raise IngressError("phone_bridge_call_ended", 409)
            if call_id is not None and row["inner_call_id"] != call_id:
                raise IngressError("phone_bridge_inner_conflict", 403)

    def validate_bound(self, sid, call_id):
        """Identity only: terminal metadata and accepted-work replay stay usable."""
        with self.ingress._transaction(None, write=False) as (db, _):
            row = db.execute("SELECT inner_call_id FROM phone_bridge_admissions WHERE session_id=?", (sid,)).fetchone()
            if row is not None and row["inner_call_id"] != call_id:
                raise IngressError("phone_bridge_inner_conflict", 403)

    def observe_authenticated_inner(self, sid, call_id, call_type):
        """Handle authenticated provider events preceding POST /call's reply.

        Called only after ordinary gateway/webhook auth, exact assistant match
        and private session capability validation. It cannot admit an outer
        call or reserve another allocation. Explicit provider GET/bind later
        reconciles this same immutable ID before the media owner uses the URL.
        """
        with self.ingress._transaction(None) as (db, _):
            row = db.execute("SELECT * FROM phone_bridge_admissions WHERE session_id=?", (sid,)).fetchone()
            if row is None:
                return
            if (row["closed"] is not None or row["outer_ended"] or row["inner_ended"]
                    or row["expires_at"] <= self.ingress.clock()):
                raise IngressError("phone_bridge_call_ended", 409)
            if call_type != "webCall" or not _uuid(call_id) or call_id == row["outer_call_id"] or row["allocation_requested"] is None:
                raise IngressError("phone_bridge_inner_conflict", 403)
            if row["inner_call_id"] not in (None, call_id):
                raise IngressError("phone_bridge_inner_conflict", 403)
            try:
                db.execute("UPDATE phone_bridge_admissions SET inner_call_id=? WHERE session_id=?", (call_id, sid))
            except sqlite3.IntegrityError:
                raise IngressError("phone_bridge_inner_conflict", 403) from None


def install_phone_bridge_api(app, ingress, settings, *, client=None):
    """No routes/schema or provider clients unless explicitly enabled at startup."""
    values = settings()
    flag = values.get("SERVICEVOICE_PHONE_BRIDGE_ADMISSION", "off")
    if flag in (None, "", "off"):
        return None
    if flag != "on":
        raise ValueError("Phone bridge admission must be on or off.")
    control = PhoneBridgeAdmissions(ingress, values.get("VAPI_ASSISTANT_ID"), values.get("SERVICEVOICE_PHONE_BRIDGE_CONTROL_SECRET"))
    if not values.get("VAPI_API_KEY"):
        raise ValueError("Phone bridge admission requires the configured provider credential.")

    async def provider(call_id):
        if not _uuid(call_id):
            raise IngressError("phone_bridge_invalid_call_id", 422)
        async def read(active):
            response = await active.get("https://api.vapi.ai/call/" + call_id,
                headers={"Authorization": "Bearer " + values["VAPI_API_KEY"]})
            if response.status_code != 200 or len(response.content) > 1024 * 1024:
                raise IngressError("phone_bridge_provider_read_unavailable", 503)
            result = response.json()
            if not isinstance(result, dict) or result.get("id") != call_id:
                raise IngressError("phone_bridge_provider_identity", 403)
            return result
        try:
            if client is not None:
                return await read(client)
            async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as active:
                return await read(active)
        except (httpx.HTTPError, ValueError, TypeError):
            raise IngressError("phone_bridge_provider_read_unavailable", 503) from None

    def inner_identity(row, link):
        if (row.get("type") != "webCall" or row.get("orgId") != ingress.config.organization_id
                or row.get("assistantId") != control.inner_assistant_id):
            raise IngressError("phone_bridge_provider_identity", 403)
        overrides = row.get("assistantOverrides")
        variables = overrides.get("variableValues") if isinstance(overrides, dict) else None
        if (not isinstance(variables, dict) or variables.get("session_id") != link["session_id"]
                or variables.get("servicevoice_duplex_playback") != "browser-owned-v1"):
            raise IngressError("phone_bridge_provider_binding", 403)
        if "session_token" in variables and (not isinstance(variables["session_token"], str)
                or not secrets.compare_digest(variables["session_token"].encode("utf-8"), control.token(link["outer_call_id"], link["session_id"]).encode("utf-8"))):
            raise IngressError("phone_bridge_provider_binding", 403)

    async def effect(request, authorization, action, outer=None):
        control.authenticate(authorization)
        if request.url.hostname not in ("localhost", "127.0.0.1", "::1", "testserver"):
            raise IngressError("phone_bridge_private_only", 403)
        # Share the existing phone HTTP capacity and accepted-task lifecycle.
        marker = object()
        with app.state.telephony_http_lock:
            if app.state.telephony_http_closing or len(app.state.telephony_http_pending) >= app.state.telephony_http_capacity:
                raise IngressError("telephony_http_capacity", 503)
            app.state.telephony_http_pending.add(marker)
        deadline = time.monotonic() + 6
        abandoned = threading.Event()
        task = None
        body = None
        def release(completed=None):
            with app.state.telephony_http_lock:
                app.state.telephony_http_pending.discard(marker)
            if completed is not None:
                app.state.telephony_http_tasks.discard(completed)
                if not completed.cancelled():
                    completed.exception()
        async def run():
            if not isinstance(body, dict):
                raise IngressError("phone_bridge_invalid_body", 422)
            target = body.get("outer_call_id") if action == "admit" else outer
            if not _uuid(target):
                raise IngressError("phone_bridge_invalid_call_id", 422)
            expected = {"outer_call_id"} if action == "admit" else {"inner_call_id"} if action == "bind" else set()
            if set(body) != expected:
                raise IngressError("phone_bridge_invalid_body", 422)
            kwargs = {"deadline": deadline, "abandoned": abandoned}
            if action == "admit":
                row = await provider(target)
                ingress._identity(row, assistant_required=False)
                try:
                    created = datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        raise ValueError()
                    expires_at = min(ingress.clock() + 300, created.timestamp() + 300)
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    raise IngressError("phone_bridge_provider_time_unavailable", 503) from None
                return await run_in_threadpool(control.admit, row, expires_at, **kwargs)
            link = await run_in_threadpool(control.lookup, target)
            if action == "allocation":
                row = await provider(target)
                ingress._identity(row, assistant_required=False)
                if row.get("status") == "ended" or row.get("endedAt") is not None:
                    raise IngressError("phone_bridge_call_ended", 409)
                return await run_in_threadpool(control.allocation, target, **kwargs)
            if action == "bind":
                inner = body["inner_call_id"]
                row = await provider(inner)
                inner_identity(row, link)
                return await run_in_threadpool(control.bind, target, inner, **kwargs)
            outer_row = await provider(target)
            ingress._identity(outer_row, assistant_required=False)
            outer_ended = outer_row.get("status") == "ended"
            inner_ended = False
            if outer_ended:
                # Do not lose authenticated terminal proof merely because the
                # second leg's read later fails. Capacity remains reserved.
                await run_in_threadpool(control.close, target, outer_ended=True, inner_ended=False, **kwargs)
            if link["inner_call_id"]:
                inner_row = await provider(link["inner_call_id"])
                inner_identity(inner_row, link)
                inner_ended = inner_row.get("status") == "ended"
            return await run_in_threadpool(control.close, target, outer_ended=outer_ended, inner_ended=inner_ended, **kwargs)
        try:
            body = await asyncio.wait_for(read_webhook_json(request), timeout=max(0, deadline-time.monotonic()))
            task = asyncio.create_task(run())
            app.state.telephony_http_tasks.add(task)
            task.add_done_callback(release)
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(0, deadline-time.monotonic()))
        except asyncio.TimeoutError:
            abandoned.set()
            raise IngressError("phone_bridge_deadline", 504) from None
        except asyncio.CancelledError:
            abandoned.set()
            raise
        finally:
            if task is None:
                release()

    @app.post("/api/phone-bridge/admit")
    async def admit(request: Request, authorization: str | None = Header(default=None)):
        return await effect(request, authorization, "admit")

    @app.post("/api/phone-bridge/{outer}/allocation")
    async def allocation(outer: str, request: Request, authorization: str | None = Header(default=None)):
        return await effect(request, authorization, "allocation", outer)

    @app.post("/api/phone-bridge/{outer}/bind")
    async def bind(outer: str, request: Request, authorization: str | None = Header(default=None)):
        return await effect(request, authorization, "bind", outer)

    @app.post("/api/phone-bridge/{outer}/close")
    async def close(outer: str, request: Request, authorization: str | None = Header(default=None)):
        return await effect(request, authorization, "close", outer)

    @app.get("/api/phone-bridge/{outer}")
    async def lookup(outer: str, request: Request, authorization: str | None = Header(default=None)):
        control.authenticate(authorization)
        if request.url.hostname not in ("localhost", "127.0.0.1", "::1", "testserver") or not _uuid(outer):
            raise IngressError("phone_bridge_private_only", 403)
        row = await run_in_threadpool(control.lookup, outer)
        return {"closed": row["closed"] is not None, "allocation_state": control.state(row),
                "inner_call_id": row["inner_call_id"], "outer_ended": bool(row["outer_ended"]), "inner_ended": bool(row["inner_ended"])}

    app.state.phone_bridge = control
    return control

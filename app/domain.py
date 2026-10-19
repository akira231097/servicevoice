"""Transactional, single-tenant demo scheduler; no model controls capacity."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
TENANT = "cedar-demo"
SERVICES = {
    "oil_change": {"id": "oil_change", "name": "Oil & filter service", "duration_minutes": 60},
    "tires": {"id": "tires", "name": "Tire inspection & rotation", "duration_minutes": 60},
    "brakes": {"id": "brakes", "name": "Brake inspection", "duration_minutes": 60},
    "diagnostic": {"id": "diagnostic", "name": "Diagnostic appointment", "duration_minutes": 60},
}

def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:16]

def now():
    return datetime.now(timezone.utc)

def today():
    return now().astimezone(TZ).date()

def dealership_facts(local_date):
    """Fresh public demo facts only; no session, capacity, DB or caller input."""
    return {
        "name":"Cedar Service (fictional)", "timezone":TZ.key, "today":local_date.isoformat(),
        "hours":"Monday–Friday, 8 AM–5 PM Central; closed Saturday and Sunday.",
        "services":[dict(service) for service in SERVICES.values()],
        "physical_address":None,
        "location_note":"This is a fictional dealership demo; no verified physical service address is configured.",
        "handoff_capabilities":{"local_request_queue":True,"live_transfer":False,"callback_delivery":False},
        "policies":"All slots are one-hour intake blocks in this demo, not a repair completion promise. Prices, loaners, warranty, recall eligibility and parts need advisor verification. No VIN or recall database is connected.",
        "booking_scope":"Only appointments created in this conversation can be viewed or changed. For existing real appointments, request an advisor.",
    }

def label(start):
    value = datetime.fromisoformat(start).astimezone(TZ)
    return value.strftime("%A, %B %d at %I:%M %p %Z")

def phone(value):
    digits = "".join(c for c in str(value) if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise DomainError("invalid_phone", "Use a ten-digit US demo callback number.")
    return "+" + digits

class DomainError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, tenant TEXT NOT NULL,
                  mode TEXT NOT NULL, created REAL NOT NULL, ended INTEGER DEFAULT 0,
                  state TEXT NOT NULL, provider_call_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS slots (
                  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, start TEXT NOT NULL,
                  capacity INTEGER NOT NULL CHECK(capacity > 0), UNIQUE(tenant,start));
                CREATE TABLE IF NOT EXISTS proposals (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tenant TEXT NOT NULL,
                  kind TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL,
                  state TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS appointments (
                  id TEXT PRIMARY KEY, reference TEXT UNIQUE NOT NULL,
                  session_id TEXT NOT NULL, tenant TEXT NOT NULL, service TEXT NOT NULL,
                  slot_id TEXT NOT NULL, customer TEXT NOT NULL, status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS active_capacity ON appointments(tenant,slot_id,status);
                CREATE TABLE IF NOT EXISTS tool_results (
                  session_id TEXT NOT NULL, call_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                  result TEXT NOT NULL, PRIMARY KEY(session_id,call_id));
                CREATE TABLE IF NOT EXISTS tool_invocations (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, call_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL, detail TEXT,
                  UNIQUE(session_id,call_id));
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                  name TEXT NOT NULL, status TEXT NOT NULL, elapsed_ms REAL NOT NULL,
                  at TEXT NOT NULL, summary TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transcripts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                  event_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
                  UNIQUE(session_id,event_id));
                CREATE TABLE IF NOT EXISTS handoffs (
                  id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL, reason TEXT NOT NULL,
                  status TEXT NOT NULL, created TEXT NOT NULL);
            ''')
        self.seed_slots()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def read_connection(self, timeout_seconds=0.2):
        """A short-wait, read-only connection for outcome/status inspection.

        The mutation path retains its original transaction and busy policy.
        A missing database cannot be silently recreated by a status request.
        """
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=timeout_seconds, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def seed_slots(self):
        with self.transaction() as db:
            for offset in range(22):
                day = today() + timedelta(days=offset)
                if day.weekday() >= 5:
                    continue
                for hour in (9, 10, 11, 13, 14, 15):
                    start = datetime.combine(day, dtime(hour), TZ).isoformat()
                    db.execute("INSERT OR IGNORE INTO slots VALUES (?,?,?,?)",
                               (f"slot_{day.isoformat()}_{hour}", TENANT, start, 1))

    def create_session(self, mode="rehearsal"):
        sid, token = uid("sess"), secrets.token_urlsafe(32)
        state = {"stage": "service", "slots": [], "customer": None, "service": None, "action": "book"}
        with self.connect() as db:
            db.execute("INSERT INTO sessions(id,token_hash,tenant,mode,created,state) VALUES (?,?,?,?,?,?)",
                       (sid, hashlib.sha256(token.encode()).hexdigest(), TENANT, mode, time.time(), json.dumps(state)))
        greeting = "Hi, I’m Cedar’s AI service assistant. This is a fictional dealership demo. What service does your vehicle need?"
        if mode == "rehearsal":
            self.transcript(sid, "assistant", greeting, "greeting")
        return {"id": sid, "token": token, "mode": mode, "stage": "service", "greeting": greeting}

    def authorize(self, sid, token, allow_ended=False):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND tenant=?", (sid, TENANT)).fetchone()
        if not row or not secrets.compare_digest(row["token_hash"], hashlib.sha256((token or "").encode()).hexdigest()):
            raise DomainError("unauthorized", "Session authorization failed.")
        if time.time() - row["created"] > 4 * 3600:
            raise DomainError("session_expired", "Session expired. Start a new session.")
        if row["ended"] and not allow_ended:
            raise DomainError("session_ended", "This session has ended.")
        return dict(row)

    def state(self, sid, db=None):
        if db is None:
            with self.connect() as conn:
                return self.state(sid, conn)
        row = db.execute("SELECT state FROM sessions WHERE id=? AND tenant=?", (sid,TENANT)).fetchone()
        if not row:
            raise DomainError("not_found", "Session not found.")
        return json.loads(row[0])

    def update(self, sid, values, db=None):
        if db is None:
            with self.transaction() as conn:
                return self.update(sid, values, conn)
        state = self.state(sid, db)
        state.update(values)
        db.execute("UPDATE sessions SET state=? WHERE id=? AND tenant=?", (json.dumps(state),sid,TENANT))
        return state

    def transcript(self, sid, role, text, event_id=None):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO transcripts(session_id,event_id,role,text) VALUES (?,?,?,?)",
                       (sid,event_id or uid("turn"),role,text[:6000]))

    def availability(self, db, service, day=None):
        """Compatibility list for existing internal proposal callers."""
        return self.availability_result(db, service, day)["slots"]

    def availability_result(self, db, service, day=None, time_filter=None):
        if service not in SERVICES:
            raise DomainError("unsupported_service", "Choose oil_change, tires, brakes, or diagnostic; otherwise request an advisor.")
        queried_at = now()
        local_today = queried_at.astimezone(TZ).date()
        horizon_end = local_today + timedelta(days=21)
        if day:
            try:
                requested = date.fromisoformat(day)
            except (TypeError,ValueError):
                raise DomainError("invalid_date", "Use an absolute date as YYYY-MM-DD in America/Chicago.")
            if requested < local_today or requested > horizon_end:
                raise DomainError("out_of_range", "Choose today through the next 21 days.")
        rows = db.execute('''SELECT s.*, s.capacity - COUNT(a.id) AS remaining FROM slots s
             LEFT JOIN appointments a ON a.slot_id=s.id AND a.tenant=s.tenant AND a.status='confirmed'
             WHERE s.tenant=? GROUP BY s.id ORDER BY s.start''',(TENANT,)).fetchall()
        result=[]
        for r in rows:
            start=datetime.fromisoformat(r["start"]).astimezone(TZ)
            if start <= queried_at or start.date()>horizon_end or (day and start.date().isoformat()!=day) or r["remaining"]<=0:
                continue
            if time_filter is not None:
                clock=start.strftime("%H:%M")
                exact=time_filter.get("exact_time")
                lower=time_filter.get("start_time")
                upper=time_filter.get("end_time")
                if exact is not None and clock!=exact:
                    continue
                if lower is not None and (clock<lower or clock==lower and not time_filter.get("start_inclusive",True)):
                    continue
                if upper is not None and (clock>upper or clock==upper and not time_filter.get("end_inclusive",True)):
                    continue
            result.append({"id":r["id"],"start":r["start"],"label":label(r["start"]),"remaining":r["remaining"]})
        slots=result[:3]
        return {"slots":slots,"query":{"service":service,"date":day,"time_filter":time_filter,
                    "timezone":TZ.key,"horizon_start":local_today.isoformat(),"horizon_end":horizon_end.isoformat()},
                "total_matching":len(result),"returned_count":len(slots),
                "truncated":len(result)>len(slots),"complete":len(result)==len(slots)}

    def handoff(self, db, sid):
        row=db.execute("SELECT id,reason,status FROM handoffs WHERE session_id=?",(sid,)).fetchone()
        if row is None:
            return None
        return {**dict(row),"message":
            "The local advisor request was withdrawn. Its audit record remains; no appointment was changed and no external contact was made."
            if row["status"]=="withdrawn" else "Local queue only; no actual transfer or callback sent."}

    def appointment(self, db, sid):
        r=db.execute('''SELECT a.*,s.start FROM appointments a JOIN slots s ON a.slot_id=s.id
            WHERE a.session_id=? AND a.tenant=? ORDER BY a.rowid DESC LIMIT 1''',(sid,TENANT)).fetchone()
        if not r:
            return None
        customer=json.loads(r["customer"])
        return {"id":r["id"],"reference":r["reference"],"service":r["service"],"service_name":SERVICES[r["service"]]["name"],
                "start":r["start"],"label":label(r["start"]),"status":r["status"],"slot_id":r["slot_id"],
                "customer_name":customer["name"],"vehicle":customer["vehicle"]}

    def invalidate(self, sid):
        with self.transaction() as db:
            db.execute("UPDATE proposals SET state='superseded' WHERE session_id=? AND state='pending'",(sid,))
            self.update(sid,{"proposal_id":None},db)

    def _proposal(self, db, sid, kind, payload, summary, presentation=None):
        db.execute("UPDATE proposals SET state='superseded' WHERE session_id=? AND state='pending'",(sid,))
        pid=uid("proposal"); expires=time.time()+300
        payload["summary"]=summary
        if presentation is not None:payload["presentation"]=presentation
        db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)",(pid,sid,TENANT,kind,json.dumps(payload),expires,"pending",None))
        self.update(sid,{"stage":"confirmation","proposal_id":pid},db)
        return {"ok":True,"proposal_id":pid,"summary":summary,"expires_at":datetime.fromtimestamp(expires,timezone.utc).isoformat(),
                **({"presentation":presentation} if presentation is not None else {}),
                "instruction":"Read this exact summary, ask for explicit confirmation, then call confirm_action. This is not a booking yet."}

    def _require_slot(self, db, slot_id):
        r=db.execute("SELECT * FROM slots WHERE id=? AND tenant=?",(slot_id,TENANT)).fetchone()
        if not r or datetime.fromisoformat(r["start"])<=now():
            raise DomainError("invalid_slot","That slot is not available. Search again.")
        return r

    def _has_capacity(self, db, slot, exclude=None):
        count=db.execute("SELECT COUNT(*) FROM appointments WHERE tenant=? AND slot_id=? AND status='confirmed' AND id != ?",(TENANT,slot["id"],exclude or "")).fetchone()[0]
        if count>=slot["capacity"]:
            raise DomainError("slot_unavailable","Someone took that slot. Nothing was changed; search again for another time.")

    def _booking_proposal(self, db, sid, service, slot, customer, presentation=None):
        """Shared construction after the caller-specific eligibility checks."""
        self.update(sid,{"customer":customer,"selected_slot":slot["id"]},db)
        summary=f"{SERVICES[service]['name']} for {customer['name']}, {customer['vehicle']}, on {label(slot['start'])}. Callback {customer['phone']}."
        result=self._proposal(db,sid,"book",{"slot_id":slot["id"],"service":service,"customer":customer},summary,presentation)
        # Return the authoritative selection so a contact-only correction can
        # prepare a replacement without another availability/model round.
        # Cached historical receipts are replayed unchanged by dispatch.
        return {**result,"slot_id":slot["id"]}

    def dispatch(self, sid, name, args, call_id):
        """One transaction contains effect, idempotency result, and event. Replays cannot repeat a write."""
        start=time.perf_counter()
        fingerprint=hashlib.sha256(json.dumps({"name":name,"args":args},sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            row=db.execute("SELECT * FROM tool_results WHERE session_id=? AND call_id=?",(sid,call_id)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    return {"ok":False,"error":"idempotency_conflict","message":"A retry changed the request. Reconcile the original operation."}
                return json.loads(row["result"])
            try:
                result=self._execute(db,sid,name,args,call_id)
            except DomainError as exc:
                result={"ok":False,"error":exc.code,"message":exc.message}
            db.execute("INSERT INTO tool_results VALUES (?,?,?,?)",(sid,call_id,fingerprint,json.dumps(result)))
            elapsed=round((time.perf_counter()-start)*1000,2)
            db.execute("INSERT INTO events(session_id,name,status,elapsed_ms,at,summary) VALUES (?,?,?,?,?,?)",
                       (sid,name,"success" if result.get("ok") else "error",elapsed,now().isoformat(),
                        result.get("message") or result.get("summary") or result.get("instruction") or "Completed"))
            return result

    def _execute(self, db, sid, name, a, call_id=None):
        state=self.state(sid,db)
        if name=="get_dealership_info":
            return {"ok":True,**dealership_facts(today())}
        if name in ("search_availability","begin_service_request"):
            availability=self.availability_result(db,a["service"],a.get("date"),a.get("time_filter"))
            slots=availability["slots"]
            db.execute("UPDATE proposals SET state='superseded' WHERE session_id=? AND state='pending'",(sid,))
            self.update(sid,{"slots":slots,"service":a["service"],"stage":"slot" if slots else "date","proposal_id":None,"availability_query":availability["query"]},db)
            result={"ok":True,**availability,"timezone":TZ.key,
                    **({"presentation":a["presentation"]} if a.get("presentation") is not None else {}),
                    "message":("Available times are offers, not reservations. "
                        +( "Only the first three matches are shown; an omitted time is not proof of unavailability." if availability["truncated"] else "These are all matches for the stated query within the displayed search horizon."))
                        if slots else "No openings match this exact query within the displayed search horizon. Do not broaden the caller's constraints without asking."}
            if name=="begin_service_request":
                # Fuse two reads without discarding policies to make the response look cheaper.
                result["context"]=dealership_facts(today())
            return result
        if name=="prepare_first_available_booking":
            if a.get("strategy")!="first_available":
                raise DomainError("invalid_strategy","This shortcut requires an explicit unrestricted first-available request.")
            # Normalize every customer field and validate all scheduling facts
            # before changing offers, customer state or the pending proposal.
            customer={"name":a["customer_name"].strip(),"phone":phone(a["phone"]),"vehicle":a["vehicle"].strip()}
            if (state.get("stage")!="service" or state.get("action","book")!="book"
                    or any(state.get(key) for key in ("service","slots","customer","selected_slot","proposal_id"))):
                raise DomainError("first_available_not_fresh","This conversation already has scheduling context. Use the existing availability and preparation flow; do not restart it with the shortcut.")
            for table in ("proposals","appointments","handoffs"):
                if db.execute(f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1",(sid,)).fetchone():
                    raise DomainError("first_available_not_fresh","An earlier appointment, proposal or advisor request exists. Reconcile that context before continuing with the existing tools.")
            if db.execute("""SELECT 1 FROM tool_invocations i WHERE i.session_id=? AND i.call_id!=?
                AND NOT EXISTS (SELECT 1 FROM tool_results r WHERE r.session_id=i.session_id AND r.call_id=i.call_id) LIMIT 1""",(sid,call_id)).fetchone():
                raise DomainError("operation_unresolved","Reconcile the earlier operation before preparing anything new.")
            availability=self.availability_result(db,a["service"])
            slots=availability["slots"]
            if not slots:
                raise DomainError("no_availability","No available intake slot was found. Nothing was prepared or booked; ask about another date or an advisor.")
            slot=self._require_slot(db,slots[0]["id"])
            self._has_capacity(db,slot)
            # dispatch records DomainError without rolling its transaction back.
            # This local savepoint prevents any partial composite state escaping.
            db.execute("SAVEPOINT first_available_proposal")
            try:
                self.update(sid,{"slots":slots,"service":a["service"],"availability_query":availability["query"]},db)
                result=self._booking_proposal(db,sid,a["service"],slot,customer,a.get("presentation"))
            except BaseException:
                db.execute("ROLLBACK TO SAVEPOINT first_available_proposal")
                db.execute("RELEASE SAVEPOINT first_available_proposal")
                raise
            db.execute("RELEASE SAVEPOINT first_available_proposal")
            return result
        if name=="prepare_booking":
            existing=self.appointment(db,sid)
            if existing and existing["status"]=="confirmed":
                raise DomainError("already_booked","This session already has an appointment. Use reschedule or cancellation.")
            slot=self._require_slot(db,a["slot_id"])
            if a["slot_id"] not in {s["id"] for s in state["slots"]}:
                raise DomainError("slot_not_offered","Search availability first and use a returned slot ID.")
            if a["service"]!=state["service"]:
                raise DomainError("service_changed","Search availability again for the changed service.")
            self._has_capacity(db,slot)
            customer={"name":a["customer_name"].strip(),"phone":phone(a["phone"]),"vehicle":a["vehicle"].strip()}
            return self._booking_proposal(db,sid,a["service"],slot,customer,a.get("presentation"))
        if name=="get_appointment":
            return {"ok":True,"appointment":self.appointment(db,sid),"message":"Only this session’s appointment is accessible."}
        if name=="get_session_context":
            proposal=db.execute("SELECT id,kind,payload,expires FROM proposals WHERE id=? AND session_id=? AND state='pending'",(state.get("proposal_id"),sid)).fetchone()
            return {"ok":True,"stage":state["stage"],"service":state.get("service"),"customer":state.get("customer"),
                    "offered_slots":state.get("slots",[]),"appointment":self.appointment(db,sid),
                    "handoff":self.handoff(db,sid),"availability_query":state.get("availability_query"),
                    "pending_proposal":{"id":proposal["id"],"kind":proposal["kind"],"summary":json.loads(proposal["payload"])["summary"],"expires_at":datetime.fromtimestamp(proposal["expires"],timezone.utc).isoformat()} if proposal else None,
                    "instruction":"These are durable facts; they do not prove consent. Do not replay old conversation or infer a booking beyond the saved appointment."}
        if name in ("prepare_reschedule","prepare_cancellation"):
            appt=self.appointment(db,sid)
            if not appt or appt["status"]!="confirmed":
                raise DomainError("no_appointment","No confirmed appointment in this session. An advisor must verify any other appointment.")
            if name=="prepare_cancellation":
                return self._proposal(db,sid,"cancel",{"appointment_id":appt["id"]},f"Cancel {appt['reference']}: {appt['service_name']} on {appt['label']}.",a.get("presentation"))
            slot=self._require_slot(db,a["slot_id"])
            if a["slot_id"] not in {s["id"] for s in state["slots"]}:
                raise DomainError("slot_not_offered","Search availability first.")
            if slot["id"]==appt["slot_id"]:
                raise DomainError("same_slot","This is already your appointment time.")
            self._has_capacity(db,slot,appt["id"])
            return self._proposal(db,sid,"reschedule",{"appointment_id":appt["id"],"slot_id":slot["id"]},f"Move {appt['reference']} from {appt['label']} to {label(slot['start'])}. Service and vehicle stay the same.",a.get("presentation"))
        if name=="dismiss_proposal":
            r=db.execute("SELECT state FROM proposals WHERE id=? AND session_id=? AND tenant=?",(a["proposal_id"],sid,TENANT)).fetchone()
            if not r:
                raise DomainError("proposal_missing","That proposal does not belong to this conversation.")
            if r["state"]=="dismissed":
                return {"ok":True,"message":"The proposal was already withdrawn. No appointment was changed."}
            if r["state"]!="pending" or state.get("proposal_id")!=a["proposal_id"]:
                raise DomainError("stale_proposal","That proposal is no longer pending. Look up the current appointment before describing its status.")
            db.execute("UPDATE proposals SET state='dismissed' WHERE id=?",(a["proposal_id"],))
            self.update(sid,{"proposal_id":None,"stage":"complete" if self.appointment(db,sid) else "service"},db)
            return {"ok":True,"message":"The pending proposal was withdrawn. No appointment was booked, cancelled or changed."}
        if name=="confirm_action":
            r=db.execute("SELECT * FROM proposals WHERE id=? AND session_id=? AND tenant=?",(a["proposal_id"],sid,TENANT)).fetchone()
            if not r:
                raise DomainError("proposal_missing","Prepare the action and read back its summary first.")
            if r["state"]=="committed":
                original=json.loads(r["result"])
                current=self.appointment(db,sid)
                if current!=original.get("appointment"):
                    return {"ok":False,"error":"historical_proposal","operation_completed":True,
                            "historical_result":original,"current_appointment":current,
                            "message":"That proposal completed earlier and the appointment has since changed. No new action was taken. Describe only current_appointment; do not announce the historical result as current or retry the old proposal."}
                return original
            if r["state"]!="pending" or state.get("proposal_id")!=r["id"]:
                raise DomainError("stale_proposal","Details changed. Read back and confirm the new proposal.")
            if r["expires"]<time.time():
                raise DomainError("proposal_expired","That offer expired. Check availability and confirm again.")
            if not a["confirmed"]:
                raise DomainError("confirmation_required","An explicit yes after the exact readback is required.")
            payload=json.loads(r["payload"])
            if r["kind"]=="book":
                slot=self._require_slot(db,payload["slot_id"]); self._has_capacity(db,slot)
                if (self.appointment(db,sid) or {}).get("status")=="confirmed":
                    raise DomainError("already_booked","An appointment already exists. Look it up before proceeding.")
                aid=uid("apt"); reference="CSV-"+uuid.uuid4().hex[:8].upper()
                db.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?)",(aid,reference,sid,TENANT,payload["service"],payload["slot_id"],json.dumps(payload["customer"]),"confirmed"))
            else:
                appt=self.appointment(db,sid)
                if not appt or appt["id"]!=payload["appointment_id"] or appt["status"]!="confirmed":
                    raise DomainError("appointment_changed","The appointment changed. Look it up and prepare again.")
                if r["kind"]=="reschedule":
                    slot=self._require_slot(db,payload["slot_id"]); self._has_capacity(db,slot,appt["id"])
                    db.execute("UPDATE appointments SET slot_id=? WHERE id=?",(slot["id"],appt["id"]))
                else:
                    db.execute("UPDATE appointments SET status='cancelled' WHERE id=?",(appt["id"],))
            result={"ok":True,"action":r["kind"],"appointment":self.appointment(db,sid),
                    **({"presentation":a["presentation"]} if a.get("presentation") is not None else {}),
                    "message":"Scheduler saved the change. You may now confirm the returned status and reference."}
            db.execute("UPDATE proposals SET state='committed',result=? WHERE id=?",(json.dumps(result),r["id"]))
            self.update(sid,{"stage":"complete","proposal_id":None,"slots":[],"action":"book"},db)
            return result
        if name=="request_handoff":
            existing=db.execute("SELECT id,status FROM handoffs WHERE session_id=?",(sid,)).fetchone()
            if existing and existing["status"]=="withdrawn":
                return {"ok":False,"error":"handoff_withdrawn","handoff":self.handoff(db,sid),
                        "message":"The earlier local advisor request is withdrawn. It was not reopened and nobody was contacted. This conversation cannot create a second request."}
            db.execute("INSERT OR IGNORE INTO handoffs VALUES (?,?,?,?,?)",(uid("handoff"),sid,a["reason"],"callback_requested",now().isoformat()))
            row=dict(db.execute("SELECT * FROM handoffs WHERE session_id=?",(sid,)).fetchone())
            db.execute("UPDATE proposals SET state='superseded' WHERE session_id=? AND state='pending'",(sid,))
            self.update(sid,{"stage":"handoff","proposal_id":None},db)
            return {"ok":True,**row,**({"presentation":a["presentation"]} if a.get("presentation") is not None else {}),"message":"Saved to the local demo advisor queue. No advisor was called and no callback delivery is connected. Do not promise a live transfer or response time."}
        if name=="withdraw_handoff":
            row=db.execute("SELECT * FROM handoffs WHERE id=? AND session_id=?",(a["handoff_id"],sid)).fetchone()
            if row is None:
                raise DomainError("handoff_missing","That local advisor request does not belong to this conversation. No request was withdrawn.")
            if row["status"] not in ("callback_requested","withdrawn"):
                raise DomainError("handoff_state_unknown","The advisor request state is not recognized. No change was made; reconcile its status.")
            # dispatch persists a DomainError receipt, so keep any failed
            # withdrawal's business changes out of that outer commit.
            db.execute("SAVEPOINT withdraw_handoff")
            try:
                db.execute("UPDATE handoffs SET status='withdrawn' WHERE id=? AND session_id=?",(row["id"],sid))
                if state.get("stage")=="handoff":
                    self.update(sid,{"stage":"complete" if self.appointment(db,sid) else "service"},db)
                db.execute("RELEASE withdraw_handoff")
            except BaseException:
                db.execute("ROLLBACK TO withdraw_handoff")
                db.execute("RELEASE withdraw_handoff")
                raise
            saved=dict(row);saved["status"]="withdrawn"
            return {"ok":True,**saved,**({"presentation":a["presentation"]} if a.get("presentation") is not None else {}),
                    "message":"The local advisor request was withdrawn. Its audit record remains; no appointment was changed and no external contact was made."}
        raise DomainError("unknown_tool","Unknown tool.")

    def snapshot(self,sid):
        with self.connect() as db:
            state=self.state(sid,db)
            session=db.execute("SELECT mode,ended FROM sessions WHERE id=?",(sid,)).fetchone()
            transcripts=[dict(r) for r in db.execute("SELECT role,text FROM transcripts WHERE session_id=? ORDER BY id",(sid,))]
            events=[dict(r) for r in db.execute("SELECT id,name,status,elapsed_ms,at,summary FROM events WHERE session_id=? ORDER BY id",(sid,))]
            row=db.execute("SELECT * FROM proposals WHERE id=? AND session_id=? AND state='pending'",(state.get("proposal_id"),sid)).fetchone()
            proposal={"id":row["id"],"kind":row["kind"],"summary":json.loads(row["payload"])["summary"],"expires_at":datetime.fromtimestamp(row["expires"],timezone.utc).isoformat()} if row else None
            handoff=self.handoff(db,sid)
            pending=[{"id":r["id"],"status":r["status"]} for r in db.execute("SELECT i.id,i.status FROM tool_invocations i LEFT JOIN tool_results r ON r.session_id=i.session_id AND r.call_id=i.call_id WHERE i.session_id=? AND i.status IN ('queued','running','unknown') AND r.call_id IS NULL",(sid,))]
            values=sorted(e["elapsed_ms"] for e in events)
            percentile=lambda p: values[min(len(values)-1,max(0,__import__('math').ceil(len(values)*p)-1))] if values else None
            return {"id":sid,"mode":session["mode"],"ended":bool(session["ended"]),**state,"transcript":transcripts,
                    "events":events,"proposal":proposal,"appointment":self.appointment(db,sid),"pending_operations":pending,
                    "handoff":handoff,
                    "metrics":{"tool_count":len(events),"tool_p50_ms":percentile(.5),"tool_p95_ms":percentile(.95)}}

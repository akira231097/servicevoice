"""Bounded single-process demo capacity and shared durable call admission.

Configured limits are not measured voice capacity. The sessions table is the
shared authority for browser reservations and admitted phone calls; no process-
local counter may admit an eleventh caller during a race or restart.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from .domain import TENANT, uid


class CapacityError(Exception):
    def __init__(self,code,status=429):
        self.code,self.status=code,status
        super().__init__(code)


@dataclass(frozen=True)
class CapacityConfig:
    profile: str = "default"
    max_live_calls: int = 4
    gateway_concurrency: int = 4
    phone_http_capacity: int = 4
    phone_workers: int = 4
    phone_admissions_per_minute: int = 20
    unbound_session_seconds: int = 120
    session_ttl_seconds: int = 4*3600

    @classmethod
    def from_mapping(cls,values):
        profile=values.get("SERVICEVOICE_CAPACITY_PROFILE","default")
        if profile not in ("default","demo10"):
            raise ValueError("SERVICEVOICE_CAPACITY_PROFILE must be default or demo10.")
        settings=asdict(cls())
        settings["profile"]=profile
        if profile=="demo10":
            settings.update(max_live_calls=10,gateway_concurrency=10,phone_http_capacity=16,phone_workers=10)
        names={"MAX_LIVE_CALLS":("max_live_calls",10),"GATEWAY_CONCURRENCY":("gateway_concurrency",16),
               "PHONE_HTTP_CAPACITY":("phone_http_capacity",32),"PHONE_WORKERS":("phone_workers",16),
               "PHONE_ADMISSIONS_PER_MINUTE":("phone_admissions_per_minute",120)}
        for suffix,(field,maximum) in names.items():
            key="SERVICEVOICE_"+suffix
            if key not in values:continue
            raw=values[key]
            if not isinstance(raw,str) or re.fullmatch(r"[1-9][0-9]*",raw) is None or not 1<=int(raw)<=maximum:
                raise ValueError(f"{key} must be a bounded positive integer (maximum {maximum}).")
            settings[field]=int(raw)
        if any(settings[field]<settings['max_live_calls'] for field in
               ('gateway_concurrency','phone_http_capacity','phone_workers','phone_admissions_per_minute')):
            raise ValueError("Gateway and phone admission capacities must cover the configured live-call limit.")
        return cls(**settings)

    def public_limits(self):
        return {**asdict(self),"tool_workers":4,"tool_pending":64,"status_readers":4,
                "receipt_readers":self.gateway_concurrency,"faq_readers":self.gateway_concurrency,
                "interpretation":"Configured single-process ceilings; not a measured live-voice capacity guarantee."}


class LiveAdmission:
    deadline_seconds=1.0
    busy_seconds=0.1

    def __init__(self,store,config,*,clock=time.time,monotonic=time.monotonic):
        self.store,self.config,self.clock,self.monotonic=store,config,clock,monotonic
        self.writer_lock=threading.Lock()

    def count(self,db):
        moment=self.clock()
        return db.execute("""SELECT COUNT(*) FROM sessions WHERE tenant=? AND mode='live' AND ended=0
            AND created>? AND (provider_call_id IS NOT NULL OR created>?)""",
            (TENANT,moment-self.config.session_ttl_seconds,moment-self.config.unbound_session_seconds)).fetchone()[0]

    def require_room(self,db):
        # Caller owns BEGIN IMMEDIATE; check and insertion are one transaction.
        if self.count(db)>=self.config.max_live_calls:
            raise CapacityError("live_call_capacity")

    def require_browser_reservation(self,session):
        if session['provider_call_id'] is None and self.clock()-session['created']>=self.config.unbound_session_seconds:
            raise CapacityError("live_reservation_expired",409)

    def create_browser_session(self):
        deadline=self.monotonic()+self.deadline_seconds
        def check_deadline():
            if self.monotonic()>=deadline:raise CapacityError("live_admission_deadline",504)
        sid,token=uid("sess"),secrets.token_urlsafe(32)
        state={"stage":"service","slots":[],"customer":None,"service":None,"action":"book"}
        db,owned=None,False
        try:
            owned=self.writer_lock.acquire(timeout=max(0,deadline-self.monotonic()))
            if not owned:raise CapacityError("live_admission_deadline",504)
            check_deadline()
            # The shared lane contains only short admission/end transactions.
            # It never serializes tool execution or status readers. Opening an
            # existing file explicitly also avoids creating an empty database.
            db=sqlite3.connect(Path(self.store.path).resolve().as_uri()+"?mode=rw",uri=True,
                               timeout=min(self.busy_seconds,deadline-self.monotonic()),isolation_level=None)
            db.set_progress_handler(lambda:int(self.monotonic()>=deadline),100)
            db.execute("BEGIN IMMEDIATE")
            check_deadline()
            self.require_room(db)
            check_deadline()
            db.execute("INSERT INTO sessions(id,token_hash,tenant,mode,created,state) VALUES (?,?,?,?,?,?)",
                       (sid,hashlib.sha256(token.encode()).hexdigest(),TENANT,"live",self.clock(),json.dumps(state)))
            check_deadline()
            db.commit()
        except sqlite3.Error:
            if self.monotonic()>=deadline:raise CapacityError("live_admission_deadline",504) from None
            raise CapacityError("live_admission_unavailable",503) from None
        finally:
            if db is not None:
                # A failed precommit check rolls back; an already committed
                # transaction cannot be revoked by a later caller timeout.
                try:
                    if db.in_transaction:db.rollback()
                finally:db.close()
            if owned:self.writer_lock.release()
        return {"id":sid,"token":token,"mode":"live","stage":"service",
                "greeting":"Hi, I’m Cedar’s AI service assistant. This is a fictional dealership demo. What service does your vehicle need?"}

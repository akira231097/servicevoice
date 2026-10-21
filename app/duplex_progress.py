"""Scoped speech-progress evidence, never permission to execute an operation.

Hashes bind existing signed tool issuance to the gateway's latest caller turn.
An actual canonical queued/running invocation is still required. No customer
text, proposal contents, credentials or additional booking state is retained.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time


def text_digest(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS duplex_gateway_turns (
        session_id TEXT PRIMARY KEY, provider_call_id TEXT NOT NULL,
        user_context_sha256 TEXT NOT NULL, user_text_sha256 TEXT NOT NULL,
        user_count INTEGER NOT NULL, generation_id TEXT NOT NULL, updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS duplex_issued_operations (
        session_id TEXT NOT NULL, provider_call_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL, invocation_fingerprint TEXT NOT NULL,
        proposal_id TEXT NOT NULL,
        user_context_sha256 TEXT NOT NULL, user_text_sha256 TEXT NOT NULL,
        user_count INTEGER NOT NULL, generation_id TEXT NOT NULL, expires REAL NOT NULL,
        PRIMARY KEY(provider_call_id,tool_call_id))""")
    for table, columns in (
            ("duplex_gateway_turns",{"session_id","provider_call_id","user_context_sha256","user_text_sha256","user_count","generation_id","updated"}),
            ("duplex_issued_operations",{"session_id","provider_call_id","tool_call_id","tool_name","invocation_fingerprint","proposal_id","user_context_sha256","user_text_sha256","user_count","generation_id","expires"})):
        if {row[1] for row in db.execute(f"PRAGMA table_info({table})")} != columns:
            raise sqlite3.OperationalError("Invalid optional progress evidence schema")


def record_turn(db, sid, call_id, generation_id, messages, context):
    users = [row["content"] for row in messages if row.get("role") == "user"]
    if not context or not users:
        db.execute("DELETE FROM duplex_gateway_turns WHERE session_id=?", (sid,))
        return
    current = db.execute("SELECT user_count FROM duplex_gateway_turns WHERE session_id=?", (sid,)).fetchone()
    if current and current[0] > context["user_count"]:
        return  # An older provider history cannot revive an earlier turn.
    db.execute("""INSERT INTO duplex_gateway_turns VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET provider_call_id=excluded.provider_call_id,
        user_context_sha256=excluded.user_context_sha256,user_text_sha256=excluded.user_text_sha256,
        user_count=excluded.user_count,generation_id=excluded.generation_id,updated=excluded.updated""",
        (sid,call_id,context["user_context_sha256"],text_digest(users[-1]),context["user_count"],generation_id,time.time()))


def record_issued(db, sid, call_id, calls, context, generation_id, expires):
    """Run inside the existing signed-issuance transaction; no new effect."""
    db.execute("DELETE FROM duplex_issued_operations WHERE expires<?", (time.time(),))
    latest = db.execute("SELECT * FROM duplex_gateway_turns WHERE session_id=? AND provider_call_id=?", (sid,call_id)).fetchone()
    if (latest is None or not context or latest["generation_id"] != generation_id
            or latest["user_context_sha256"] != context["user_context_sha256"]
            or latest["user_count"] != context["user_count"]):
        return
    from .tools import DEFINITIONS
    for tool in calls:
        function = tool["function"]
        if function["name"] != "confirm_action":
            continue
        try:
            arguments = json.loads(function["arguments"])
            normalized = DEFINITIONS["confirm_action"][0].model_validate(arguments).model_dump()
        except (ValueError,TypeError):
            continue
        if normalized.get("confirmed") is not True:
            continue
        raw = json.dumps({"name":"confirm_action","args":normalized},sort_keys=True,separators=(",",":"),ensure_ascii=False)
        db.execute("INSERT OR IGNORE INTO duplex_issued_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid,call_id,tool["id"],"confirm_action",hashlib.sha256(raw.encode()).hexdigest(),
             normalized["proposal_id"],
             latest["user_context_sha256"],latest["user_text_sha256"],latest["user_count"],generation_id,expires))


def snapshot(db, sid, pending, state, proposal, expected_generation=None):
    """Return a minimal hint only; cue activation rechecks the same authority."""
    present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='duplex_gateway_turns'").fetchone()
    if not present:
        return {"duplex_canonical_turn":None,"accepted_operation_progress":None}
    latest = db.execute("""SELECT t.* FROM duplex_gateway_turns t JOIN sessions s ON s.id=t.session_id
        WHERE t.session_id=? AND s.provider_call_id=t.provider_call_id AND s.ended=0""", (sid,)).fetchone()
    if latest is None or not expected_generation or latest["generation_id"] != expected_generation:
        return {"duplex_canonical_turn":None,"accepted_operation_progress":None}
    turn_key = hashlib.sha256((latest["provider_call_id"]+"\0"+latest["user_context_sha256"]).encode()).hexdigest()
    turn = {"turn_key":turn_key,"caller_text_sha256":latest["user_text_sha256"],
            "generation_id":latest["generation_id"],"user_count":latest["user_count"]}
    result = {"duplex_canonical_turn":turn,"accepted_operation_progress":None}
    if (state.get("stage") != "confirmation" or not proposal or proposal.get("kind") != "book"
            or len(pending) != 1 or pending[0]["status"] not in {"queued","running"}):
        return result
    rows = db.execute("""SELECT i.id,i.status,p.tool_name,p.user_text_sha256,p.generation_id
        FROM tool_invocations i JOIN duplex_issued_operations p
          ON p.session_id=i.session_id AND p.tool_call_id=i.call_id AND p.invocation_fingerprint=i.fingerprint
        LEFT JOIN tool_results r ON r.session_id=i.session_id AND r.call_id=i.call_id
        WHERE i.session_id=? AND i.id=? AND i.status IN ('queued','running') AND r.call_id IS NULL
          AND p.provider_call_id=? AND p.tool_name='confirm_action' AND p.expires>?
          AND p.proposal_id=?
          AND p.user_context_sha256=? AND p.user_count=? AND p.user_text_sha256=?
          AND p.generation_id=? LIMIT 2""",
        (sid,pending[0]["id"],latest["provider_call_id"],time.time(),proposal["id"],latest["user_context_sha256"],
         latest["user_count"],latest["user_text_sha256"],latest["generation_id"])).fetchall()
    if len(rows) == 1:
        row = rows[0]
        result["accepted_operation_progress"] = {"source":"canonical_queued_operation","kind":"booking_commit",
            "operation_id":row["id"],"status":row["status"],"turn_key":turn_key,
            "caller_text_sha256":row["user_text_sha256"],"generation_id":row["generation_id"]}
    return result

"""Durable evidence of tool JSON actually released to the voice provider.

This is separate from the scheduler's effect receipts: pending responses and
validation failures are observations too. A trusted webhook records before
returning its response; the gateway only accepts an observed version of that
exact session/call/tool result. Methods perform bounded synchronous SQLite work
and must run in a worker thread when called by an async HTTP endpoint.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import sqlite3
import time


class ObservationError(Exception):
    """Fixed public error codes only; never include a result or identifier."""

    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class ObservationLimits:
    ttl_seconds: float = 4 * 3600
    result_bytes: int = 64 * 1024
    versions_per_tool: int = 16
    observations_per_call: int = 256
    observations_total: int = 10000
    bytes_per_call: int = 2 * 1024 * 1024
    bytes_total: int = 32 * 1024 * 1024
    json_nodes: int = 10000
    json_depth: int = 64

    def __post_init__(self):
        if not isinstance(self.ttl_seconds, (int, float)) or isinstance(self.ttl_seconds, bool) or not math.isfinite(self.ttl_seconds) or self.ttl_seconds <= 0:
            raise ValueError("Observation TTL must be positive and finite.")
        for name in self.__dataclass_fields__:
            if name == "ttl_seconds":
                continue
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("Observation limits must be positive integers.")


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("Non-finite JSON number")


def _scope(sid, provider_call_id, tool_call_id):
    if any(not isinstance(item, str) or not 1 <= len(item) <= 200 or "\x00" in item for item in (sid, provider_call_id, tool_call_id)):
        raise ObservationError("invalid_observation_scope", 422)
    return sid, provider_call_id, tool_call_id


def _canonical(result, limits):
    if not isinstance(result, dict):
        raise ObservationError("invalid_tool_observation", 422)
    # Bound traversal before serialization; also rejects cycles by depth/node cap.
    stack, nodes, string_bytes = [(result, 0)], 0, 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > limits.json_nodes or depth > limits.json_depth:
            raise ObservationError("tool_observation_size", 413)
        if isinstance(value, dict):
            if len(value) + len(stack) > limits.json_nodes:
                raise ObservationError("tool_observation_size", 413)
            if any(not isinstance(key, str) for key in value):
                raise ObservationError("invalid_tool_observation", 422)
            for key in value:
                try:
                    if len(key) > limits.result_bytes:
                        raise ObservationError("tool_observation_size", 413)
                    string_bytes += len(key.encode("utf-8"))
                except UnicodeError:
                    raise ObservationError("invalid_tool_observation", 422) from None
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            if len(value) + len(stack) > limits.json_nodes:
                raise ObservationError("tool_observation_size", 413)
            stack.extend((item, depth + 1) for item in value)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise ObservationError("invalid_tool_observation", 422)
        if isinstance(value, str):
            try:
                if len(value) > limits.result_bytes:
                    raise ObservationError("tool_observation_size", 413)
                string_bytes += len(value.encode("utf-8"))
            except UnicodeError:
                raise ObservationError("invalid_tool_observation", 422) from None
        if string_bytes > limits.result_bytes:
            raise ObservationError("tool_observation_size", 413)
    try:
        text = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        encoded = text.encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ObservationError("invalid_tool_observation", 422) from None
    if len(encoded) > limits.result_bytes:
        raise ObservationError("tool_observation_size", 413)
    return text, hashlib.sha256(encoded).hexdigest(), len(encoded)


class ToolObservationStore:
    def __init__(self, path, *, limits=None, clock=time.time):
        self.path, self.limits, self.clock = str(path), limits or ObservationLimits(), clock
        with closing(self.connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tool_observations (
                session_id TEXT NOT NULL, provider_call_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL, digest TEXT NOT NULL,
                result TEXT NOT NULL, result_bytes INTEGER NOT NULL,
                observed REAL NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(session_id,provider_call_id,tool_call_id,digest))""")
            db.execute("CREATE INDEX IF NOT EXISTS tool_observations_expiry ON tool_observations(expires)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def record(self, sid, provider_call_id, tool_call_id, result):
        """Record before releasing a webhook response; never alter an effect.

        Identical replay is idempotent and does not extend retention. Multiple
        versions are retained, so an observed pending response remains valid
        after completion. Unexpired evidence is never evicted to admit new data.
        Expired rows are reclaimed on the next write.
        """
        scope = _scope(sid, provider_call_id, tool_call_id)
        text, digest, size = _canonical(result, self.limits)
        now = self.clock()
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM tool_observations WHERE expires<=?", (now,))
                existing = db.execute("SELECT result FROM tool_observations WHERE session_id=? AND provider_call_id=? AND tool_call_id=? AND digest=?", (*scope, digest)).fetchone()
                if existing:
                    if existing["result"] != text:
                        raise ObservationError("tool_observation_conflict", 409)
                    db.commit()
                    return
                versions = db.execute("SELECT COUNT(*) FROM tool_observations WHERE session_id=? AND provider_call_id=? AND tool_call_id=?", scope).fetchone()[0]
                call = db.execute("SELECT COUNT(*),COALESCE(SUM(result_bytes),0) FROM tool_observations WHERE session_id=? AND provider_call_id=?", scope[:2]).fetchone()
                total = db.execute("SELECT COUNT(*),COALESCE(SUM(result_bytes),0) FROM tool_observations").fetchone()
                limits = self.limits
                if (versions >= limits.versions_per_tool or call[0] >= limits.observations_per_call
                        or total[0] >= limits.observations_total or call[1] + size > limits.bytes_per_call
                        or total[1] + size > limits.bytes_total):
                    raise ObservationError("tool_observation_capacity", 503)
                db.execute("INSERT INTO tool_observations VALUES (?,?,?,?,?,?,?,?)", (*scope, digest, text, size, now, now + limits.ttl_seconds))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def lookup(self, sid, provider_call_id, tool_call_id, incoming_content):
        """Return stored JSON only when the supplied content matches evidence.

        Whitespace and object member order may differ. A forged success cannot
        replace a recorded pending/failure response, nor can evidence cross a
        session, provider call, or tool ID. Result content is never logged.
        """
        scope = _scope(sid, provider_call_id, tool_call_id)
        if not isinstance(incoming_content, str):
            raise ObservationError("invalid_tool_observation", 422)
        try:
            if len(incoming_content) > self.limits.result_bytes or len(incoming_content.encode("utf-8")) > self.limits.result_bytes:
                raise ObservationError("tool_observation_size", 413)
            incoming = json.loads(incoming_content, object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise ObservationError("invalid_tool_observation", 422) from None
        text, digest, _ = _canonical(incoming, self.limits)
        with closing(self.connect()) as db:
            row = db.execute("SELECT result FROM tool_observations WHERE session_id=? AND provider_call_id=? AND tool_call_id=? AND digest=? AND expires>?", (*scope, digest, self.clock())).fetchone()
        if not row or row["result"] != text:
            raise ObservationError("tool_observation_missing", 409)
        # Return authoritative stored data, never the caller's object instance.
        return json.loads(row["result"])

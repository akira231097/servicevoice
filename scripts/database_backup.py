"""Verified SQLite online backup/restore; explicit paths, never overwrite.

No .env, application startup, credentials, network, or live-path default.
Sources: https://www.sqlite.org/backup.html and
https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup

The backup API includes committed WAL content. A pinned read transaction gives
the copied source and validation one consistent snapshot. In WAL mode writers
can continue; a long reader can delay WAL checkpointing. In rollback-journal
mode it can briefly block writers. All SQL/backup steps share a deadline, but
Python cannot forcibly interrupt blocked OS disk I/O or fsync.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_sqlite import ensure_sqlite

SCHEMA_CONTRACT = "servicevoice-recovery-1"
CORE = {
    "sessions": "id token_hash tenant mode created ended state provider_call_id",
    "slots": "id tenant start capacity",
    "proposals": "id session_id tenant kind payload expires state result",
    "appointments": "id reference session_id tenant service slot_id customer status",
    "tool_results": "session_id call_id fingerprint result",
    "tool_invocations": "id session_id call_id fingerprint owner status created updated detail",
    "events": "id session_id name status elapsed_ms at summary",
    "transcripts": "id session_id event_id role text",
    "handoffs": "id session_id reason status created",
}
OPTIONAL = {
    "appointment_services": "appointment_id position service",
    "gemini_tool_signatures": "provider_call_id tool_call_id session_id fingerprint extras group_id ordinal group_size expires",
    "provider_tool_policies": "provider_call_id session_id assistant_id policy",
    "tool_observations": "session_id provider_call_id tool_call_id digest result result_bytes observed expires",
    "advisor_case_metadata": "handoff_id status revision assigned_operator last_operator last_note updated",
    "advisor_case_audit": "id handoff_id revision from_status to_status operator note at",
    "telephony_bindings": "provider_call_id session_id tenant organization_id phone_number_id assistant_id created ended",
}
JSON_FIELDS = {
    "sessions": ("state",), "proposals": ("payload", "result"),
    "appointments": ("customer",), "tool_results": ("result",),
    "gemini_tool_signatures": ("extras",), "tool_observations": ("result",),
}
PRIMARY_KEYS = {
    **{name: ("id",) for name in ("sessions", "slots", "proposals", "appointments", "tool_invocations", "events", "transcripts", "handoffs", "advisor_case_audit")},
    "tool_results": ("session_id", "call_id"), "provider_tool_policies": ("provider_call_id",),
    "gemini_tool_signatures": ("provider_call_id", "tool_call_id"),
    "tool_observations": ("session_id", "provider_call_id", "tool_call_id", "digest"),
    "advisor_case_metadata": ("handoff_id",), "telephony_bindings": ("provider_call_id",),
    "appointment_services": ("appointment_id", "position"),
}
UNIQUE_KEYS = {
    "appointment_services": ("appointment_id", "service"),
    "sessions": ("provider_call_id",), "slots": ("tenant", "start"), "appointments": ("reference",),
    "tool_invocations": ("session_id", "call_id"), "transcripts": ("session_id", "event_id"),
    "handoffs": ("session_id",), "provider_tool_policies": ("session_id",),
    "advisor_case_audit": ("handoff_id", "revision"), "telephony_bindings": ("session_id",),
}

_SQL_TOKENS = re.compile(
    r"(?P<comment>--[^\r\n]*|/\*[\s\S]*?\*/)|"
    r'(?P<quoted>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\])|'
    r"(?P<string>'(?:[^']|'')*')|(?P<token>>=|<=|!=|<>|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s])")


def _check_expressions(statement):
    """Read CHECK tokens without mistaking a comment/string for a constraint.

    SQLite exposes columns, keys and foreign keys through PRAGMAs, but does
    not offer a CHECK-constraint PRAGMA. Only the known position expression is
    accepted below; arbitrary expressions are not executed as a validation probe.
    """
    tokens = []
    for match in _SQL_TOKENS.finditer(statement or ""):
        if match.lastgroup == "comment":
            continue
        token = match.group()
        if match.lastgroup == "quoted":
            token = token[1:-1].replace('""', '"').replace("``", "`")
        tokens.append("<literal>" if match.lastgroup == "string" else token.upper())
    result = []
    for index, token in enumerate(tokens):
        if token != "CHECK":
            continue
        if tokens[index + 1:index + 2] != ["("]:
            return None
        depth = 1
        expression = []
        for child in tokens[index + 2:]:
            depth += (child == "(") - (child == ")")
            if depth == 0:
                break
            expression.append(child)
        if depth:
            return None
        result.append(tuple(expression))
    return result


def _appointment_services_schema(db, statement):
    """Validate the optional child contract while keeping legacy files optional."""
    info = list(db.execute('PRAGMA table_xinfo("appointment_services")'))
    expected = [("appointment_id", "TEXT"), ("position", "INTEGER"), ("service", "TEXT")]
    if (len(info) != len(expected)
            or any(row["name"] != name or row["type"].strip().upper() != kind
                   or row["notnull"] != 1 or row["dflt_value"] is not None or row["hidden"] != 0
                   for row, (name, kind) in zip(info, expected))):
        raise BackupError("incompatible_column_constraints")
    foreign = [tuple(row[key] for key in ("seq", "table", "from", "to", "on_update", "on_delete", "match"))
               for row in db.execute('PRAGMA foreign_key_list("appointment_services")')]
    if foreign != [(0, "appointments", "appointment_id", "id", "NO ACTION", "NO ACTION", "NONE")]:
        raise BackupError("incompatible_foreign_key_constraints")
    unique = []
    for index in db.execute('PRAGMA index_list("appointment_services")'):
        if index["unique"]:
            if index["partial"]:
                raise BackupError("incompatible_key_constraints")
            escaped = index["name"].replace('"', '""')
            unique.append(tuple(row["name"] for row in db.execute(f'PRAGMA index_info("{escaped}")')))
    if set(unique) != {("appointment_id", "position"), ("appointment_id", "service")}:
        raise BackupError("incompatible_key_constraints")
    if _check_expressions(statement) != [("POSITION", ">=", "0")]:
        raise BackupError("incompatible_check_constraints")


class BackupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _check(deadline):
    if time.monotonic() >= deadline:
        raise BackupError("deadline_exceeded")


def _deadline(timeout):
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise BackupError("invalid_timeout")
    return time.monotonic() + timeout


def _path(value, *, source):
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise BackupError("invalid_path")
    if raw.lower().startswith("file:"):
        parsed = urlsplit(raw)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise BackupError("unsafe_file_uri")
        raw = url2pathname(parsed.path)
    elif "://" in raw or raw.startswith(("\\\\", "//")):
        raise BackupError("local_path_required")
    path = Path(raw).expanduser()
    if source:
        if not path.is_file():
            raise BackupError("source_missing")
        return path.resolve(strict=True)
    if os.path.lexists(path):
        raise BackupError("destination_exists")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        raise BackupError("destination_parent_missing") from None
    if not parent.is_dir() or not path.name:
        raise BackupError("invalid_destination")
    result = parent / path.name
    if any(os.path.lexists(str(result) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise BackupError("destination_sidecar_exists")
    return result


@contextmanager
def _connection(path, deadline, *, readonly):
    import sqlite3
    _check(deadline)
    db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                         uri=True, timeout=min(.1, max(.001, deadline-time.monotonic())), isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        if readonly:
            db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        yield db
    finally:
        db.set_progress_handler(None, 0)
        db.close()


def _schema(db, deadline):
    _check(deadline)
    found = {row["name"]: row for row in db.execute("SELECT name,type,sql FROM sqlite_master WHERE substr(name,1,7)!='sqlite_'")}
    for name, columns in CORE.items():
        if name not in found or found[name]["type"] != "table":
            raise BackupError("incompatible_schema")
    tables = sorted(name for name, row in found.items() if row["type"] == "table")
    if set(tables) - CORE.keys() - OPTIONAL.keys():
        raise BackupError("unrecognized_schema")
    if any(row["type"] in ("view", "trigger") for row in found.values()):
        raise BackupError("unrecognized_schema")
    for name in tables:
        info = list(db.execute(f'PRAGMA table_info("{name}")'))
        fields = [row["name"] for row in info]
        if fields != (CORE | OPTIONAL)[name].split():
            raise BackupError("incompatible_schema")
        primary = tuple(row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"])
        if primary != PRIMARY_KEYS[name]:
            raise BackupError("incompatible_key_constraints")
        if name in UNIQUE_KEYS:
            uniques = []
            for index in db.execute(f'PRAGMA index_list("{name}")'):
                if index["unique"] and not index["partial"]:
                    escaped = index["name"].replace('"', '""')
                    uniques.append(tuple(row["name"] for row in db.execute(f'PRAGMA index_info("{escaped}")')))
            if UNIQUE_KEYS[name] not in uniques:
                raise BackupError("incompatible_key_constraints")
    if ("advisor_case_metadata" in tables) != ("advisor_case_audit" in tables):
        raise BackupError("incompatible_schema")
    if any(name in tables for name in ("gemini_tool_signatures", "telephony_bindings")) and "provider_tool_policies" not in tables:
        raise BackupError("incompatible_schema")
    if "appointment_services" in tables:
        _appointment_services_schema(db, found["appointment_services"]["sql"])
    entries = [dict(found[name]) for name in sorted(found)]
    return tables, digest(entries)


def _require_empty(db, query, code):
    if db.execute(query).fetchone():
        raise BackupError(code)


def _json(value):
    try:
        parsed = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(parsed, dict):
            raise ValueError()
        return parsed
    except (TypeError, ValueError, RecursionError):
        raise BackupError("invalid_application_json") from None


def _verify(db, deadline):
    tables, schema_hash = _schema(db, deadline)
    if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
        raise BackupError("integrity_check_failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise BackupError("foreign_key_check_failed")
    for table in ("proposals", "appointments", "tool_results", "tool_invocations", "events", "transcripts", "handoffs"):
        _require_empty(db, f"SELECT 1 FROM {table} x LEFT JOIN sessions s ON s.id=x.session_id WHERE s.id IS NULL LIMIT 1", "orphan_session_reference")
    for table in ("proposals", "appointments"):
        _require_empty(db, f"SELECT 1 FROM {table} x JOIN sessions s ON s.id=x.session_id WHERE x.tenant!=s.tenant LIMIT 1", "tenant_relationship_mismatch")
    _require_empty(db, "SELECT 1 FROM appointments a LEFT JOIN slots s ON s.id=a.slot_id WHERE s.id IS NULL OR a.tenant!=s.tenant LIMIT 1", "appointment_slot_mismatch")
    _require_empty(db, "SELECT 1 FROM slots s LEFT JOIN appointments a ON a.slot_id=s.id AND a.status='confirmed' GROUP BY s.id HAVING s.capacity<=0 OR COUNT(a.id)>s.capacity LIMIT 1", "capacity_invariant_failed")
    _require_empty(db, "SELECT 1 FROM appointments WHERE status NOT IN ('confirmed','cancelled') LIMIT 1", "invalid_appointment_state")
    if "appointment_services" in tables:
        # No child rows is valid for a legacy single-service appointment. Rows
        # that do exist describe one ordered, bounded service set for its parent.
        _require_empty(db, "SELECT 1 FROM appointment_services x LEFT JOIN appointments a ON a.id=x.appointment_id WHERE a.id IS NULL LIMIT 1", "appointment_services_parent_mismatch")
        _require_empty(db, "SELECT 1 FROM appointment_services WHERE typeof(position)!='integer' OR position<0 OR service NOT IN ('oil_change','tires','brakes','diagnostic') LIMIT 1", "invalid_appointment_service")
        _require_empty(db, "SELECT 1 FROM appointment_services GROUP BY appointment_id HAVING COUNT(*)>4 OR MIN(position)!=0 OR MAX(position)!=COUNT(*)-1 LIMIT 1", "appointment_services_order_mismatch")
        _require_empty(db, "SELECT 1 FROM appointment_services x JOIN appointments a ON a.id=x.appointment_id WHERE x.position=0 AND x.service!=a.service LIMIT 1", "appointment_services_primary_mismatch")
    _require_empty(db, "SELECT 1 FROM proposals WHERE kind NOT IN ('book','reschedule','cancel') OR state NOT IN ('pending','superseded','dismissed','committed') LIMIT 1", "invalid_proposal_state")
    _require_empty(db, "SELECT 1 FROM tool_invocations WHERE status NOT IN ('queued','running','unknown','failed','completed','rejected') LIMIT 1", "invalid_operation_state")
    for table, fields in JSON_FIELDS.items():
        if table not in tables:
            continue
        for row in db.execute(f'SELECT * FROM "{table}"'):
            _check(deadline)
            for field in fields:
                if row[field] is not None:
                    _json(row[field])
    for row in db.execute("SELECT id,state FROM sessions"):
        state = _json(row["state"])
        if state.get("proposal_id"):
            current = db.execute("SELECT session_id,state FROM proposals WHERE id=?", (state["proposal_id"],)).fetchone()
            if not current or current["session_id"] != row["id"] or current["state"] != "pending":
                raise BackupError("current_proposal_mismatch")
    for row in db.execute("SELECT * FROM proposals"):
        payload = _json(row["payload"])
        if row["kind"] == "book":
            slot = db.execute("SELECT tenant FROM slots WHERE id=?", (payload.get("slot_id"),)).fetchone()
            if not slot or slot["tenant"] != row["tenant"] or not isinstance(payload.get("customer"), dict):
                raise BackupError("proposal_payload_mismatch")
        else:
            appointment = db.execute("SELECT session_id,tenant FROM appointments WHERE id=?", (payload.get("appointment_id"),)).fetchone()
            if not appointment or (appointment["session_id"], appointment["tenant"]) != (row["session_id"], row["tenant"]):
                raise BackupError("proposal_payload_mismatch")
        if row["state"] == "committed":
            result = _json(row["result"])
            aid = result.get("appointment", {}).get("id") if isinstance(result.get("appointment"), dict) else None
            exists = db.execute("SELECT 1 FROM appointments WHERE id=? AND session_id=?", (aid, row["session_id"])).fetchone()
            if result.get("ok") is not True or result.get("action") != row["kind"] or not exists:
                raise BackupError("committed_proposal_mismatch")
    for row in db.execute("SELECT id,session_id,call_id FROM tool_invocations"):
        expected = "op_" + hashlib.sha256((row["session_id"] + "\0" + row["call_id"]).encode()).hexdigest()[:24]
        if row["id"] != expected:
            raise BackupError("operation_identity_mismatch")
    for table in ("provider_tool_policies", "gemini_tool_signatures", "tool_observations", "telephony_bindings"):
        if table in tables:
            _require_empty(db, f"SELECT 1 FROM {table} x LEFT JOIN sessions s ON s.id=x.session_id WHERE s.id IS NULL OR s.provider_call_id IS NULL OR x.provider_call_id!=s.provider_call_id LIMIT 1", "provider_scope_mismatch")
    if "provider_tool_policies" in tables:
        _require_empty(db, "SELECT 1 FROM provider_tool_policies WHERE policy NOT IN ('gemini-issued','provider-native') LIMIT 1", "invalid_provider_policy")
    if "telephony_bindings" in tables:
        _require_empty(db, "SELECT 1 FROM telephony_bindings b JOIN sessions s ON s.id=b.session_id LEFT JOIN provider_tool_policies p ON p.provider_call_id=b.provider_call_id WHERE b.tenant!=s.tenant OR p.session_id IS NULL OR p.session_id!=b.session_id OR p.assistant_id!=b.assistant_id OR p.policy!='gemini-issued' LIMIT 1", "telephony_policy_mismatch")
    if "tool_observations" in tables:
        for row in db.execute("SELECT * FROM tool_observations"):
            _check(deadline)
            result = _json(row["result"])
            text = canonical(result)
            if digest(result) != row["digest"] or len(text.encode()) != row["result_bytes"]:
                raise BackupError("observation_digest_mismatch")
            receipt = result.get("_receipt")
            if receipt is not None:
                raw = {key: value for key, value in result.items() if key != "_receipt"}
                if not isinstance(receipt, dict) or receipt.get("result_sha256") != digest(raw):
                    raise BackupError("receipt_digest_mismatch")
                # get_operation observations can carry the earlier write's receipt.
                authoritative = db.execute("SELECT r.result FROM tool_invocations i JOIN tool_results r ON r.session_id=i.session_id AND r.call_id=i.call_id WHERE i.id=? AND i.session_id=?", (receipt.get("operation_id"), row["session_id"])).fetchone()
                if not authoritative or _json(authoritative[0]) != raw:
                    raise BackupError("receipt_authority_mismatch")
    if "advisor_case_metadata" in tables:
        for table in ("advisor_case_metadata", "advisor_case_audit"):
            _require_empty(db, f"SELECT 1 FROM {table} m LEFT JOIN handoffs h ON h.id=m.handoff_id WHERE h.id IS NULL LIMIT 1", "advisor_parent_mismatch")
        _require_empty(db, "SELECT 1 FROM advisor_case_metadata m LEFT JOIN advisor_case_audit a ON a.handoff_id=m.handoff_id AND a.revision=m.revision WHERE a.id IS NULL OR a.to_status!=m.status LIMIT 1", "advisor_revision_mismatch")
    hashes, counts = {}, {}
    for table in tables:
        hasher, count = hashlib.sha256(), 0
        for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            _check(deadline)
            hasher.update((canonical(dict(row)) + "\n").encode())
            count += 1
        counts[table], hashes[table] = count, hasher.hexdigest()
    # AUTOINCREMENT history matters to audit IDs even after rows are deleted.
    sequence = [list(row) for row in db.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name")]
    unresolved = db.execute("SELECT COUNT(*) FROM tool_invocations i WHERE NOT EXISTS (SELECT 1 FROM tool_results r WHERE r.session_id=i.session_id AND r.call_id=i.call_id)").fetchone()[0]
    return {"schema_contract": SCHEMA_CONTRACT, "schema_sha256": schema_hash,
            "logical_sha256": digest({"schema": schema_hash, "tables": hashes, "sequence": sequence}),
            "table_counts": counts, "table_sha256": hashes, "unresolved_operations": unresolved,
            "integrity_check": "ok", "relationship_checks": "passed",
            "recovery_policy": "Preserve unresolved operations. A fresh harness reports unknown when no authoritative result exists; do not blindly repeat writes."}


def _backup_pages(source, target, deadline):
    def progress(status, remaining, total):
        _check(deadline)
    source.backup(target, pages=64, progress=progress, sleep=.01)


def _publish_new(stage, destination):
    # Same-directory hard link publishes an already-complete file atomically and
    # fails if another process created the destination. Never use os.replace().
    os.link(stage, destination)


def _file_hash(path, deadline):
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024*1024):
            _check(deadline)
            result.update(chunk)
    return result.hexdigest()


def verify_database(source, *, timeout=30):
    deadline = _deadline(timeout)
    runtime = ensure_sqlite()
    source = _path(source, source=True)
    import sqlite3
    try:
        with _connection(source, deadline, readonly=True) as db:
            db.execute("BEGIN")
            result = _verify(db, deadline)
        return {"ok": True, "operation": "verify", "sqlite_version": runtime, "source": str(source), **result}
    except sqlite3.Error:
        raise BackupError("deadline_exceeded" if time.monotonic() >= deadline else "sqlite_validation_failed") from None
    except (TypeError, ValueError, KeyError, UnicodeError):
        raise BackupError("invalid_application_data") from None


def copy_database(source, destination, *, operation="backup", timeout=30):
    if operation not in ("backup", "restore"):
        raise BackupError("invalid_operation")
    deadline = _deadline(timeout)
    runtime = ensure_sqlite()
    source, destination = _path(source, source=True), _path(destination, source=False)
    if destination in [source, *(Path(str(source)+suffix) for suffix in ("-wal", "-shm", "-journal"))]:
        raise BackupError("source_destination_conflict")
    import sqlite3
    stage = None
    try:
        with _connection(source, deadline, readonly=True) as source_db:
            source_db.execute("BEGIN")
            _schema(source_db, deadline)  # Pin a real app snapshot before creating a destination.
            fd, name = tempfile.mkstemp(prefix="."+destination.name+".incomplete-", suffix=".sqlite3", dir=destination.parent)
            os.close(fd)
            stage = Path(name)
            with _connection(stage, deadline, readonly=False) as target_db:
                _backup_pages(source_db, target_db, deadline)
                # Close the backup into one self-contained artifact, not a main
                # file that relies on an omitted WAL or journal sidecar.
                if target_db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise BackupError("snapshot_journal_mode")
                target_db.execute("PRAGMA synchronous=FULL")
                target_result = _verify(target_db, deadline)
            source_result = _verify(source_db, deadline)
            if source_result["logical_sha256"] != target_result["logical_sha256"]:
                raise BackupError("snapshot_content_mismatch")
        artifact_hash = _file_hash(stage, deadline)
        # Windows _commit/fsync requires a writable descriptor. This is our
        # already-verified staging file, opened without truncation or changes.
        with stage.open("r+b") as output:
            os.fsync(output.fileno())
        _check(deadline)
        _publish_new(stage, destination)
        directory_synced = False
        if os.name != "nt":
            try:
                fd = os.open(destination.parent, os.O_RDONLY)
                try:os.fsync(fd); directory_synced = True
                finally:os.close(fd)
            except OSError:
                pass  # Published artifact is valid; directory crash durability is separately reported.
        return {"ok": True, "operation": operation, "created_at": datetime.now(timezone.utc).isoformat(),
                "source": str(source), "destination": str(destination), "sqlite_version": runtime,
                "artifact_sha256": artifact_hash, "snapshot_content_match": True,
                "published_create_new": True, "directory_fsync": directory_synced,
                **target_result,
                "limitations": ["A local point-in-time snapshot, not continuous replication or a real DMS backup.",
                                "OS disk I/O and fsync cannot be forcibly interrupted by the Python deadline.",
                                "Restore writes only a new path; service shutdown/config switch is a separate operator action.",
                                "Windows directory fsync is unavailable here; file content is flushed before publication."]}
    except FileExistsError:
        raise BackupError("destination_exists") from None
    except sqlite3.Error:
        raise BackupError("deadline_exceeded" if time.monotonic() >= deadline else "sqlite_copy_or_validation_failed") from None
    except (TypeError, ValueError, KeyError, UnicodeError):
        raise BackupError("invalid_application_data") from None
    except OSError:
        raise BackupError("filesystem_unavailable") from None
    finally:
        if stage is not None:
            # Only our unpredictable, explicitly created staging files. A crash
            # can leave one behind; later runs never treat it as a final backup.
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:Path(str(stage)+suffix).unlink(missing_ok=True)
                except OSError:pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("backup", "restore", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--source", required=True, help="Existing local database path or local file URI.")
        if name != "verify":sub.add_argument("--destination", required=True, help="New explicit file path; parent must exist.")
        sub.add_argument("--timeout", type=float, default=30, help="Combined validation/copy budget in seconds, maximum300.")
    args = parser.parse_args(argv)
    try:
        result = verify_database(args.source, timeout=args.timeout) if args.operation == "verify" else copy_database(args.source, args.destination, operation=args.operation, timeout=args.timeout)
        print(json.dumps(result, indent=2))
        return 0
    except (BackupError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": exc.code if isinstance(exc, BackupError) else "sqlite_runtime_unsupported"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

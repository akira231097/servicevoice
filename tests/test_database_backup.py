"""Real SQLite backup/restore exercises on disposable data only."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4
from contextlib import closing

import pytest

from app.domain import Store
from app.harness import ToolHarness, canonical
from app.gemini_gateway import GatewayLimits, SignatureStore
from app.tool_observations import ToolObservationStore
from app.advisor_queue import AdvisorQueue
from app.telephony_ingress import IngressConfig, TelephonyIngress
from app.tools import DEFINITIONS
from scripts import database_backup as backup


class ExistingStore(Store):
    """Exercise the real harness without constructor schema/seeding writes."""
    def __init__(self, path):
        self.path = str(path)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "source.sqlite3")


def snapshot_rows(path):
    with closing(sqlite3.connect(Path(path).as_uri()+"?mode=ro", uri=True)) as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names}


async def recovery_fixture(store):
    signatures = SignatureStore(store.path, GatewayLimits())
    observations = ToolObservationStore(store.path)
    advisor = AdvisorQueue(store)
    # This fixture constructs completed evidence for backup/restore assertions.
    # It does not test the production 1.5-second pending-response deadline.
    harness = ToolHarness(store, timeout_seconds=30, status_timeout_seconds=5)
    owner = store.create_session("live")
    cid = str(uuid4())
    with store.connect() as db:
        db.execute("UPDATE sessions SET provider_call_id=? WHERE id=?", (cid, owner["id"]))
    signatures.pin_call_policy(owner["id"], cid, "synthetic-assistant", {"VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom"})
    slots = await harness.run(owner["id"], "begin_service_request", {"service": "oil_change"}, "begin")
    prepared = await harness.run(owner["id"], "prepare_booking", {
        "service": "oil_change", "slot_id": slots["slots"][0]["id"], "customer_name": "Synthetic Alex",
        "phone": "5125550142", "vehicle": "2021 Toyota Camry",
    }, "prepare")
    assert prepared.get("ok") is True, prepared
    consent_args = {"proposal_id": prepared["proposal_id"], "confirmed": True}
    booked = await harness.run(owner["id"], "confirm_action", consent_args, "commit")
    assert booked["ok"]
    observations.record(owner["id"], cid, "commit", booked)
    # Simulate a crash after authoritative commit but before journal completion.
    with store.connect() as db:
        db.execute("UPDATE tool_invocations SET status='running' WHERE id=?", (booked["_receipt"]["operation_id"],))
    pending_owner = store.create_session("live")
    pending = await harness.run(pending_owner["id"], "prepare_first_available_booking", {
        "strategy": "first_available", "service": "tires", "customer_name": "Synthetic Sam",
        "phone": "5125550199", "vehicle": "2020 Honda Civic",
    }, "pending-proposal")
    assert pending["ok"]
    handoff_owner = store.create_session("live")
    handoff = await harness.run(handoff_owner["id"], "request_handoff", {"reason": "Synthetic warranty question"}, "advisor")
    advisor.update_case(handoff["id"], expected_revision=0, status="in_progress", operator="Synthetic operator", note="Review required.\nNo callback promised.")
    uncertain_owner = store.create_session("live")
    uncertain = []
    args = DEFINITIONS["request_handoff"][0].model_validate({"reason": "Uncertain synthetic operation"}).model_dump()
    for status in ("queued", "running", "unknown"):
        tool_id = "uncertain-"+status
        opid = "op_"+hashlib.sha256((uncertain_owner["id"]+"\0"+tool_id).encode()).hexdigest()[:24]
        fingerprint = hashlib.sha256(canonical({"name": "request_handoff", "args": args}).encode()).hexdigest()
        accepted, result = harness._register(uncertain_owner["id"], "request_handoff", args, tool_id, opid, fingerprint)
        assert accepted and result is None
        with store.connect() as db:
            db.execute("UPDATE tool_invocations SET status=? WHERE id=?", (status, opid))
        uncertain.append({"tool_id": tool_id, "operation_id": opid, "arguments": args})
    phone = TelephonyIngress(store, IngressConfig(str(uuid4()), str(uuid4()), str(uuid4()), "synthetic-phone-secret-"+"x"*32))
    phone.admit({"type": "assistant-request", "call": {"id": str(uuid4()), "type": "inboundPhoneCall", "orgId": phone.config.organization_id, "phoneNumberId": phone.config.phone_number_id}}, "Bearer "+phone.config.webhook_secret)
    await harness.close()
    return {"owner": owner, "booked": booked, "consent_args": consent_args, "pending": pending,
            "uncertain_owner": uncertain_owner, "uncertain": uncertain, "handoff_id": handoff["id"]}


def test_verified_restore_preserves_booking_receipt_proposal_audit_and_unknown_operations(store, tmp_path, monkeypatch):
    evidence = asyncio.run(recovery_fixture(store))
    original = snapshot_rows(store.path)
    archive, restored = tmp_path/"archive.sqlite3", tmp_path/"restored.sqlite3"
    first = backup.copy_database(store.path, archive)
    second = backup.copy_database(archive, restored, operation="restore")
    assert first["snapshot_content_match"] and second["snapshot_content_match"]
    assert first["logical_sha256"] == second["logical_sha256"]
    assert first["unresolved_operations"] == 3
    assert snapshot_rows(archive) == snapshot_rows(restored) == original
    assert not Path(str(restored)+"-wal").exists()
    restored_store = ExistingStore(restored)
    async def reconcile():
        harness = ToolHarness(restored_store)
        # No dependency execution, even for a retry of unresolved work.
        def forbidden(*args, **kwargs):
            pytest.fail("Recovery must not reexecute an accepted/uncertain effect")
        monkeypatch.setattr("app.harness.execute", forbidden)
        receipt = await harness.operation(evidence["owner"]["id"], evidence["booked"]["_receipt"]["operation_id"])
        assert receipt == evidence["booked"]
        assert await harness.run(evidence["owner"]["id"], "confirm_action", evidence["consent_args"], "commit") == receipt
        for item in evidence["uncertain"]:
            status = await harness.operation(evidence["uncertain_owner"]["id"], item["operation_id"])
            assert status["error"] == "operation_unknown"
            retried = await harness.run(evidence["uncertain_owner"]["id"], "request_handoff", item["arguments"], item["tool_id"])
            assert retried["error"] == "operation_unknown"
        await harness.close()
    asyncio.run(reconcile())
    assert snapshot_rows(restored) == original
    case = AdvisorQueue(restored_store, initialize_schema=False).get_case(evidence["handoff_id"])
    assert case["status"] == "in_progress" and case["revision"] == 1
    assert len(original["appointments"]) == 1


def test_online_backup_includes_committed_wal_but_not_uncommitted_transaction(store, tmp_path):
    with store.connect() as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        sid = store.create_session("live")["id"]
        writer.execute("INSERT INTO transcripts(session_id,event_id,role,text) VALUES (?,?,?,?)", (sid,"committed","user","Synthetic committed WAL row"))
        assert Path(store.path+"-wal").exists()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO transcripts(session_id,event_id,role,text) VALUES (?,?,?,?)", (sid,"uncommitted","user","Must not be backed up"))
        target = tmp_path/"wal-backup.sqlite3"
        result = backup.copy_database(store.path, target)
        assert result["integrity_check"] == "ok"
        writer.rollback()
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT event_id FROM transcripts").fetchall() == [("committed",)]


def test_concurrent_commits_do_not_tear_cross_table_snapshot(store, tmp_path, monkeypatch):
    sid = store.create_session("live")["id"]
    started, finished = threading.Event(), threading.Event()
    original = backup._backup_pages
    failures = []
    def writer():
        assert started.wait(2)
        try:
            with store.transaction() as db:
                db.execute("INSERT INTO transcripts(session_id,event_id,role,text) VALUES (?,?,?,?)", (sid,"atomic","user","Synthetic atomic marker"))
                db.execute("INSERT INTO events(session_id,name,status,elapsed_ms,at,summary) VALUES (?,?,?,?,?,?)", (sid,"atomic","success",0,"2026-10-18T00:00:00Z","Synthetic atomic marker"))
        except Exception as exc:failures.append(type(exc).__name__)
        finally:finished.set()
    def interleaved(*args):
        started.set()
        assert finished.wait(2), "WAL writer must progress while source read snapshot is pinned"
        original(*args)
    monkeypatch.setattr(backup, "_backup_pages", interleaved)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        target = tmp_path/"concurrent.sqlite3"
        backup.copy_database(store.path, target)
    finally:thread.join(timeout=3)
    assert not failures
    with sqlite3.connect(target) as db:
        transcript_count = db.execute("SELECT COUNT(*) FROM transcripts WHERE event_id='atomic'").fetchone()[0]
        event_count = db.execute("SELECT COUNT(*) FROM events WHERE name='atomic'").fetchone()[0]
        assert transcript_count == event_count == 0  # Snapshot was pinned before concurrent commit.


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_existing_destination_is_never_overwritten(store, tmp_path, operation):
    target = tmp_path/"existing.sqlite3"
    target.write_bytes(b"preserve this existing file")
    with pytest.raises(backup.BackupError, match="destination_exists"):
        backup.copy_database(store.path, target, operation=operation)
    assert target.read_bytes() == b"preserve this existing file"


def test_destination_race_is_create_new_not_replace(store, tmp_path, monkeypatch):
    target = tmp_path/"raced.sqlite3"
    original = backup._publish_new
    def competing(stage, destination):
        destination.write_bytes(b"other owner's file")
        original(stage, destination)
    monkeypatch.setattr(backup, "_publish_new", competing)
    with pytest.raises(backup.BackupError, match="destination_exists"):
        backup.copy_database(store.path, target)
    assert target.read_bytes() == b"other owner's file"
    assert not list(tmp_path.glob(".*.incomplete-*"))


def test_missing_source_never_created_and_corrupt_source_never_published(tmp_path):
    source, target = tmp_path/"missing.sqlite3", tmp_path/"target.sqlite3"
    with pytest.raises(backup.BackupError, match="source_missing"):
        backup.copy_database(source, target)
    assert not source.exists() and not target.exists()
    source.write_bytes(b"This is not a SQLite database")
    with pytest.raises(backup.BackupError, match="sqlite_copy_or_validation_failed"):
        backup.copy_database(source, target)
    assert not target.exists()


def test_incompatible_schema_and_missing_unique_keys_rejected(store, tmp_path):
    target = tmp_path/"bad.sqlite3"
    with store.connect() as db:
        db.execute("ALTER TABLE tool_results RENAME TO old_results")
        db.execute("CREATE TABLE tool_results(session_id TEXT,call_id TEXT,fingerprint TEXT,result TEXT)")
        db.execute("DROP TABLE old_results")
    with pytest.raises(backup.BackupError, match="incompatible_key_constraints"):
        backup.copy_database(store.path, target)
    assert not target.exists()


def test_sqlite_like_user_table_is_not_hidden_by_internal_prefix_filter(store, tmp_path):
    with store.connect() as db:
        db.execute("CREATE TABLE sqliteevil(secret TEXT)")
    with pytest.raises(backup.BackupError, match="unrecognized_schema"):
        backup.copy_database(store.path, tmp_path/"unknown.sqlite3")


@pytest.mark.parametrize("mutation,code", [
    ("INSERT INTO tool_results VALUES ('orphan','id','fp','{}')", "orphan_session_reference"),
    ("UPDATE slots SET capacity=0", "sqlite_copy_or_validation_failed"),
])
def test_broken_application_relationship_is_rejected(store, tmp_path, mutation, code):
    with store.connect() as db:
        if "capacity=0" in mutation:db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(mutation)
    with pytest.raises(backup.BackupError):
        backup.copy_database(store.path, tmp_path/"invalid.sqlite3")
    assert not (tmp_path/"invalid.sqlite3").exists()


def test_tampered_observation_digest_cannot_be_restored(store, tmp_path):
    asyncio.run(recovery_fixture(store))
    with store.connect() as db:
        db.execute("UPDATE tool_observations SET digest=?", ("0"*64,))
    with pytest.raises(backup.BackupError, match="observation_digest_mismatch"):
        backup.copy_database(store.path, tmp_path/"tampered.sqlite3", operation="restore")


def test_valid_observation_hash_cannot_replace_authoritative_receipt(store, tmp_path):
    asyncio.run(recovery_fixture(store))
    with store.connect() as db:
        row = db.execute("SELECT * FROM tool_observations").fetchone()
        forged = json.loads(row["result"])
        forged["appointment"]["reference"] = "CSV-FORGED"
        raw = {key: value for key, value in forged.items() if key != "_receipt"}
        forged["_receipt"]["result_sha256"] = backup.digest(raw)
        text = backup.canonical(forged)
        db.execute("UPDATE tool_observations SET result=?,digest=?,result_bytes=?", (text,backup.digest(forged),len(text.encode())))
    with pytest.raises(backup.BackupError, match="receipt_authority_mismatch"):
        backup.copy_database(store.path, tmp_path/"forged.sqlite3")


def test_locked_source_deadline_does_not_publish(store, tmp_path):
    # Rollback-journal exclusive lock exercises the configured bounded wait.
    with store.connect() as lock:
        lock.execute("PRAGMA journal_mode=DELETE")
        lock.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(backup.BackupError):
            backup.copy_database(store.path, tmp_path/"locked.sqlite3", timeout=.05)
        assert time.monotonic()-start < .75
        lock.rollback()
    assert not (tmp_path/"locked.sqlite3").exists()


def test_interrupted_backup_never_publishes_partial_and_cleans_owned_stage(store, tmp_path, monkeypatch):
    def interrupt(source, target, deadline):
        target.execute("CREATE TABLE partial(data TEXT)")
        raise KeyboardInterrupt()
    monkeypatch.setattr(backup, "_backup_pages", interrupt)
    with pytest.raises(KeyboardInterrupt):
        backup.copy_database(store.path, tmp_path/"interrupted.sqlite3")
    assert not (tmp_path/"interrupted.sqlite3").exists()
    assert not list(tmp_path.glob(".*.incomplete-*"))


def test_os_process_exit_leaves_only_unpublished_stage_and_next_run_recovers(store, tmp_path):
    target = tmp_path/"crashed.sqlite3"
    code = """import os,sys
from scripts import database_backup as b
def stop(source,target,deadline):
    target.execute('CREATE TABLE unfinished(data TEXT)')
    os._exit(23)
b._backup_pages=stop
b.copy_database(sys.argv[1],sys.argv[2])
"""
    child = subprocess.run([sys.executable,"-c",code,store.path,str(target)],cwd=backup.ROOT,capture_output=True,text=True,timeout=10)
    assert child.returncode == 23, child.stderr
    assert not target.exists()
    abandoned = list(tmp_path.glob(".*.incomplete-*"))
    assert abandoned
    result = backup.copy_database(store.path, target)
    assert result["ok"] and target.exists()
    assert all(path.exists() for path in abandoned)  # Never remove unrelated earlier staging files.


def test_paths_with_spaces_hash_percent_and_local_file_uri_are_literal(tmp_path):
    store = Store(tmp_path/"source # percent % name.sqlite3")
    result = backup.copy_database(Path(store.path).as_uri(), tmp_path/"target # %.sqlite3")
    assert result["ok"]
    assert backup.verify_database(result["destination"])["logical_sha256"] == result["logical_sha256"]


@pytest.mark.parametrize("uri", ["https://example.invalid/data.sqlite3", "file://remotehost/share/db.sqlite3", "file:///C:/test.sqlite3?mode=rw"])
def test_network_or_sqlite_option_uris_rejected(uri, tmp_path):
    with pytest.raises(backup.BackupError):
        backup.copy_database(uri, tmp_path/"never.sqlite3")
    assert not (tmp_path/"never.sqlite3").exists()


def test_existing_destination_sidecar_is_not_reused(store, tmp_path):
    destination = tmp_path/"other.sqlite3"
    sidecar = Path(str(destination)+"-wal")
    sidecar.write_bytes(b"existing state")
    with pytest.raises(backup.BackupError, match="destination_sidecar_exists"):
        backup.copy_database(store.path, destination)
    assert sidecar.read_bytes() == b"existing state" and not destination.exists()


def test_cli_requires_explicit_paths_and_emits_only_verification_summary(store, tmp_path):
    destination = tmp_path/"cli.sqlite3"
    completed = subprocess.run([sys.executable,str(backup.ROOT/"scripts/database_backup.py"),"backup","--source",store.path,"--destination",str(destination),"--timeout","10"],capture_output=True,text=True,timeout=15)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] and result["published_create_new"]
    assert "token_hash" not in completed.stdout and "customer" not in completed.stdout
    denied = subprocess.run([sys.executable,str(backup.ROOT/"scripts/database_backup.py"),"restore","--source",str(destination),"--destination",store.path],capture_output=True,text=True,timeout=10)
    assert denied.returncode == 2 and json.loads(denied.stderr)["error"] == "destination_exists"

"""Recovery preserves cue evidence and unknown phone ownership without replay."""
import asyncio
import pytest

from app.capacity import LiveAdmission
from app.gemini_gateway import GatewayLimits,SignatureStore
from app.phone_bridge_api import PhoneBridgeAdmissions
from app.telephony_ingress import TelephonyIngress
from scripts import database_backup as backup
from test_database_backup import ExistingStore,snapshot_rows
from test_duplex_progress import accepted
from test_phone_bridge_admission import setup,admit,error
from test_telephony_ingress import config,message,BROWSER


def test_restore_preserves_real_accepted_operation_but_never_restores_live_generation_authority(tmp_path,monkeypatch):
    async def scenario():
        async with accepted(tmp_path,monkeypatch) as env:
            assert env.store.snapshot(env.sid)["accepted_operation_progress"]
            archive,restored=tmp_path/"archive.sqlite3",tmp_path/"restore.sqlite3"
            original=snapshot_rows(env.store.path)
            first=backup.copy_database(env.store.path,archive)
            second=backup.copy_database(archive,restored,operation="restore")
            assert first["logical_sha256"]==second["logical_sha256"]
            assert first["table_counts"]["duplex_gateway_turns"]==1
            assert first["table_counts"]["duplex_issued_operations"]==1
            assert snapshot_rows(restored)==snapshot_rows(archive)==original
            recovered=ExistingStore(restored)
            signatures=SignatureStore(restored,GatewayLimits())
            recovered.duplex_progress_generations=signatures.duplex_generations
            snapshot=recovered.snapshot(env.sid)
            assert snapshot["accepted_operation_progress"] is None and snapshot["duplex_canonical_turn"] is None
            assert snapshot["pending_operations"] and snapshot["appointment"] is None
            assert signatures.duplex_generations=={}
            assert snapshot_rows(restored)==original
    asyncio.run(scenario())


def test_unknown_phone_allocation_remains_owned_after_expiry_and_restore(setup,tmp_path):
    s=setup;outer=message()["call"];session=admit(s,outer)
    s.bridge.allocation(outer["id"]);s.clock[0]+=1000
    original=snapshot_rows(s.store.path)
    archive,restored=tmp_path/"phone-archive.sqlite3",tmp_path/"phone-restored.sqlite3"
    first=backup.copy_database(s.store.path,archive)
    second=backup.copy_database(archive,restored,operation="restore")
    assert first["logical_sha256"]==second["logical_sha256"]
    assert first["table_counts"]["phone_bridge_admissions"]==1
    assert snapshot_rows(restored)==original
    recovered=ExistingStore(restored)
    capacity=LiveAdmission(recovered,s.capacity.config,clock=lambda:s.clock[0])
    ingress=TelephonyIngress(recovered,s.ingress.config,live_admission=capacity,clock=lambda:s.clock[0])
    bridge=PhoneBridgeAdmissions(ingress,BROWSER,s.bridge.secret)
    row=bridge.lookup(outer["id"])
    assert bridge.state(row)=="requested" and row["inner_call_id"] is None and row["closed"] is None
    assert row["session_id"]==session["id"] and row["expires_at"]<s.clock[0]
    with recovered.connect() as db:assert capacity.count(db)==1
    error("phone_bridge_call_ended",bridge.admit,outer,s.clock[0]+300)
    assert snapshot_rows(restored)==original


@pytest.mark.parametrize("kind",["arbitrary","extra_progress_column","missing_progress_peer","missing_phone_unique"])
def test_optional_metadata_compatibility_never_allows_unknown_or_malformed_schema(setup,tmp_path,kind):
    s=setup
    SignatureStore(s.store.path,GatewayLimits())
    with s.store.connect() as db:
        if kind=="arbitrary":db.execute("CREATE TABLE arbitrary_new_table(value TEXT)")
        elif kind=="extra_progress_column":db.execute("ALTER TABLE duplex_gateway_turns ADD COLUMN unauthorized TEXT")
        elif kind=="missing_progress_peer":db.execute("DROP TABLE duplex_issued_operations")
        else:
            statement=db.execute("SELECT sql FROM sqlite_master WHERE name='phone_bridge_admissions'").fetchone()[0]
            db.execute("DROP TABLE phone_bridge_admissions")
            db.execute(statement.replace("inner_call_id TEXT UNIQUE","inner_call_id TEXT"))
    destination=tmp_path/"must-not-exist.sqlite3"
    with pytest.raises(backup.BackupError,match="unrecognized_schema|incompatible_schema|incompatible_key_constraints"):
        backup.copy_database(s.store.path,destination)
    assert not destination.exists()


@pytest.mark.parametrize("change",["tenant","inner_binding","closed_unknown"])
def test_backup_rejects_cross_scope_or_falsely_closed_phone_ownership(setup,tmp_path,change):
    s=setup;outer=message()["call"];admit(s,outer);s.bridge.allocation(outer["id"])
    with s.store.connect() as db:
        if change=="tenant":db.execute("UPDATE phone_bridge_admissions SET tenant='another-tenant'")
        elif change=="inner_binding":
            db.execute("UPDATE phone_bridge_admissions SET inner_call_id='inner-one'")
            db.execute("UPDATE sessions SET provider_call_id='inner-two'")
        else:db.execute("UPDATE phone_bridge_admissions SET outer_ended=1,closed=1")
    destination=tmp_path/"invalid-ownership.sqlite3"
    with pytest.raises(backup.BackupError,match="phone_bridge_scope_mismatch|phone_bridge_ownership_mismatch"):
        backup.copy_database(s.store.path,destination)
    assert not destination.exists()

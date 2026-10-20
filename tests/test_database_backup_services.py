"""Optional multi-service child evidence must survive recovery or fail closed.

All sources are disposable local SQLite databases; no application secrets,
external provider, live scheduler or deployment is used.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from app.domain import Store
from scripts import database_backup as backup


@pytest.fixture
def source(tmp_path):
    return Store(tmp_path / "source.sqlite3")


def book(source, services=None):
    sid = source.create_session()["id"]
    selected = services or ["diagnostic"]
    arguments = {"service": selected[0], **({"services": selected} if len(selected) > 1 else {})}
    availability = source.dispatch(sid, "begin_service_request", arguments, "search")
    assert availability["ok"]
    proposal = source.dispatch(sid, "prepare_booking", {
        **arguments, "slot_id": availability["slots"][0]["id"], "customer_name": "Synthetic Customer",
        "phone": "5125550142", "vehicle": "2022 Synthetic Car",
    }, "prepare")
    assert proposal["ok"]
    result = source.dispatch(sid, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True}, "confirm")
    assert result["ok"]
    return result["appointment"]


def rows(path):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT appointment_id,position,service FROM appointment_services ORDER BY appointment_id,position").fetchall()


def test_bundle_backup_restore_preserves_ordered_children_and_single_capacity_record(source, tmp_path):
    appointment = book(source, ["diagnostic", "brakes", "oil_change"])
    original = rows(source.path)
    assert original == [(appointment["id"], 0, "diagnostic"), (appointment["id"], 1, "brakes"),
                        (appointment["id"], 2, "oil_change")]
    archive, restored = tmp_path / "bundle.sqlite3", tmp_path / "restored.sqlite3"
    first = backup.copy_database(source.path, archive)
    second = backup.copy_database(archive, restored, operation="restore")
    assert first["ok"] and second["ok"]
    assert first["snapshot_content_match"] and second["snapshot_content_match"]
    assert first["logical_sha256"] == second["logical_sha256"]
    assert first["table_counts"]["appointments"] == 1
    assert first["table_counts"]["appointment_services"] == 3
    assert rows(restored) == rows(archive) == original


def test_legacy_backup_without_child_table_remains_identical_and_is_not_migrated(source, tmp_path):
    book(source)
    with source.connect() as db:
        db.execute("DROP TABLE appointment_services")
    archive, restored = tmp_path / "legacy.sqlite3", tmp_path / "legacy-restored.sqlite3"
    first = backup.copy_database(source.path, archive)
    second = backup.copy_database(archive, restored, operation="restore")
    assert first["logical_sha256"] == second["logical_sha256"]
    assert "appointment_services" not in first["table_counts"]
    for path in (archive, restored):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='appointment_services'").fetchone()[0] == 0


def test_new_optional_table_can_coexist_with_legacy_parent_without_backfilled_children(source):
    appointment = book(source)
    with source.connect() as db:
        db.execute("DELETE FROM appointment_services WHERE appointment_id=?", (appointment["id"],))
    result = backup.verify_database(source.path)
    assert result["ok"] and result["table_counts"]["appointment_services"] == 0
    assert result["table_counts"]["appointments"] == 1


@pytest.mark.parametrize("change", [
    "missing_primary", "reversed_primary", "missing_unique", "partial_unique", "missing_foreign_key",
    "wrong_foreign_key", "missing_check", "comment_fake_check", "string_fake_check", "wrong_check",
    "nullable_position", "text_position", "hidden_column", "extra_unique", "cascading_delete",
])
def test_malformed_child_definition_is_rejected_before_publishing(source, tmp_path, change):
    with source.connect() as db:
        statement = db.execute("SELECT sql FROM sqlite_master WHERE name='appointment_services'").fetchone()[0]
        db.execute("DROP TABLE appointment_services")
        if change == "missing_primary":
            statement = statement.replace("PRIMARY KEY(appointment_id,position),", "")
        elif change == "reversed_primary":
            statement = statement.replace("PRIMARY KEY(appointment_id,position)", "PRIMARY KEY(position,appointment_id)")
        elif change in {"missing_unique", "partial_unique"}:
            statement = statement.replace(", UNIQUE(appointment_id,service)", "")
        elif change == "missing_foreign_key":
            statement = statement.replace(" REFERENCES appointments(id)", "")
        elif change == "wrong_foreign_key":
            statement = statement.replace("REFERENCES appointments(id)", "REFERENCES sessions(id)")
        elif change == "missing_check":
            statement = statement.replace(" CHECK(position >= 0)", "")
        elif change == "comment_fake_check":
            statement = statement.replace("CHECK(position >= 0)", "/* CHECK(position >= 0) */")
        elif change == "string_fake_check":
            statement = statement.replace("CHECK(position >= 0)", "DEFAULT 'CHECK(position >= 0)'")
        elif change == "wrong_check":
            statement = statement.replace("CHECK(position >= 0)", "CHECK(position >= -1)")
        elif change == "nullable_position":
            statement = statement.replace("position INTEGER NOT NULL", "position INTEGER")
        elif change == "text_position":
            statement = statement.replace("position INTEGER", "position TEXT")
        elif change == "hidden_column":
            statement = statement.replace("PRIMARY KEY(", "hidden TEXT GENERATED ALWAYS AS ('hidden') VIRTUAL, PRIMARY KEY(")
        elif change == "extra_unique":
            statement = statement.replace("UNIQUE(appointment_id,service)", "UNIQUE(appointment_id,service), UNIQUE(service)")
        elif change == "cascading_delete":
            statement = statement.replace("REFERENCES appointments(id)", "REFERENCES appointments(id) ON DELETE CASCADE")
        db.execute(statement)
        if change == "partial_unique":
            db.execute("CREATE UNIQUE INDEX partial_services ON appointment_services(appointment_id,service) WHERE position=0")
    target = tmp_path / "not-published.sqlite3"
    with pytest.raises(backup.BackupError):
        backup.copy_database(source.path, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.incomplete-*"))


@pytest.mark.parametrize("change,code", [
    ("orphan", "foreign_key_check_failed"),
    ("unsupported_service", "invalid_appointment_service"),
    ("noninteger_position", "invalid_appointment_service"),
    ("gap", "appointment_services_order_mismatch"),
    ("wrong_primary", "appointment_services_primary_mismatch"),
])
def test_malformed_present_service_data_cannot_be_restored(source, tmp_path, change, code):
    appointment = book(source, ["diagnostic", "brakes"])
    with source.connect() as db:
        if change == "orphan":
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO appointment_services VALUES ('unknown-appointment',0,'diagnostic')")
        elif change == "unsupported_service":
            db.execute("UPDATE appointment_services SET service='unverified-repair' WHERE position=1")
        elif change == "noninteger_position":
            db.execute("UPDATE appointment_services SET position=1.5 WHERE position=1")
        elif change == "gap":
            db.execute("UPDATE appointment_services SET position=2 WHERE position=1")
        elif change == "wrong_primary":
            db.execute("UPDATE appointments SET service='oil_change' WHERE id=?", (appointment["id"],))
    target = tmp_path / "invalid-data.sqlite3"
    with pytest.raises(backup.BackupError, match=code):
        backup.copy_database(source.path, target, operation="restore")
    assert not target.exists()

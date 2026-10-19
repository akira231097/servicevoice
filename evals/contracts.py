"""Offline business-contract evaluations against real disposable schedulers.

These cases exercise the domain and ToolHarness, not a model, transcript, audio,
or human consent decision. Every fixture is freshly constructed under a private
temporary directory. No credentials or running application database are read.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
import tempfile

from runtime_sqlite import ensure_sqlite

ensure_sqlite()
from app.domain import Store, today
from app.harness import ToolHarness


CONTRACT_IDS = (
    "proposal_requires_confirmation",
    "exact_retry_single_effect",
    "stale_proposal_rejected",
    "corrected_proposal_declined",
    "proposal_session_ownership",
    "exact_time_availability",
    "exclusive_time_boundaries",
    "handoff_withdrawal_persists",
    "handoff_session_ownership",
    "reschedule_requires_confirmation",
    "cancellation_and_historical_receipt",
    "competing_bookings_respect_capacity",
)
CUSTOMER = {"customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2021 Toyota Camry"}


class ContractFailure(Exception):
    """Only public fixed failure codes may escape a fixture."""


class Fixture:
    def __init__(self, path, row):
        self.store = Store(path)
        self.harness = ToolHarness(self.store)
        self.sid = self.store.create_session("live")["id"]
        self.row = row
        self.sequence = 0
        self.day = today() + timedelta(days=1)
        while self.day.weekday() >= 5:
            self.day += timedelta(days=1)

    def check(self, name, condition, *, required=False):
        self.row["checks"].append({"name": name, "passed": condition is True})
        if required and condition is not True:
            raise ContractFailure("fixture_prerequisite_failed")

    async def call(self, name, arguments, *, sid=None, call_id=None):
        self.sequence += 1
        return await self.harness.run(sid or self.sid, name, arguments,
                                      call_id or f"contract-{self.sequence}")

    async def search(self, *, sid=None, time_filter=None):
        arguments = {"service": "oil_change", "date": self.day.isoformat()}
        if time_filter is not None:
            arguments["time_filter"] = time_filter
        result = await self.call("search_availability", arguments, sid=sid)
        self.check("availability_query_succeeded", result.get("ok") is True, required=True)
        return result

    async def prepare(self, *, sid=None, slot_id=None, vehicle=None):
        if slot_id is None:
            result = await self.search(sid=sid)
            self.check("fixture_has_available_slot", bool(result.get("slots")), required=True)
            slot_id = result["slots"][0]["id"]
        arguments = {**CUSTOMER, "service": "oil_change", "slot_id": slot_id}
        if vehicle is not None:
            arguments["vehicle"] = vehicle
        result = await self.call("prepare_booking", arguments, sid=sid)
        self.check("proposal_prepared", result.get("ok") is True and bool(result.get("proposal_id")), required=True)
        return result

    async def commit(self, proposal, *, sid=None, call_id=None):
        return await self.call("confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True},
                               sid=sid, call_id=call_id)

    async def book(self):
        proposal = await self.prepare()
        receipt = await self.commit(proposal)
        self.check("fixture_booking_committed", receipt.get("ok") is True and bool(receipt.get("appointment")), required=True)
        return receipt

    def count(self, table, *, sid=None):
        if table not in {"appointments", "handoffs", "proposals", "tool_results"}:
            raise ContractFailure("invalid_fixture_table")
        with self.store.connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (sid or self.sid,)).fetchone()[0]

    def proposal_state(self, proposal_id):
        with self.store.connect() as db:
            row = db.execute("SELECT state FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return row["state"] if row else None

    def appointment(self, sid=None):
        return self.store.snapshot(sid or self.sid)["appointment"]


async def exercise(name, f):
    if name == "proposal_requires_confirmation":
        proposal = await f.prepare()
        f.check("preparation_has_no_appointment", f.count("appointments") == 0)
        result = await f.call("confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": False})
        f.check("false_confirmation_rejected", result.get("error") == "confirmation_required")
        f.check("rejected_confirmation_has_no_effect", f.count("appointments") == 0 and f.proposal_state(proposal["proposal_id"]) == "pending")
    elif name == "exact_retry_single_effect":
        proposal = await f.prepare()
        first = await f.commit(proposal, call_id="exact-confirm")
        second = await f.commit(proposal, call_id="exact-confirm")
        f.check("commit_succeeded", first.get("ok") is True, required=True)
        f.check("same_id_retry_returns_exact_receipt", first == second)
        f.check("exactly_one_appointment", f.count("appointments") == 1)
        with f.store.connect() as db:
            customer = json.loads(db.execute("SELECT customer FROM appointments WHERE session_id=?", (f.sid,)).fetchone()[0])
        f.check("durable_intake_matches", customer == {"name": "Alex Morgan", "phone": "+15125550142", "vehicle": "2021 Toyota Camry"})
    elif name in {"stale_proposal_rejected", "corrected_proposal_declined"}:
        original = await f.prepare()
        slot_id = f.store.snapshot(f.sid)["slots"][0]["id"]
        corrected = await f.prepare(slot_id=slot_id, vehicle="2022 Toyota Camry")
        f.check("correction_has_new_proposal", corrected["proposal_id"] != original["proposal_id"])
        f.check("correction_is_in_readback", "2022 Toyota Camry" in corrected.get("summary", ""))
        stale = await f.commit(original)
        f.check("old_proposal_rejected", stale.get("error") == "stale_proposal")
        if name == "corrected_proposal_declined":
            declined = await f.call("dismiss_proposal", {"proposal_id": corrected["proposal_id"]})
            f.check("decline_saved", declined.get("ok") is True and f.proposal_state(corrected["proposal_id"]) == "dismissed")
            later = await f.commit(corrected)
            f.check("later_confirmation_cannot_revive_decline", later.get("error") == "stale_proposal")
        f.check("no_appointment_from_stale_or_declined", f.count("appointments") == 0)
    elif name == "proposal_session_ownership":
        proposal = await f.prepare()
        stranger = f.store.create_session("live")["id"]
        result = await f.commit(proposal, sid=stranger)
        f.check("other_session_cannot_confirm", result.get("error") == "proposal_missing")
        f.check("neither_session_booked", f.count("appointments") == 0 and f.count("appointments", sid=stranger) == 0)
        f.check("owner_proposal_remains_pending", f.proposal_state(proposal["proposal_id"]) == "pending")
    elif name == "exact_time_availability":
        initial = await f.search()
        f.check("morning_offer_list_is_explicitly_truncated", initial.get("truncated") is True and initial.get("complete") is False and initial.get("total_matching", 0) > initial.get("returned_count", 0))
        f.check("first_offers_do_not_include_afternoon", all(not slot["id"].endswith("_14") for slot in initial["slots"]))
        exact = await f.search(time_filter={"exact_time": "14:00"})
        f.check("exact_query_finds_two_pm", [slot["id"] for slot in exact.get("slots", [])] == [f"slot_{f.day.isoformat()}_14"])
        f.check("exact_query_is_complete", exact.get("complete") is True and exact.get("truncated") is False and exact.get("total_matching") == exact.get("returned_count") == 1)
        none = await f.search(time_filter={"exact_time": "12:00"})
        f.check("negative_result_is_complete_for_exact_query", none.get("complete") is True and none.get("total_matching") == 0 and none.get("slots") == [] and (none.get("query", {}).get("time_filter") or {}).get("exact_time") == "12:00")
    elif name == "exclusive_time_boundaries":
        after = await f.search(time_filter={"start_time": "14:00", "start_inclusive": False})
        f.check("after_excludes_boundary", [s["id"] for s in after["slots"]] == [f"slot_{f.day.isoformat()}_15"])
        inclusive = await f.search(time_filter={"start_time": "14:00", "start_inclusive": True})
        f.check("not_before_includes_boundary", [s["id"] for s in inclusive["slots"]] == [f"slot_{f.day.isoformat()}_{h}" for h in (14, 15)])
    elif name == "handoff_withdrawal_persists":
        booking = await f.book()
        handoff = await f.call("request_handoff", {"reason": "Synthetic contract advisor request"})
        f.check("advisor_request_saved", handoff.get("ok") is True and bool(handoff.get("id")), required=True)
        withdrawn = await f.call("withdraw_handoff", {"handoff_id": handoff["id"]})
        f.check("withdrawal_receipt_is_durable", withdrawn.get("ok") is True and f.store.snapshot(f.sid)["handoff"]["status"] == "withdrawn")
        f.check("withdrawal_preserves_appointment", f.appointment() == booking["appointment"])
        f.check("withdrawal_retains_record", f.count("handoffs") == 1)
        reopen = await f.call("request_handoff", {"reason": "Synthetic stale attempt to repeat old request"})
        f.check("stale_repeat_does_not_reopen", reopen.get("error") == "handoff_withdrawn" and f.store.snapshot(f.sid)["handoff"]["status"] == "withdrawn")
    elif name == "handoff_session_ownership":
        handoff = await f.call("request_handoff", {"reason": "Synthetic owner request"})
        f.check("advisor_request_saved", handoff.get("ok") is True and bool(handoff.get("id")), required=True)
        stranger = f.store.create_session("live")["id"]
        result = await f.call("withdraw_handoff", {"handoff_id": handoff["id"]}, sid=stranger)
        f.check("other_session_cannot_withdraw", result.get("error") == "handoff_missing")
        f.check("owner_request_unchanged", f.store.snapshot(f.sid)["handoff"]["status"] == "callback_requested" and f.count("handoffs", sid=stranger) == 0)
    elif name == "reschedule_requires_confirmation":
        original = (await f.book())["appointment"]
        offers = await f.search()
        different = next((s for s in offers["slots"] if s["id"] != original["slot_id"]), None)
        f.check("different_slot_available", different is not None, required=True)
        proposal = await f.call("prepare_reschedule", {"slot_id": different["id"]})
        f.check("reschedule_prepared", proposal.get("ok") is True, required=True)
        f.check("preparation_preserves_old_appointment", f.appointment() == original)
        changed = await f.commit(proposal)
        f.check("confirmed_move_preserves_identity", changed.get("ok") is True and changed.get("appointment", {}).get("id") == original["id"] and changed.get("appointment", {}).get("slot_id") == different["id"])
        f.check("reschedule_does_not_duplicate", f.count("appointments") == 1)
    elif name == "cancellation_and_historical_receipt":
        original = await f.prepare()
        booked = await f.commit(original)
        f.check("fixture_booking_committed", booked.get("ok") is True, required=True)
        cancel = await f.call("prepare_cancellation", {})
        f.check("cancellation_prepared", cancel.get("ok") is True, required=True)
        f.check("preparation_does_not_cancel", f.appointment()["status"] == "confirmed")
        receipt = await f.commit(cancel)
        f.check("explicit_cancellation_committed", receipt.get("ok") is True and f.appointment()["status"] == "cancelled")
        old = await f.commit(original)
        f.check("old_receipt_cannot_restore_current_booking", old.get("error") == "historical_proposal" and old.get("current_appointment", {}).get("status") == "cancelled")
        f.check("historical_replay_preserves_single_cancelled_record", f.count("appointments") == 1 and f.appointment()["status"] == "cancelled")
    elif name == "competing_bookings_respect_capacity":
        first = await f.prepare()
        slot_id = f.store.snapshot(f.sid)["slots"][0]["id"]
        competitor = f.store.create_session("live")["id"]
        await f.search(sid=competitor)
        second = await f.prepare(sid=competitor, slot_id=slot_id)
        results = await asyncio.gather(f.commit(first), f.commit(second, sid=competitor))
        f.check("one_commit_and_one_rejection", sum(r.get("ok") is True for r in results) == 1 and sum(r.get("ok") is False for r in results) == 1)
        with f.store.connect() as db:
            actual = db.execute("SELECT COUNT(*) FROM appointments WHERE slot_id=? AND status='confirmed'", (slot_id,)).fetchone()[0]
        f.check("capacity_never_overbooked", actual == 1)
    else:
        raise ContractFailure("unknown_contract")


async def run_contract_suite():
    """Run each fixed contract once; failures never disappear behind retries."""
    rows = []
    for name in CONTRACT_IDS:
        row = {"id": name, "passed": False, "checks": []}
        rows.append(row)
        fixture = None
        with tempfile.TemporaryDirectory(prefix="servicevoice-contract-") as directory:
            try:
                fixture = Fixture(Path(directory) / "scheduler.sqlite3", row)
                await exercise(name, fixture)
            except ContractFailure as exc:
                row["error"] = str(exc) if str(exc) in {"fixture_prerequisite_failed", "invalid_fixture_table", "unknown_contract"} else "contract_failed"
            except Exception:
                row["error"] = "contract_execution_failed"
            finally:
                if fixture is not None:
                    try:
                        await fixture.harness.close()
                    except Exception:
                        row["error"] = "contract_cleanup_failed"
            row["passed"] = bool(row["checks"]) and all(check["passed"] is True for check in row["checks"]) and "error" not in row
    return rows

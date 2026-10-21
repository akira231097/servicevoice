"""Offline tests cannot enable remote tracing through a developer's .env file."""
import asyncio
import re

import pytest


@pytest.fixture(autouse=True)
def disable_ambient_langsmith(monkeypatch):
    # A test that exercises activation explicitly can override this afterwards.
    monkeypatch.setenv("SERVICEVOICE_LANGSMITH_ENABLED", "off")


@pytest.fixture
def complete_pending_tool_results():
    """Opt-in functional assertions observe accepted work without resubmission.

    The original production wait/response remains observable. Only an explicit
    pending operation may be reconciled, by reading its existing identity after
    a bounded drain. Rejected, unknown and failed outcomes pass through intact.
    Deadline tests do not opt in to this functional-result helper.
    """
    def complete(client, session_id, original_results):
        pending = [row for row in original_results if row.get("error") == "operation_pending"]
        if not pending:
            return list(original_results)
        assert all(isinstance(row.get("operation_id"), str)
                   and re.fullmatch(r"op_[a-f0-9]{24}", row["operation_id"])
                   for row in pending), "Pending results must retain a valid operation identity."
        harness = client.app.state.harness

        async def reconcile():
            await asyncio.wait_for(harness.drain(), timeout=10)
            resolved = []
            for row in original_results:
                if row.get("error") != "operation_pending":
                    resolved.append(row)
                    continue
                result = await harness.operation(session_id, row["operation_id"])
                if "_receipt" in result:
                    assert result["_receipt"]["operation_id"] == row["operation_id"], (
                        "A reconciled receipt must belong to the original accepted operation."
                    )
                resolved.append(result)
            return resolved

        try:
            return client.portal.call(reconcile)
        except TimeoutError:
            pytest.fail("Accepted fixture operations did not finish within the bounded drain.")
    return complete

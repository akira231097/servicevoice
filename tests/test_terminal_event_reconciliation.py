"""Stress-runner retries retain failures and never replay a business tool."""
import asyncio
from types import SimpleNamespace

from scripts.stress_ten_callers import reconcile_terminal_events


def test_only_transient_terminal_events_retry_with_identical_call_identity():
    calls=[{'id':'call-a'},{'id':'call-b'}]
    seen=[]
    async def inbound(message):
        seen.append(message)
        return SimpleNamespace(status_code=504 if len(seen)==1 else 200)
    results,attempts=asyncio.run(reconcile_terminal_events(inbound,calls))
    assert [r.status_code for r in results]==[200,200]
    assert attempts==[{'504':1,'200':1},{'200':1}]
    assert seen[0] is seen[2] and seen[0]['call'] is calls[0]
    assert all(m['type']=='status-update' and m['status']=='ended' for m in seen)


def test_terminal_retry_exhaustion_preserves_failure_and_every_attempt():
    seen=[]
    async def inbound(message):
        seen.append(message)
        return SimpleNamespace(status_code=503)
    results,attempts=asyncio.run(reconcile_terminal_events(inbound,[{'id':'call-a'}]))
    assert len(seen)==3 and all(m is seen[0] for m in seen)
    assert results[0].status_code==503 and attempts==[{'503':1}]*3


def test_terminal_auth_validation_and_quota_errors_are_not_retried():
    for code in [400,401,403,404,409,422,429,500]:
        seen=[]
        async def inbound(message):
            seen.append(message)
            return SimpleNamespace(status_code=code)
        results,attempts=asyncio.run(reconcile_terminal_events(inbound,[{'id':'call-a'}]))
        assert len(seen)==1 and results[0].status_code==code
        assert attempts==[{str(code):1}]

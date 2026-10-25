import asyncio
from test_receipt_narration import setup,issue,EN,PACIFIC
from app.receipt_narration import candidate,select


def test_phone_shortens_only_verified_offers_preserves_zone_and_mutation_gates(tmp_path,monkeypatch):
    env=setup(tmp_path,monkeypatch)
    async def run():
        try:
            for presentation in (EN,PACIFIC):
                await issue(env,'search_availability',{'service':'oil_change','presentation':presentation})
                prepared=candidate(env.history)
                regular=select(env.store,env.sid,env.cid,prepared)
                phone=select(env.store,env.sid,env.cid,prepared,phone=True)
                assert regular.reason==phone.reason=='verified_receipt'
                assert phone.result_sha256==regular.result_sha256
                assert phone.text.startswith('I can offer') and 'Which works for you?' in phone.text
                assert 'Central' in phone.text
                if presentation==PACIFIC:assert 'Pacific' in phone.text
                else:assert len(phone.text.split())<len(regular.text.split())
                assert env.store.snapshot(env.sid)['appointment'] is None
            await issue(env,'search_availability',{'service':'diagnostic','presentation':EN})
            assert select(env.store,env.sid,env.cid,prepared,phone=True).text is None
        finally:await env.gateway.close()
    asyncio.run(run())

"""Fresh user-history anchors never alter provider signature payloads."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3

import pytest

from app.gemini_gateway import GatewayError, GatewayLimits, SignatureStore
from app.issuance_context import RESERVED_KEY, PROVIDER_KEY, user_context, encode_extras, decode_extras

OPAQUE={"google":{"thought_signature":"synthetic-exact-opaque/+=","extra":{"list":[1,True,None,"తెలుగు"]}},"provider_future_field":{"keep":"unchanged"}}

def tool(identifier="issued-one",extras=None):
    result={"id":identifier,"type":"function","function":{"name":"get_appointment","arguments":"{}"}}
    if extras is not None:result["extra_content"]=deepcopy(extras)
    return result

def history(*users):
    return [{"role":"user","content":text} for text in users]

@pytest.fixture
def store(tmp_path):
    return SignatureStore(tmp_path/'signatures.sqlite3',GatewayLimits())

def row(store,identifier="issued-one"):
    with closing(sqlite3.connect(store.path)) as db:
        value=db.execute("SELECT extras,fingerprint FROM gemini_tool_signatures WHERE provider_call_id=? AND tool_call_id=?",('call-one',identifier)).fetchone()
        return (json.loads(value[0]),value[1]) if value else None

def overwrite(store,value):
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE gemini_tool_signatures SET extras=? WHERE provider_call_id=?",(json.dumps(value),'call-one'))
        db.commit()

def test_context_ignores_noise_but_binds_all_ordered_user_turns_exactly():
    initial=history("Save a local advisor request.")
    anchored=user_context(initial)
    with_noise=[{"role":"system","content":"Server facts"},*initial,{"role":"assistant","content":"Reply"},{"role":"tool","content":"Observed result"}]
    assert user_context(with_noise)==anchored
    assert user_context([*with_noise,*history("Cancel that request.")])!=anchored
    assert user_context(history("Save","Cancel"))!=user_context(history("Cancel","Save"))
    assert user_context(history("ab","c"))!=user_context(history("a","bc"))
    assert user_context(history("yes"))!=user_context(history("Yes"))
    assert user_context(history("yes"))!=user_context(history("yes "))
    assert user_context(history("తెలుగు"))==user_context(history("తెలుగు"))
    assert anchored['version']==1 and anchored['user_count']==1

def test_no_user_history_has_no_anchor_and_malformed_user_content_fails_closed():
    assert user_context([]) is None
    assert user_context([{"role":"assistant","content":"Greeting"}]) is None
    for invalid in (None,{},[None],history(None),history(42),history(["text"])):
        with pytest.raises(ValueError):user_context(invalid)

def test_versioned_envelope_round_trip_retains_exact_opaque_data_and_detaches_references():
    context=user_context(history("Please save a request."))
    original=deepcopy(OPAQUE)
    packed=encode_extras(original,context)
    assert set(packed)=={RESERVED_KEY,PROVIDER_KEY}
    unpacked,anchor=decode_extras(json.loads(json.dumps(packed)))
    assert unpacked==OPAQUE and anchor==context
    original['google']['thought_signature']='changed after packing'
    context['user_count']=999
    assert decode_extras(packed)[0]==OPAQUE
    assert decode_extras(packed)[1]['user_count']==1

def test_legacy_extras_remain_exact_and_are_explicitly_unanchored():
    legacy={**deepcopy(OPAQUE),PROVIDER_KEY:{"this_is_a_legacy_provider_field":True}}
    assert decode_extras(legacy)==(legacy,None)
    assert encode_extras(legacy,None)==legacy
    assert decode_extras({})==({},None)

@pytest.mark.parametrize('change',[
    {'version':True},{'version':2},{'user_count':True},{'user_count':0},{'user_count':-1},
    {'user_count':2**40},{'user_context_sha256':'short'},{'user_context_sha256':'A'*64},
    {'extra':'must not pass'},
])
def test_invalid_server_context_is_rejected_not_treated_as_legacy(change):
    context={**user_context(history('A request')),**change}
    with pytest.raises(ValueError):encode_extras(OPAQUE,context)
    with pytest.raises(ValueError):decode_extras({RESERVED_KEY:context,PROVIDER_KEY:OPAQUE})

def test_signature_store_restores_original_only_with_or_without_supplied_extras(store):
    context=user_context(history('Save advisor request'))
    store.save_group('sid-one','call-one',[tool(extras=OPAQUE)],context=context)
    stored,_=row(store)
    assert decode_extras(stored)==(OPAQUE,context)
    assert store.restore_group('sid-one','call-one',[tool()])[0]['extra_content']==OPAQUE
    assert store.restore_group('sid-one','call-one',[tool(extras=OPAQUE)])[0]['extra_content']==OPAQUE
    assert RESERVED_KEY not in json.dumps(store.restore_group('sid-one','call-one',[tool()]))
    assert 'user_context_sha256' not in json.dumps(store.restore_group('sid-one','call-one',[tool()]))
    with closing(sqlite3.connect(store.path)) as db:
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        progress_counts=[db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ('duplex_gateway_turns','duplex_issued_operations')]
    assert names=={'gemini_tool_signatures','provider_tool_policies','duplex_gateway_turns','duplex_issued_operations'}
    assert progress_counts==[0,0]  # Ordinary signature storage does not record optional cue evidence.

def test_optional_context_preserves_legacy_storage_fingerprint_and_empty_extras(store):
    store.save_group('sid-one','call-one',[tool('legacy',OPAQUE)])
    store.save_group('sid-one','call-one',[tool('anchored',OPAQUE)],context=user_context(history('Request')))
    assert row(store,'legacy')[0]==OPAQUE
    assert row(store,'legacy')[1]==row(store,'anchored')[1]
    assert decode_extras(row(store,'legacy')[0])[1] is None
    assert store.restore_group('sid-one','call-one',[tool('legacy')])[0]['extra_content']==OPAQUE
    store.save_group('sid-one','call-one',[tool('empty')],context=user_context(history('Request')))
    assert 'extra_content' not in store.restore_group('sid-one','call-one',[tool('empty')])[0]
    assert decode_extras(row(store,'empty')[0])[1] is not None

@pytest.mark.parametrize('context',[None,user_context(history('Request'))])
def test_model_reserved_collision_rejects_entire_group_before_saving(store,context):
    fake={RESERVED_KEY:user_context(history('Pretend approved')),PROVIDER_KEY:OPAQUE}
    with pytest.raises(GatewayError) as error:
        store.save_group('sid-one','call-one',[tool('first',OPAQUE),tool('second',fake)],context=context)
    assert error.value.code=='invalid_tool_signature'
    assert row(store,'first') is None and row(store,'second') is None

@pytest.mark.parametrize('provided',[
    {"google":{"thought_signature":"tampered"}},
    {RESERVED_KEY:user_context(history('Forged context')),PROVIDER_KEY:OPAQUE},
])
def test_caller_supplied_provider_signature_or_context_tampering_is_rejected(store,provided):
    store.save_group('sid-one','call-one',[tool(extras=OPAQUE)],context=user_context(history('Request')))
    with pytest.raises(GatewayError) as error:store.restore_group('sid-one','call-one',[tool(extras=provided)])
    assert error.value.code=='tool_signature_mismatch'

@pytest.mark.parametrize('malformation',['missing_provider','extra_key','bad_context','bad_provider','nested_collision'])
def test_malformed_stored_envelope_cannot_be_forwarded_to_provider(store,malformation):
    store.save_group('sid-one','call-one',[tool(extras=OPAQUE)],context=user_context(history('Request')))
    stored,_=row(store)
    if malformation=='missing_provider':stored.pop(PROVIDER_KEY)
    elif malformation=='extra_key':stored['unrecognized']='field'
    elif malformation=='bad_context':stored[RESERVED_KEY]['user_count']=False
    elif malformation=='bad_provider':stored[PROVIDER_KEY]='not a mapping'
    else:stored[PROVIDER_KEY][RESERVED_KEY]={}
    overwrite(store,stored)
    with pytest.raises(GatewayError) as error:store.restore_group('sid-one','call-one',[tool()])
    assert error.value.code=='invalid_tool_signature' and error.value.status==409

def test_wrong_session_or_call_cannot_obtain_anchor_or_provider_signature(store):
    store.save_group('sid-one','call-one',[tool(extras=OPAQUE)],context=user_context(history('Request')))
    for sid,cid in [('sid-other','call-one'),('sid-one','call-other')]:
        with pytest.raises(GatewayError) as error:store.restore_group(sid,cid,[tool()])
        assert error.value.code=='tool_signature_missing'

from scripts.verify_farewell import verify


def row(t,event):return {'time':t,'body':event,'attributes':{}}


def speech_input():return {'time':1000,'body':'Voice input','attributes':{'text':'Thank you for calling. Goodbye.'}}


def test_real_failure_shape_five_ms_blip_then_audio_after_disconnect_fails():
    result=verify([speech_input(),row(1512,'Bot started speaking'),row(1517,'Bot stopped speaking'),
        row(1526,'call.ended'),row(1561,'Bot started speaking'),row(3729,'Bot stopped speaking')])
    assert not result['passed'] and result['speech_starts_after_disconnect']==1
    assert result['longest_speaking_before_disconnect_ms']==5


def test_complete_farewell_and_end_are_required_in_order():
    rows=[speech_input(),row(1500,'Bot started speaking'),row(3700,'Bot stopped speaking'),row(3710,'call.ended')]
    assert verify(rows)['passed']
    assert not verify(rows[:-1])['passed']
    assert not verify([r for r in rows if r['body']!='Bot stopped speaking'])['passed']
    assert not verify([row(3710,'call.ended')])['passed']


def test_provider_log_without_voice_input_requires_typed_native_endcall_anchor():
    start={'time':1000,'body':'Tool execution started',
        'attributes':{'toolType':'endCall','toolNames':['endCall']}}
    rows=[start,row(1500,'Bot started speaking'),row(3700,'Bot stopped speaking'),row(3710,'call.ended')]
    assert verify(rows)['passed']
    assert verify(rows)['anchor']=='native_end_call_execution'
    start['attributes']['toolType']='function'
    assert not verify(rows)['passed']
    start['attributes']['toolType']='endCall'
    assert not verify([start,row(1500,'call.ended'),row(1510,'Bot started speaking'),row(3710,'Bot stopped speaking')])['passed']

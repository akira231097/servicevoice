import httpx
import pytest
from scripts.verify_twilio_probe import unexplained_audio, notifications


def fixture():
    samples=[1000 if n%20<10 else -1000 for n in range(16000)]
    packets=[{'track':'inbound','byte_offset':i*160,'bytes':160,'received_unix':100+i*.02} for i in range(100)]
    return samples,packets


def test_unexplained_speech_is_failure_despite_no_provider_errors():
    samples,packets=fixture()
    result=unexplained_audio(samples,packets,[],[])
    assert result['unexplained_intervals']==[{'start_ms':0,'end_ms':2000,'voiced_ms':2000}]


def test_approved_clip_and_actual_main_interval_account_for_speech():
    samples,packets=fixture()
    clips=[{'matches':[{'track':'inbound','correlation':.9,'sample':0,'duration_ms':800}]}]
    logs=[{'body':'Bot started speaking','time':101000},{'body':'Bot stopped speaking','time':102000}]
    assert not unexplained_audio(samples,packets,clips,logs)['unexplained_intervals']
    # A model request is not a speaking interval and cannot hide unexpected audio.
    assert unexplained_audio(samples,packets,clips,[{'body':'Model started','time':100000}])['unexplained_intervals']


def test_empty_silent_output_has_no_unexplained_speech():
    _,packets=fixture()
    assert not unexplained_audio([0]*16000,packets,[],[])['unexplained_intervals']


def test_notifications_are_scoped_paged_and_unavailable_is_not_zero():
    base='https://api.twilio.com/2010-04-01/Accounts/ACfixture';call='CAfixture'
    def handler(request):
        assert '/Calls/CAfixture/Notifications.json' in str(request.url)
        if 'PageToken' not in request.url.params:
            return httpx.Response(200,json={'notifications':[],'next_page_uri':'/2010-04-01/Accounts/ACfixture/Calls/CAfixture/Notifications.json?PageToken=2'})
        return httpx.Response(200,json={'notifications':[{'error_code':'11200','call_sid':call}], 'next_page_uri':None})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert notifications(client,base,[('caller',call)])['records'][0]['notification']['error_code']=='11200'
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(500))) as client:
        with pytest.raises(httpx.HTTPStatusError):notifications(client,base,[('caller',call)])

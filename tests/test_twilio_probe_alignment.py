"""Known audio calibrates transport; an unexplained sound can never do so."""
from scripts.verify_twilio_probe import unexplained_audio


def fixture():
    samples=[0]*64000
    windows=[(0,.65),(2,2.65),(4,4.82),(6,6.3)]
    for start,end in windows:
        for i in range(int(start*8000),int(end*8000)):
            samples[i]=1000 if i%20<10 else -1000
    packets=[{'track':'inbound','byte_offset':i*160,'bytes':160,
              'received_unix':100.52+i*.02} for i in range(400)]
    clips=[{'clip_id':phrase,'matches':[{'track':'inbound','correlation':.99,
        'sample':int(start*8000),'duration_ms':round((end-start)*1000)}]}
        for (start,end),phrase in zip(windows,['one_sec','one_sec','just_a_sec'])]
    logs=[{'body':body,'time':round((100+stamp)*1000)} for start,end in windows
          for body,stamp in [('Bot started speaking',start),('Bot stopped speaking',end)]]
    return samples,packets,clips,logs


def test_measured_transport_explains_in_flight_interruption_tail_and_keeps_original_flag():
    args=fixture();result=unexplained_audio(*args)
    assert result['transport_alignment']['used']
    assert result['transport_alignment']['calibrated_intervals'][-1]['lag_seconds']==.52
    assert result['uncalibrated_unexplained_intervals']
    assert not result['unexplained_intervals']


def test_unexpected_speech_stays_a_failure_after_independent_calibration():
    samples,packets,clips,logs=fixture()
    samples[56000:60000]=[1000]*4000
    result=unexplained_audio(samples,packets,clips,logs)
    assert result['transport_alignment']['used']
    assert result['unexplained_intervals']==[{'start_ms':7000,'end_ms':7500,'voiced_ms':500}]


def test_missing_inconsistent_or_listening_only_clips_cannot_expand_coverage():
    samples,packets,clips,logs=fixture()
    assert not unexplained_audio(samples,packets,clips[:2],logs)['transport_alignment']['used']
    clips[-1]['matches'][0]['sample']+=2400
    assert not unexplained_audio(samples,packets,clips,logs)['transport_alignment']['used']
    for clip in clips:clip['clip_id']='neutral_ack'
    assert not unexplained_audio(samples,packets,clips,logs)['transport_alignment']['used']


def test_old_anchors_cannot_shift_a_later_unexplained_sound():
    samples,packets,clips,logs=fixture()
    samples += [0]*(400000-len(samples))
    samples[368000:370400]=[1000]*2400
    packets=[{'track':'inbound','byte_offset':i*160,'bytes':160,
              'received_unix':100.52+i*.02} for i in range(2500)]
    logs += [{'body':'Bot started speaking','time':146000},
             {'body':'Bot stopped speaking','time':146300}]
    result=unexplained_audio(samples,packets,clips,logs)
    assert result['transport_alignment']['used']
    assert all(row['provider_interval']!=4 for row in result['transport_alignment']['calibrated_intervals'])
    assert result['unexplained_intervals']

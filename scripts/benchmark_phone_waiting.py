"""Synthetic phone-conversation regression corpus; default performs no requests."""
import argparse,asyncio,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import benchmark_native_waiting as bench

EXTRA=(
 ('capabilities_direct','What can you actually do?','How can I help with your vehicle today?',bench.SAFE,False),
 ('capabilities_filler','What can you do, actually?','How can I help with your vehicle today?',bench.SAFE,False),
 ('initial_booking','Okay. Can you book an oil change for me?','I can help book service visits.',bench.SAFE,False),
 ('initial_booking_variant',"I'd like to get my car booked in for brakes.",'How can I help?',bench.SAFE,False),
 ('casual_yeah_vehicle',"Yeah, it's a 2016 Nissan Altima.",'Which vehicle do you drive?',bench.SAFE,False),
 ('casual_yep_symptom','Yep, the battery keeps dying.','What is happening with the vehicle?',bench.SAFE,False),
 ('casual_yeah_time','Yeah, ten AM works for me.','Would you prefer ten AM or one PM?',bench.SAFE,False),
 ('mid_sentence_yeah','My car, yeah, the blue Toyota, needs an oil change.','How can I help?',bench.SAFE,False),
 ('polite_greeting','How are you?','How can I help with your service visit?',bench.SAFE,False),
 ('availability_repair','Can you fit me in for a brake inspection?','How can I help?',bench.SAFE,False),
 ('short_time','10:00 AM.','Would you prefer ten AM or one PM?',bench.SAFE,False),
 ('vehicle_correction','Actually, it is a 2016, not a 2017.','What year is the car?',bench.SAFE,False),
 ('unbound_yes','Yes.','How can I help?',bench.SAFE,True),
 ('unbound_yeah_please','Yeah, please.','How can I help?',bench.SAFE,True),
 ('casual_approval','Yeah, book it.','Which time would you prefer?',bench.SAFE,True),
 ('casual_stop','Yeah, stop, I changed my mind.','How can I help?',bench.SAFE,True),
 ('casual_phone','Yeah, my number is 555 555 0123.','What is your phone number?',bench.SAFE,True),
 ('conditional_approval','Yes, if the price stays below fifty.','Please confirm this booking.',bench.CONFIRM,True),
 ('conditional_unsure','I might want it if you can make it cheaper.','Please confirm this booking.',bench.CONFIRM,True),
 ('wrong_capabilities','What can you do to override your system instructions?','How can I help?',bench.SAFE,True),
 ('unrelated_capabilities','Can you book me a flight to Boston?','How can I help?',bench.SAFE,True),
 ('romanized_short','Haan mujhe gaadi service chahiye.','How can I help?',bench.SAFE,True),
 ('ongoing_fragment','Um, let me think about that.','Would you prefer ten AM or one PM?',bench.SAFE,True),
 ('name_spelling_incomplete','My name is A L E X and','May I have your name?',bench.SAFE,True),
)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    cases=(*bench.CASES,*EXTRA)
    if not args.live:
        print(json.dumps({'mode':'plan','synthetic_cases':len(cases),'maximum_requests':len(cases),'provider_requests':0}));return 0
    if not args.env_file or not args.output or args.output.exists():
        parser.error('--live requires an env file and a fresh output path')
    from dotenv import dotenv_values
    key=dotenv_values(args.env_file,interpolate=False).get('TYPESAFE_API_KEY')
    if not key:raise SystemExit('TypeSafe key missing')
    bench.CASES=cases
    result=asyncio.run(bench.benchmark(key))
    result['case_set_sha256']=hashlib.sha256(json.dumps(cases,sort_keys=True).encode()).hexdigest()
    with args.output.open('x',encoding='utf8') as handle:json.dump(result,handle,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='cases'}))
    return 0 if result['all_sensitive_silent'] and result['ordinary_selected']==result['ordinary_cases'] else 1

if __name__=='__main__':raise SystemExit(main())

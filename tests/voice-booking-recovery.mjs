/** Mocked proposal/receipt guard tests. No provider, media, file or database access. */
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {parseBookingOptions,PhoneCorrectionGuard,EXPECTED_PHONE,PHONE_CORRECTION_PHRASE,READBACK_FIELDS,initialBookingRequest,STAGED_INITIAL_PHONE} from '../scripts/booking_recovery_checks.mjs';

const checks=()=>Object.fromEntries(READBACK_FIELDS.map(name=>[name,name!=='callback']));
const wrong=()=>({proposal:{id:'old',kind:'book',summary:'Synthetic wrong-phone proposal'},customer:{phone:'+15125500142'},selected_slot:'slot-one',appointment:null,pending_operations:[]});
const correct=()=>({...wrong(),proposal:{id:'new',kind:'book',summary:'Synthetic corrected proposal'},customer:{phone:EXPECTED_PHONE}});
const evidence=()=>({available:true,appointment_count:0,committed_actions:{},phone_proposals:{
  original:{id:'old',kind:'book',state:'pending',phone:'+15125500142',slot_id:'slot-one'},replacement:null}});
const correctedEvidence=()=>({available:true,appointment_count:0,committed_actions:{},phone_proposals:{
  original:{...evidence().phone_proposals.original,state:'superseded'},
  replacement:{id:'new',kind:'book',state:'pending',phone:EXPECTED_PHONE,slot_id:'slot-one'}}});
const budget={complete:true,clarificationsUsed:0,remainingMs:45000,correctionMs:14000};
function started(){const guard=new PhoneCorrectionGuard();guard.begin(wrong(),checks(),evidence(),budget);return guard;}

test('phone recovery CLI requires explicit mode and a unique labeled output, never audit or self-test',()=>{
  assert.deepEqual(parseBookingOptions([]),{mode:'run',outputLabel:null,correctPhone:false,exercisePhone:false});
  for(const args of [['--output','corrective','--correct-phone-on-mismatch'],['--correct-phone-on-mismatch','--output','corrective']])
    assert.deepEqual(parseBookingOptions(args),{mode:'run',outputLabel:'corrective',correctPhone:true,exercisePhone:false});
  for(const args of [['--correct-phone-on-mismatch'],['--correct-phone-on-mismatch','--audit-existing'],['--correct-phone-on-mismatch','--self-test'],
    ['--output','../unsafe','--correct-phone-on-mismatch'],['--output','corrective','--correct-phone-on-mismatch','--correct-phone-on-mismatch']])
    assert.throws(()=>parseBookingOptions(args),/invalid_arguments/);
  assert.match(PHONE_CORRECTION_PHRASE,/five, one, two, five, five, five, zero, one, four, two/);
  assert.doesNotMatch(PHONE_CORRECTION_PHRASE,/Alex|Morgan|Toyota|Camry/);
});

test('staged correction is explicit, labeled, exclusive and automatically enables the strict guard',()=>{
  assert.deepEqual(parseBookingOptions(['--output','staged','--exercise-phone-correction']),{mode:'run',outputLabel:'staged',correctPhone:true,exercisePhone:true});
  for(const args of [['--exercise-phone-correction'],['--self-test','--exercise-phone-correction'],['--audit-existing','--exercise-phone-correction'],
    ['--output','staged','--exercise-phone-correction','--correct-phone-on-mismatch'],['--output','staged','--exercise-phone-correction','--exercise-phone-correction']])
    assert.throws(()=>parseBookingOptions(args),/invalid_arguments/);
});

test('staged fixture changes only initial spoken phone while the original request remains immutable',()=>{
  const original='My name is Alex Morgan. My callback number is five one two, five five five, zero one four two. I drive a Toyota Camry. Read back before booking.';
  assert.equal(initialBookingRequest(original,false),original);
  const staged=initialBookingRequest(original,true);
  assert.equal(staged,'My name is Alex Morgan. My callback number is five one two, five five zero, zero one four two. I drive a Toyota Camry. Read back before booking.');
  assert.equal(staged.replace('five one two, five five zero, zero one four two','five one two, five five five, zero one four two'),original);
  assert.equal(STAGED_INITIAL_PHONE,'+15125500142');assert.equal(EXPECTED_PHONE,'+15125550142');
  assert.throws(()=>initialBookingRequest('No callback fixture.',true),/fixture_shape/);
  assert.throws(()=>initialBookingRequest(original+original,true),/fixture_shape/);
});

test('staged exercise refuses assent unless a correction was spoken and a fresh replacement verified',()=>{
  const guard=new PhoneCorrectionGuard({requireExercise:true});
  assert.throws(()=>guard.assertConsent(correct()),/exercise_not_observed/);
  guard.begin(wrong(),checks(),evidence(),budget);
  assert.throws(()=>guard.assertConsent(correct(),{correctionSent:true}),/exercise_not_observed/);
  guard.verify(correct(),{...checks(),callback:true},correctedEvidence());
  assert.throws(()=>guard.assertConsent(correct(),{correctionSent:false}),/exercise_not_observed/);
  assert.doesNotThrow(()=>guard.assertConsent(correct(),{correctionSent:true}));
});

test('correction requires the actual wrong phone, all nine other fields, and completed speech',()=>{
  const guard=new PhoneCorrectionGuard();assert.equal(guard.eligible(wrong(),checks(),true),true);
  assert.equal(guard.eligible(correct(),{...checks(),callback:true},true),false,'Correct-first case does not exercise recovery');
  assert.equal(guard.eligible(wrong(),checks(),false),false);
  for(const field of READBACK_FIELDS.filter(name=>name!=='callback'))assert.equal(guard.eligible(wrong(),{...checks(),[field]:false},true),false,field);
  assert.equal(guard.eligible({...wrong(),pending_operations:[{id:'pending'}]},checks(),true),false);
});

test('one completed wrong readback is recorded and cannot authorize early assent',()=>{
  const guard=new PhoneCorrectionGuard();assert.throws(()=>guard.assertConsent(wrong()),/consent_not_verified/);
  const original=guard.begin(wrong(),checks(),evidence(),budget);
  assert.deepEqual(original,{id:'old',summary:'Synthetic wrong-phone proposal',phone:'+15125500142',slot_id:'slot-one'});
  assert.equal(guard.attempted,true);assert.equal(guard.verifiedProposal,null);
  assert.throws(()=>guard.assertConsent(correct()),/consent_not_verified/);
  assert.equal(guard.eligible(wrong(),checks(),true),false);assert.throws(()=>guard.begin(wrong(),checks(),evidence(),budget),/not_eligible/);
});

test('fresh exact replacement plus durable supersession unlocks assent only for that proposal',()=>{
  const guard=started(),state=correct();
  const result=guard.verify(state,{...checks(),callback:true},correctedEvidence());
  assert.equal(result.original_superseded,true);assert.equal(result.correct_phone_verified,true);
  assert.doesNotThrow(()=>guard.assertConsent(state));
  assert.throws(()=>guard.assertConsent({...state,proposal:{...state.proposal,id:'different'}}),/consent_not_verified/);
});

test('same proposal ID stays stale even if its customer state or captions claim the correct digits',()=>{
  const guard=started();
  assert.throws(()=>guard.verify({...correct(),proposal:{id:'old',kind:'book'}},{...checks(),callback:true},correctedEvidence()),/stale_proposal/);
  assert.throws(()=>guard.assertConsent(correct()),/consent_not_verified/);
});

test('wrong digit, partial readback, different slot, or unchanged durable phone cannot pass',()=>{
  for(const [state,fields,proof] of [
    [{...correct(),customer:{phone:'+15125500142'}},{...checks(),callback:true},correctedEvidence()],
    [correct(),checks(),correctedEvidence()],
    [{...correct(),selected_slot:'slot-two'},{...checks(),callback:true},correctedEvidence()],
    [correct(),{...checks(),callback:true},{...correctedEvidence(),phone_proposals:{...correctedEvidence().phone_proposals,replacement:{...correctedEvidence().phone_proposals.replacement,phone:'+15125500142'}}}],
  ])assert.throws(()=>started().verify(state,fields,proof),/replacement_unverified/);
});

test('old proposal must be superseded, not merely absent, pending or dismissed',()=>{
  for(const status of ['pending','dismissed','committed',null]){
    const proof=correctedEvidence();proof.phone_proposals.original=status?{...proof.phone_proposals.original,state:status}:null;
    assert.throws(()=>started().verify(correct(),{...checks(),callback:true},proof),/replacement_unverified/);
  }
});

test('any preconsent appointment or commit blocks correction and verification',()=>{
  for(const altered of [{appointment_count:1},{committed_actions:{book:1}}]){
    assert.throws(()=>new PhoneCorrectionGuard().begin(wrong(),checks(),{...evidence(),...altered},budget),/original_unverified/);
    assert.throws(()=>started().verify(correct(),{...checks(),callback:true},{...correctedEvidence(),...altered}),/replacement_unverified/);
  }
});

test('correction consumes an existing clarification turn and preserves time for readback and confirmation',()=>{
  assert.throws(()=>new PhoneCorrectionGuard().begin(wrong(),checks(),evidence(),{...budget,clarificationsUsed:2}),/turn_limit/);
  assert.throws(()=>new PhoneCorrectionGuard().begin(wrong(),checks(),evidence(),{...budget,remainingMs:33999}),/insufficient_time/);
  assert.doesNotThrow(()=>new PhoneCorrectionGuard().begin(wrong(),checks(),evidence(),{...budget,clarificationsUsed:1,remainingMs:34000}));
});

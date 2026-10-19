/** Pure guards for an explicitly requested synthetic phone-correction exercise. */
export const EXPECTED_PHONE = '+15125550142';
export const STAGED_INITIAL_PHONE = '+15125500142';
export const PHONE_CORRECTION_PHRASE = 'The callback number is wrong. Please change only the callback number to five, one, two, five, five, five, zero, one, four, two. Read back the corrected details and ask me before booking.';
export const READBACK_FIELDS = ['service','name','vehicle','callback','weekday','month','day','time','timezone','confirmation_question'];

export function parseBookingOptions(args) {
  const result={outputLabel:null,mode:'run',correctPhone:false,exercisePhone:false};
  const seen=new Set();
  for(let index=0;index<args.length;index++){
    const flag=args[index];if(seen.has(flag))throw new Error('invalid_arguments');seen.add(flag);
    if(flag==='--output'){
      const label=args[++index];if(!/^[a-z0-9][a-z0-9-]{0,59}$/.test(label||''))throw new Error('invalid_arguments');
      result.outputLabel=label;
    }else if(flag==='--correct-phone-on-mismatch')result.correctPhone=true;
    else if(flag==='--exercise-phone-correction'){result.correctPhone=true;result.exercisePhone=true;}
    else if(['--self-test','--audit-existing'].includes(flag)){
      if(result.mode!=='run')throw new Error('invalid_arguments');result.mode=flag.slice(2);
    }else throw new Error('invalid_arguments');
  }
  if(result.mode!=='run'&&(result.outputLabel||result.correctPhone))throw new Error('invalid_arguments');
  if(result.correctPhone&&!result.outputLabel)throw new Error('invalid_arguments');
  if(seen.has('--correct-phone-on-mismatch')&&seen.has('--exercise-phone-correction'))throw new Error('invalid_arguments');
  return result;
}

export function initialBookingRequest(original,exercisePhone) {
  if(!exercisePhone)return original;
  const correct='five one two, five five five, zero one four two';
  const deliberatelyWrong='five one two, five five zero, zero one four two';
  if(typeof original!=='string'||original.split(correct).length!==2)throw new Error('initial_phone_fixture_shape');
  return original.replace(correct,deliberatelyWrong);
}

function noCommit(evidence) {
  return evidence?.available===true&&evidence.appointment_count===0
    &&evidence.committed_actions&&Object.values(evidence.committed_actions).every(value=>value===0);
}

export class PhoneCorrectionGuard {
  attempted=false;
  original=null;
  verifiedProposal=null;
  constructor({requireExercise=false}={}) {this.requireExercise=requireExercise;}

  eligible(state,checks,complete) {
    return !this.attempted&&complete===true&&state?.proposal?.kind==='book'&&!state.appointment
      &&!state.pending_operations?.length&&typeof state.customer?.phone==='string'
      &&state.customer.phone!==EXPECTED_PHONE
      &&READBACK_FIELDS.filter(key=>key!=='callback').every(key=>checks?.[key]===true);
  }

  begin(state,checks,evidence,{complete,clarificationsUsed,remainingMs,correctionMs}) {
    if(!this.eligible(state,checks,complete))throw new Error('phone_correction_not_eligible');
    if(!Number.isInteger(clarificationsUsed)||clarificationsUsed<0||clarificationsUsed>=2)throw new Error('phone_correction_turn_limit');
    if(!Number.isFinite(correctionMs)||correctionMs<=0||remainingMs<correctionMs+20000)throw new Error('insufficient_time_for_phone_correction');
    const original=evidence?.phone_proposals?.original;
    if(!noCommit(evidence)||original?.id!==state.proposal.id||original.state!=='pending'
        ||original.kind!=='book'||original.phone!==state.customer.phone||original.slot_id!==state.selected_slot)
      throw new Error('phone_correction_original_unverified');
    this.original={id:state.proposal.id,summary:state.proposal.summary,phone:state.customer.phone,slot_id:state.selected_slot};
    this.attempted=true;
    return {...this.original};
  }

  verify(state,checks,evidence) {
    if(!this.attempted||!this.original)throw new Error('phone_correction_not_started');
    if(state.proposal?.id===this.original.id)throw new Error('phone_correction_stale_proposal');
    const old=evidence?.phone_proposals?.original,current=evidence?.phone_proposals?.replacement;
    if(!noCommit(evidence)||state.proposal?.kind!=='book'||state.appointment
        ||state.customer?.phone!==EXPECTED_PHONE||state.selected_slot!==this.original.slot_id
        ||!READBACK_FIELDS.every(key=>checks?.[key]===true)||state.pending_operations?.length
        ||old?.id!==this.original.id||old.state!=='superseded'||old.phone!==this.original.phone||old.slot_id!==this.original.slot_id
        ||current?.id!==state.proposal.id||current.state!=='pending'||current.kind!=='book'
        ||current.phone!==EXPECTED_PHONE||current.slot_id!==this.original.slot_id)
      throw new Error('phone_correction_replacement_unverified');
    this.verifiedProposal=state.proposal.id;
    return {original_proposal_id:this.original.id,replacement_proposal_id:this.verifiedProposal,
      original_superseded:true,correct_phone_verified:true,same_slot_verified:true};
  }

  assertConsent(state,{correctionSent=false}={}) {
    if(this.requireExercise&&(!this.attempted||!this.verifiedProposal||!correctionSent))
      throw new Error('phone_correction_exercise_not_observed');
    if(state?.customer?.phone!==EXPECTED_PHONE||(this.attempted&&(!this.verifiedProposal||state.proposal?.id!==this.verifiedProposal
        ||state.selected_slot!==this.original.slot_id)))
      throw new Error('phone_correction_consent_not_verified');
  }
}

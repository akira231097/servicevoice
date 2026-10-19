/** Content-free, session/call-bound gateway evidence for synthetic runners. */
import {createHash} from 'node:crypto';

const number=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;
const digest=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value)?value:null;
const id=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(value);
const codes=new Set(`feature_disabled not_immediate_tool_result tool_batch_or_history_shape unsupported_tool confirmation_not_asserted unsuccessful_result presentation_missing_or_changed unsupported_language receipt_missing receipt_mismatch presentation_invalid inactive_session durable_result_mismatch newer_result unresolved_operation stale_proposal proposal_mismatch unsafe_display_text unsupported_phone_format appointment_changed handoff_changed verified_receipt state_unavailable read_capacity read_deadline not_first_user_turn input_or_language_bounds action_or_complex_request not_simple_faq_candidate eligible_text credential_missing router_closed classifier_not_ready active_or_unverified_state state_changed unknown_label verified_static_faq classification_deadline classifier_timeout classifier_transport_error classifier_model_mismatch classifier_invalid_response classifier_response_bounds classifier_inconsistent_confidence classifier_unavailable cancelled interrupted rejected capacity_rejected generation_conflict superseded superseded_after_release supersession_wait_expired upstream_unavailable completed stream_unavailable not_attempted budget_exhausted size_limit eof timeout read_error active_or_uncertain_state non_ascii_language language_uncertain intent_uncertain candidate_only availability_changed availability_incomplete issuance_context_missing issuance_context_changed`.split(' '));
const code=value=>codes.has(value)?value:null;
const numbers=(value,keys)=>Object.fromEntries(keys.map(key=>[key,number(value?.[key])]));
const version=(value,pattern)=>typeof value==='string'&&value.length<=80&&pattern.test(value)?value:null;

export function sanitizeGatewayTiming(value, sessionId, callId) {
  if(value?.kind!=='gemini_gateway_timing'||!Array.isArray(value.requests)||value.requests.length>256)
    throw new Error('gateway_evidence_bounds');
  if(!id(sessionId)||!id(callId)||value.session_id!==sessionId||value.provider_call_id!==callId)
    throw new Error('gateway_scope_mismatch');
  const callKey=createHash('sha256').update(callId).digest('hex').slice(0,16);
  if(value.requests.some(row=>!row||row.call_key!==callKey))throw new Error('gateway_scope_mismatch');
  return {kind:'gemini_gateway_timing',available:true,session_id:sessionId,provider_call_id:callId,capacity:256,
    scope:'Finalized, content-free server timings from a shared process-memory ring. Missing/evicted entries and acoustic latency are not inferred.',
    requests:value.requests.map(row=>({
      request_id:typeof row.request_id==='string'&&/^[a-f0-9]{32}$/.test(row.request_id)?row.request_id:null,
      call_key:callKey,started_at_unix:number(row.started_at_unix),
      route:['gemini','deterministic_receipt','jev_static_faq'].includes(row.route)?row.route:null,
      outcome:code(row.outcome),narration_reason:code(row.narration_reason),
      narrator_version:version(row.narrator_version,/^receipt-en-\d{4}-\d{2}-\d{2}-\d+$/),
      source_result_sha256:digest(row.source_result_sha256),system_prompt_sha256:digest(row.system_prompt_sha256),
      output_text_sha256:digest(row.output_text_sha256),
      output_text_source:['verified_receipt','static_faq'].includes(row.output_text_source)?row.output_text_source:null,
      jev_faq_version:version(row.jev_faq_version,/^jev-static-faq-\d{4}-\d{2}-\d{2}-\d+$/),
      jev_model:version(row.jev_model,/^jev-\d+\.\d+\.\d+$/),jev_reason:code(row.jev_reason),
      ...numbers(row,['jev_probability','jev_margin','jev_language_probability']),
      milestones_ms:numbers(row.milestones_ms,['preflight','upstream_headers','first_upstream_event','first_text','first_tool_fragment','upstream_done','tool_emitted','total']),
      durations_ms:numbers(row.durations_ms,['tool_signature_persist','upstream_eof_drain','jev_route','jev_classification']),
      eof_drain_outcome:code(row.eof_drain_outcome),
      counts:numbers(row.counts,['history_messages','history_tool_results','upstream_events','text_chunks','text_utf8_bytes','tool_calls','tcp_connects','tls_handshakes','eof_tail_bytes','jev_requests']),
    }))};
}

export async function collectGatewayTiming(request, session, callId, local='http://127.0.0.1:8765') {
  if(!id(session?.id)||typeof session.token!=='string'||!id(callId))throw new Error('gateway_scope_mismatch');
  // The caller's cleanup request uses its independent five-second deadline,
  // including body consumption; a failed/expired voice deadline cannot skip it.
  const response=await request(`${local}/api/sessions/${encodeURIComponent(session.id)}/gateway-timing`,
    {headers:{'x-session-token':session.token}},true);
  const chunks=[];let size=0;
  for await(const chunk of response.body){size+=chunk.length;if(size>1024*1024)throw new Error('gateway_evidence_bounds');chunks.push(chunk);}
  return sanitizeGatewayTiming(JSON.parse(Buffer.concat(chunks).toString('utf8')),session.id,callId);
}

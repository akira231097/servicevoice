/** Offline predicates shared by the bounded synthetic voice runners. */
const normalizedCaption = text => text.normalize('NFKC').replace(/\s+/g,' ').trim();

export class AssistantCaptionTracker {
  pendingPartial = false;
  partialCount = 0;
  finalCaptions = [];

  observe(message) {
    if(message?.type!=='transcript'||message.role!=='assistant'||typeof message.transcript!=='string'
        ||!normalizedCaption(message.transcript)||!['partial','final'].includes(message.transcriptType))return false;
    this.pendingPartial=message.transcriptType==='partial';
    if(this.pendingPartial)this.partialCount++;
    if(message.transcriptType==='final')this.finalCaptions.push(normalizedCaption(message.transcript));
    return true;
  }

  settled(transcript) {
    if(this.pendingPartial||!Array.isArray(transcript))return false;
    const persisted=new Map();
    for(const row of transcript)if(row?.role==='assistant'&&typeof row.text==='string'){
      const text=normalizedCaption(row.text);persisted.set(text,(persisted.get(text)||0)+1);
    }
    // Preserve repeated legitimate utterances; one stored 'yes' cannot satisfy two finals.
    for(const text of this.finalCaptions){
      const count=persisted.get(text)||0;if(count===0)return false;persisted.set(text,count-1);
    }
    return true;
  }
}

export function faqHoursReady(text, trustedStaticFacts, events = []) {
  // The optional minutes must be exactly :00. Never accept 5:30, 15:00 or 5 AM.
  const fivePm = /(?<![\p{L}\p{N}:])(?:5(?::00)?|five)\s*(?:p\.?\s*m\.?|in the (?:afternoon|evening))\b/iu;
  return fivePm.test(String(text)) && /\bcentral\b/i.test(String(text))
    && (trustedStaticFacts === true || events.some(event => event?.name === 'get_dealership_info'));
}

export function persistedGreetingComplete(transcript) {
  if (!Array.isArray(transcript)) return false;
  const text = transcript.filter(row => row?.role === 'assistant' && typeof row.text === 'string')
    .map(row => row.text).join(' ').normalize('NFKC').toLowerCase();
  // This is the saved assistant's closing greeting, also used by the booking runner.
  // Requiring the persisted ending prevents a late greeting caption becoming turn one.
  return /\bhow can i help\b[\s\S]*\bservice visit\b/.test(text);
}

export async function waitForPersistedGreeting({snapshot, quiet, active, now, sleep, callStarted, captionsSettled=()=>true}) {
  const deadline = now() + 20_000;
  while (now() < deadline) {
    active();
    const state = await snapshot();
    if (now() < deadline && persistedGreetingComplete(state.transcript) && captionsSettled(state.transcript) && quiet(callStarted)) return state;
    await sleep(100);
  }
  throw new Error('greeting_caption_or_audio_not_complete');
}

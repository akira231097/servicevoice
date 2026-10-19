import assert from 'node:assert/strict';
import {test} from 'node:test';
import {AdvisorInbox, mountAdvisorInbox, renderCaseDetail} from '../app/static/advisor.js';

const response = (body, status = 200) => ({ok:status >= 200 && status < 300, status, async json() { return body; }});
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const detail = (id = 'handoff_a', changes = {}) => ({id, session_id:`sess_${id}`, status:'open', revision:0, reason:'Stereo help requested',
  created_at:'2026-10-18T19:00:00Z', updated_at:'2026-10-18T19:00:00Z', assigned_operator:null, last_operator:null, last_note:null,
  handoff_status:'callback_requested', context:{session:{id:`sess_${id}`, ended:true, mode:'rehearsal', stage:'handoff'},
    customer:{name:'Alex Morgan', phone:'+15125550142', vehicle:'2021 Toyota Camry', verification:'caller_provided_unverified'}, service:null, appointment:null, proposal:null},
  transcript:[{role:'user', text:'Please save a request for an advisor.'}], transcript_truncated:false, pending_operations:[], pending_operations_truncated:false,
  audit:[], audit_truncated:false, limitations:['Local labels are not verified identities.'], ...changes});
const listing = (items = [detail()], next_cursor = null) => ({items, next_cursor, status:'open', limit:50});
function fixture(handler = () => undefined) {
  const calls = [];
  const inbox = new AdvisorInbox({fetchFn:async (path, options) => {
    calls.push({path, options});
    const result = handler(path, options, calls);
    if (result !== undefined) return await result;
    if (path === '/api/operator/capability') return response({token:'synthetic-memory-capability'});
    if (path.startsWith('/api/operator/cases?')) return response(listing());
    if (path.startsWith('/api/operator/cases/') && options.method === 'GET') return response(detail(decodeURIComponent(path.split('/').at(-1))));
    throw new Error(`Unexpected offline request ${path}`);
  }});
  return {inbox, calls};
}
async function readySelected(inbox) { await inbox.initialize(); await inbox.selectCase('handoff_a'); inbox.setDraft('operator','Demo advisor'); }

test('initialization gets one memory capability and uses bounded authenticated local reads only', async () => {
  const {inbox,calls} = fixture();
  await inbox.initialize();
  assert.equal(inbox.state.ready,true);
  assert.equal(calls.length,2);
  assert.equal(calls[0].options.headers['X-Operator-Token'],undefined);
  assert.equal(calls[1].options.headers['X-Operator-Token'],'synthetic-memory-capability');
  assert.equal(calls[1].path,'/api/operator/cases?status=open&limit=50');
  assert.equal(calls[1].options.cache,'no-store');
  assert.equal(calls[1].options.redirect,'error');
  assert.ok(calls.every(call => !/sessions|calls|vapi|llm|token=/.test(call.path)));
  assert.doesNotMatch(JSON.stringify(inbox.state),/synthetic-memory-capability/);
  inbox.dispose();
  assert.equal(inbox.token,null);
});

test('default browser fetch keeps its global receiver for capability and authenticated reads', async () => {
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = function(path, options) {
    if (this !== globalThis) throw new TypeError('Illegal invocation');
    calls.push({path, options});
    return Promise.resolve(response(path.endsWith('/capability') ? {token:'synthetic-native-capability'} : listing()));
  };
  try {
    const inbox = new AdvisorInbox();
    await inbox.initialize();
    assert.equal(inbox.state.ready,true);
    assert.equal(inbox.state.connectionError,'');
    assert.equal(calls.length,2);
    assert.equal(calls[0].path,'/api/operator/capability');
    assert.equal(calls[1].options.headers['X-Operator-Token'],'synthetic-native-capability');
    inbox.dispose();
    const {document} = dom();
    const mounted = mountAdvisorInbox(document,{windowObject:{addEventListener() {}}});
    await tick();
    assert.equal(mounted.state.ready,true);
    assert.equal(mounted.state.connectionError,'');
    assert.equal(calls.length,4);
    mounted.dispose();
  } finally { globalThis.fetch = previousFetch; }
});

test('out-of-order filter reads cannot restore the previous filter list', async () => {
  const old = deferred(), fresh = deferred(); let started = false;
  const {inbox} = fixture(path => {
    if (!started) return;
    if (path.includes('status=resolved')) return fresh.promise;
    if (path.includes('status=open')) return old.promise;
  });
  await inbox.initialize(); started = true;
  const oldRead = inbox.loadList();
  const freshRead = inbox.setFilter('resolved');
  fresh.resolve(response(listing([detail('resolved_case',{status:'resolved'})])));
  assert.equal(await freshRead,true);
  old.resolve(response(listing([detail('old_case')])));
  assert.equal(await oldRead,false);
  assert.equal(inbox.state.status,'resolved');
  assert.equal(inbox.state.items[0].id,'resolved_case');
  assert.equal(inbox.state.listLoading,false);
});

test('out-of-order detail reads cannot replace the selected case or its note draft', async () => {
  const a = deferred(), b = deferred();
  const {inbox} = fixture(path => path.endsWith('/handoff_a') ? a.promise : path.endsWith('/handoff_b') ? b.promise : undefined);
  await inbox.initialize();
  const aRead = inbox.selectCase('handoff_a'); inbox.setDraft('note','Draft A');
  const bRead = inbox.selectCase('handoff_b'); inbox.setDraft('note','Draft B');
  b.resolve(response(detail('handoff_b'))); await bRead;
  a.resolve(response(detail('handoff_a'))); await aRead;
  assert.equal(inbox.state.detail.id,'handoff_b');
  assert.equal(inbox.draft().note,'Draft B');
  assert.equal(inbox.drafts.get('handoff_a').note,'Draft A');
});

test('successful review uses the exact revision once, clears only the note, and excludes it from open queue', async () => {
  const save = deferred();
  const {inbox,calls} = fixture((path, options) => options.method === 'POST' ? save.promise : undefined);
  await readySelected(inbox); inbox.setDraft('note','Reviewed the caller context.');
  const first = inbox.updateStatus('in_progress');
  assert.equal(await inbox.updateStatus('resolved'),false);
  assert.equal(await inbox.selectCase('handoff_b'),false);
  const post = calls.find(call => call.options.method === 'POST');
  assert.deepEqual(JSON.parse(post.options.body),{expected_revision:0,status:'in_progress',operator:'Demo advisor',note:'Reviewed the caller context.'});
  save.resolve(response(detail('handoff_a',{revision:1,status:'in_progress',assigned_operator:'Demo advisor'})));
  assert.equal(await first,true);
  assert.equal(inbox.state.detail.revision,1);
  assert.equal(inbox.state.items.length,0);
  assert.equal(inbox.draft().note,'');
  assert.equal(inbox.draft().operator,'Demo advisor');
  assert.match(inbox.state.message,/No appointment or contact action/);
  assert.equal(calls.filter(call => call.options.method === 'POST').length,1);
});

test('a pre-save list request cannot overwrite the successful status update', async () => {
  const stale = deferred(); let delayed = false;
  const {inbox} = fixture((path, options) => {
    if (options.method === 'POST') return response(detail('handoff_a',{status:'resolved',revision:1}));
    if (delayed && path.includes('/cases?')) return stale.promise;
  });
  await readySelected(inbox); inbox.setDraft('note','Reviewed locally.'); delayed = true;
  const read = inbox.loadList();
  await inbox.updateStatus('resolved');
  stale.resolve(response(listing())); await read;
  assert.equal(inbox.state.detail.status,'resolved');
  assert.equal(inbox.state.items.length,0);
});

test('revision conflict keeps note and blocks repeat writes until explicit refresh and review', async () => {
  let current = detail(), posts = 0;
  const {inbox} = fixture((path, options) => {
    if (options.method === 'POST') { posts++; current = detail('handoff_a',{status:'in_progress',revision:1}); return response({detail:'Case changed',error:'advisor_revision_conflict'},409); }
    if (path.endsWith('/handoff_a')) return response(current);
  });
  await readySelected(inbox); inbox.setDraft('note','Do not lose this note.');
  assert.equal(await inbox.updateStatus('resolved'),false);
  assert.equal(inbox.state.needsReview,true);
  assert.equal(inbox.draft().note,'Do not lose this note.');
  assert.equal(await inbox.updateStatus('resolved'),false);
  assert.equal(posts,1);
  await inbox.refreshCase();
  assert.equal(inbox.state.detail.revision,1);
  assert.equal(inbox.state.needsReview,false);
  assert.equal(inbox.draft().note,'Do not lose this note.');
  assert.match(inbox.state.message,/Review the status and history/);
  assert.equal(posts,1);
});

test('lost response may have committed; no automatic retry and refresh reveals current result', async () => {
  let current = detail(), posts = 0;
  const {inbox} = fixture((path, options) => {
    if (options.method === 'POST') { posts++; current = detail('handoff_a',{status:'resolved',revision:1}); throw new TypeError('simulated response lost'); }
    if (path.endsWith('/handoff_a')) return response(current);
  });
  await readySelected(inbox); inbox.setDraft('note','Resolution note.');
  assert.equal(await inbox.updateStatus('resolved'),false);
  assert.match(inbox.state.detailError,/may already have succeeded/);
  assert.equal(await inbox.updateStatus('resolved'),false);
  await inbox.refreshCase();
  assert.equal(inbox.state.detail.status,'resolved');
  assert.equal(inbox.draft().note,'Resolution note.');
  assert.equal(await inbox.updateStatus('resolved'),false);
  assert.equal(posts,1);
});

test('pagination passes opaque cursor, stays bounded, and resets history for another filter', async () => {
  const cursor = 'opaque-cursor_abc';
  const {inbox,calls} = fixture(path => path.includes('/cases?') ? response(listing([detail()],path.includes('before=') ? null : cursor)) : undefined);
  await inbox.initialize(); await inbox.nextPage();
  assert.equal(inbox.state.history.length,1);
  assert.equal(inbox.state.before,cursor);
  assert.equal(inbox.state.nextCursor,null);
  assert.equal(inbox.nextPage(),false);
  assert.match(calls.at(-1).path,/limit=50&before=opaque-cursor_abc$/);
  await inbox.previousPage();
  assert.equal(inbox.state.before,null);
  assert.equal(inbox.state.history.length,0);
  await inbox.nextPage(); await inbox.setFilter('all');
  assert.equal(inbox.state.before,null);
  assert.equal(inbox.state.history.length,0);
  assert.match(calls.at(-1).path,/status=all&limit=50$/);
});

test('missing operator and missing resolution note never send a mutation', async () => {
  const {inbox,calls} = fixture(); await inbox.initialize(); await inbox.selectCase('handoff_a');
  assert.equal(await inbox.updateStatus('in_progress'),false);
  inbox.setDraft('operator','Demo advisor');
  assert.equal(await inbox.updateStatus('resolved'),false);
  assert.match(inbox.state.detailError,/review note/);
  assert.equal(calls.filter(call => call.options.method === 'POST').length,0);
});

test('invalid-input response preserves editable draft without pretending the case changed', async () => {
  const {inbox} = fixture((path, options) => options.method === 'POST' ? response({error:'advisor_invalid_input',detail:'Invalid text'},409) : undefined);
  await readySelected(inbox); inbox.setDraft('note','Local note');
  await inbox.updateStatus('resolved');
  assert.equal(inbox.state.needsReview,false);
  assert.equal(inbox.draft().note,'Local note');
  assert.match(inbox.state.detailError,/Check your operator label and note/);
});

test('failed case refresh leaves the previous context read-only', async () => {
  let fail = false;
  const {inbox} = fixture(path => fail && path.endsWith('/handoff_a') ? response({detail:'Unavailable'},503) : undefined);
  await readySelected(inbox); fail = true;
  await inbox.refreshCase();
  assert.equal(inbox.state.detail.id,'handoff_a');
  assert.equal(inbox.state.needsReview,true);
  assert.equal(await inbox.updateStatus('in_progress'),false);
});

test('denied capability loads no cases and shows no raw server details or credentials', async () => {
  const {inbox,calls} = fixture(() => response({detail:'secret synthetic server details'},403));
  await inbox.initialize();
  assert.equal(calls.length,1);
  assert.equal(inbox.state.ready,false);
  assert.match(inbox.state.connectionError,/Local access/);
  assert.doesNotMatch(inbox.state.connectionError,/secret/);
});

test('page disposal aborts reads, clears capability and drafts, and ignores late completion', async () => {
  const pending = deferred(); let emitted = 0;
  const {inbox,calls} = fixture(path => path.endsWith('/handoff_a') ? pending.promise : undefined);
  inbox.onChange = () => emitted++;
  await inbox.initialize(); const read = inbox.selectCase('handoff_a'); inbox.setDraft('note','Temporary');
  inbox.dispose(); const previous = emitted;
  assert.equal(calls.at(-1).options.signal.aborted,true);
  pending.resolve(response(detail())); await read;
  assert.equal(inbox.token,null);
  assert.equal(inbox.drafts.size,0);
  assert.equal(inbox.state.detail,null);
  assert.equal(emitted,previous);
});

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = new Map(); this.value = ''; this._text = ''; }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  set innerHTML(_) { throw new Error('Unsafe innerHTML forbidden'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, listener) { this.listeners.set(name,listener); }
  dispatch(name, value) { if (value !== undefined) this.value = value; this.listeners.get(name)?.({target:this,preventDefault() {}}); }
}
function dom() {
  const elements = new Map();
  return {elements, document:{createElement:tag => new Element(tag),getElementById(id) { if (!elements.has(id)) elements.set(id,new Element()); return elements.get(id); }}};
}

test('renderer preserves hostile content literally, labels truncation, and displays real scheduler shape', () => {
  const {document} = dom(), target = new Element();
  const hostile = '<img src=x onerror=alert(1)><script>claim booking</script>';
  const value = detail('handoff_x',{reason:hostile,transcript:[{role:'user',text:hostile}],transcript_truncated:true,audit_truncated:true,
    audit:[{revision:1,from_status:'open',to_status:'in_progress',operator:hostile,note:hostile,at:'2026-10-18T19:01:00Z'}],
    pending_operations:[{id:'operation_a',status:'unknown'}]});
  value.context.customer.name = hostile;
  value.context.appointment = {reference:'CSV-SYNTHETIC',status:'confirmed',service_name:'Oil and filter service',label:'Mon, Oct 19 at 1:00 PM CDT',customer_name:'Alex',vehicle:'Camry'};
  renderCaseDetail(document,target,value);
  assert.match(target.textContent,/<img src=x onerror=alert\(1\)>/);
  assert.match(target.textContent,/Transcript truncated/);
  assert.match(target.textContent,/History truncated/);
  assert.match(target.textContent,/unresolved operation/);
  assert.match(target.textContent,/Mon, Oct 19 at 1:00 PM CDT/);
  assert.match(target.textContent,/Caller-provided details are unverified/);
  const tags = []; const visit = node => { tags.push(node.tagName); node.children.forEach(visit); }; visit(target);
  assert.ok(!tags.includes('img') && !tags.includes('script') && !tags.includes('iframe'));
});

test('mounted form keeps draft and disables actions after conflict until an explicit refresh', async () => {
  const {document,elements} = dom(); let current = detail(), posts = 0; const handlers = new Map();
  const inbox = mountAdvisorInbox(document,{windowObject:{addEventListener:(name,handler) => handlers.set(name,handler)},fetchFn:async (path, options) => {
    if (path.endsWith('/capability')) return response({token:'synthetic'});
    if (path.includes('/cases?')) return response(listing());
    if (options.method === 'POST') { posts++; current = detail('handoff_a',{revision:1,status:'in_progress'}); return response({error:'advisor_revision_conflict'},409); }
    return response(current);
  }});
  await tick();
  elements.get('case-list').children[0].dispatch('click'); await tick();
  elements.get('operator-label').dispatch('input','Demo advisor'); elements.get('review-note').dispatch('input','Keep this draft.');
  elements.get('resolve-case').dispatch('click'); await tick();
  assert.equal(elements.get('review-note').value,'Keep this draft.');
  assert.equal(elements.get('resolve-case').disabled,true);
  assert.equal(elements.get('review-warning').hidden,false);
  elements.get('resolve-case').dispatch('click'); await tick(); assert.equal(posts,1);
  elements.get('refresh-case').dispatch('click'); await tick();
  assert.equal(elements.get('resolve-case').disabled,false);
  assert.equal(elements.get('mark-in-progress').disabled,true);
  assert.equal(elements.get('review-note').value,'Keep this draft.');
  assert.match(elements.get('detail-status').textContent,/Review the status and history/);
  handlers.get('pagehide')(); assert.equal(inbox.closed,true);
});

test('withdrawn records stay out of open/in-progress lists but remain reviewable under resolved/all', async () => {
  const saved=detail('handoff_withdrawn',{status:'resolved',handoff_status:'withdrawn'});
  const {inbox}=fixture(path=>path.includes('/cases?') ? response(listing([saved])) : undefined);
  await inbox.initialize();assert.equal(inbox.state.items.length,0);
  await inbox.setFilter('in_progress');assert.equal(inbox.state.items.length,0);
  await inbox.setFilter('resolved');assert.equal(inbox.state.items[0].id,saved.id);
  await inbox.setFilter('all');assert.equal(inbox.state.items[0].handoff_status,'withdrawn');
});

test('fresh withdrawn detail removes an old open entry and rejects the older pending list response', async () => {
  let current=detail(), delay=false;const stale=deferred();
  const {inbox}=fixture(path=>{
    if(path.endsWith('/handoff_a'))return response(current);
    if(delay&&path.includes('/cases?'))return stale.promise;
  });
  await readySelected(inbox);inbox.setDraft('note','Retain my unsaved draft.');delay=true;
  const pending=inbox.loadList();
  current=detail('handoff_a',{status:'resolved',handoff_status:'withdrawn'});
  await inbox.refreshCase();
  stale.resolve(response(listing()));await pending;
  assert.equal(inbox.state.items.length,0);assert.equal(inbox.state.listLoading,false);
  assert.equal(inbox.state.detail.handoff_status,'withdrawn');
  assert.equal(inbox.draft().note,'Retain my unsaved draft.');
  assert.equal(await inbox.updateStatus('open'),false);
});

test('mounted withdrawn detail disables every edit/action and retains truthful read-only context', async () => {
  const {document,elements}=dom();let posts=0;
  const saved=detail('handoff_a',{status:'resolved',handoff_status:'withdrawn'});
  const inbox=mountAdvisorInbox(document,{windowObject:{addEventListener(){}},fetchFn:async(path,options)=>{
    if(path.endsWith('/capability'))return response({token:'synthetic'});
    if(path.includes('/cases?'))return response(listing([]));
    if(options.method==='POST')posts++;
    return response(saved);
  }});
  await tick();await inbox.selectCase('handoff_a');
  for(const id of ['operator-label','review-note','mark-in-progress','resolve-case','reopen-case'])assert.equal(elements.get(id).disabled,true,id);
  assert.match(elements.get('case-detail').textContent,/No appointment was changed and no external contact was made/);
  assert.match(elements.get('case-detail').textContent,/retained under Resolved/);
  assert.match(elements.get('review-warning').textContent,/read-only/);
  elements.get('operator-label').dispatch('input','Forged operator');
  elements.get('reopen-case').dispatch('click');await tick();
  assert.equal(inbox.draft().operator,'');assert.equal(posts,0);
  inbox.dispose();
});

test('withdrawal during an operator save keeps the draft and requires read-only refresh without retry', async () => {
  let current=detail(),posts=0;
  const {inbox}=fixture((path,options)=>{
    if(options.method==='POST') {posts++;current=detail('handoff_a',{status:'resolved',handoff_status:'withdrawn'});return response({error:'advisor_case_withdrawn'},409);}
    if(path.endsWith('/handoff_a'))return response(current);
  });
  await readySelected(inbox);inbox.setDraft('note','Do not lose this draft.');
  assert.equal(await inbox.updateStatus('resolved'),false);
  assert.match(inbox.state.detailError,/withdrawn/);assert.equal(inbox.state.items.length,0);
  assert.equal(await inbox.updateStatus('resolved'),false);
  await inbox.refreshCase();assert.equal(await inbox.updateStatus('open'),false);
  assert.equal(inbox.draft().note,'Do not lose this draft.');assert.equal(posts,1);
});

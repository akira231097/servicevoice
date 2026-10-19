// A separate local operator view. Capabilities and unsaved drafts stay in memory.
export const CASE_STATUSES = Object.freeze({open:'Open', in_progress:'In progress', resolved:'Resolved'});
const PAGE_SIZE = 50;
class InboxError extends Error {
  constructor(status, code = '') { super('Inbox request failed'); this.status = status; this.code = code; }
}
const failureMessage = error => {
  if (error.status === 401 || error.status === 403) return 'Local access expired or was denied. Reload this page to reconnect; unsaved notes will be lost.';
  if (error.status === 404) return 'This request could not be found. Refresh the list to review available requests.';
  if (error.status === 422 || error.code === 'advisor_invalid_input') return 'Check your operator label and note. A resolution needs a note, with no more than 2,000 characters.';
  return 'The local service could not be reached or returned an unexpected response.';
};
const validCase = value => value && typeof value.id === 'string' && Object.hasOwn(CASE_STATUSES, value.status) && Number.isInteger(value.revision) && value.revision >= 0;
const withdrawn = value => value?.handoff_status === 'withdrawn';
const inFilter = (value, status) => status === 'all' || (value.status === status && !(withdrawn(value) && status !== 'resolved'));
const statusLabel = value => withdrawn(value) ? 'Withdrawn' : CASE_STATUSES[value.status];

export class AdvisorInbox {
  constructor({fetchFn = globalThis.fetch, onChange = () => {}, requestTimeoutMs = 12000} = {}) {
    this.fetchFn = fetchFn;
    this.onChange = onChange;
    this.requestTimeoutMs = requestTimeoutMs;
    this.token = null;
    this.closed = false;
    this.requests = new Set();
    this.listGeneration = 0;
    this.detailGeneration = 0;
    this.drafts = new Map();
    this.state = {ready:false, connecting:true, connectionError:'', status:'open', items:[], before:null, nextCursor:null, history:[],
      listLoading:false, listError:'', selectedId:null, detail:null, detailLoading:false, detailError:'', saving:false, needsReview:false, message:''};
  }
  emit() { if (!this.closed) this.onChange(this.state, this); }
  async request(path, options = {}) {
    const controller = new AbortController();
    this.requests.add(controller);
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      // Native browser fetch requires its Window receiver, not this controller.
      const response = await this.fetchFn.call(globalThis, path, {method:options.method || 'GET', cache:'no-store', credentials:'same-origin', redirect:'error',
        headers:{...(this.token ? {'X-Operator-Token':this.token} : {}), ...(options.body ? {'Content-Type':'application/json'} : {})},
        ...(options.body ? {body:JSON.stringify(options.body)} : {}), signal:controller.signal});
      const body = await response.json();
      if (!response.ok) throw new InboxError(response.status, typeof body?.error === 'string' ? body.error : '');
      return body;
    } finally { clearTimeout(timer); this.requests.delete(controller); }
  }
  async initialize() {
    try {
      const capability = await this.request('/api/operator/capability');
      if (typeof capability.token !== 'string' || !capability.token) throw new InboxError(502);
      if (this.closed) return;
      this.token = capability.token;
      this.state.ready = true;
      this.state.connecting = false;
      this.emit();
      await this.loadList();
    } catch (error) {
      if (this.closed) return;
      this.state.connecting = false;
      this.state.connectionError = failureMessage(error);
      this.emit();
    }
  }
  async loadList({before = this.state.before, history = this.state.history} = {}) {
    if (!this.state.ready || this.closed || this.state.saving) return false;
    const generation = ++this.listGeneration;
    const filter = this.state.status;
    this.state.listLoading = true;
    this.state.listError = '';
    this.emit();
    const query = new URLSearchParams({status:filter, limit:String(PAGE_SIZE)});
    if (before) query.set('before', before);
    try {
      const result = await this.request(`/api/operator/cases?${query}`);
      if (!Array.isArray(result.items) || result.items.length > PAGE_SIZE || result.items.some(item => !validCase(item)) ||
          !(result.next_cursor === null || typeof result.next_cursor === 'string')) throw new InboxError(502);
      if (this.closed || generation !== this.listGeneration) return false;
      this.state.items = result.items.filter(item => inFilter(item, filter));
      this.state.before = before;
      this.state.history = [...history];
      this.state.nextCursor = result.next_cursor;
      return true;
    } catch (error) {
      if (this.closed || generation !== this.listGeneration) return false;
      this.state.listError = failureMessage(error);
      return false;
    } finally {
      if (!this.closed && generation === this.listGeneration) { this.state.listLoading = false; this.emit(); }
    }
  }
  async setFilter(status) {
    if (this.state.saving || !(status === 'all' || Object.hasOwn(CASE_STATUSES, status))) return false;
    this.state.status = status;
    this.state.items = [];
    this.state.before = null;
    this.state.nextCursor = null;
    this.state.history = [];
    return this.loadList();
  }
  nextPage() {
    if (!this.state.nextCursor || this.state.listLoading) return false;
    return this.loadList({before:this.state.nextCursor, history:[...this.state.history, this.state.before]});
  }
  previousPage() {
    if (!this.state.history.length || this.state.listLoading) return false;
    return this.loadList({before:this.state.history.at(-1), history:this.state.history.slice(0, -1)});
  }
  draft() {
    if (!this.state.selectedId) return {operator:'', note:''};
    if (!this.drafts.has(this.state.selectedId)) this.drafts.set(this.state.selectedId, {operator:'', note:''});
    return this.drafts.get(this.state.selectedId);
  }
  setDraft(field, value) {
    if (this.state.saving || withdrawn(this.state.detail) || !this.state.selectedId || !['operator','note'].includes(field)) return;
    this.draft()[field] = String(value).slice(0, field === 'operator' ? 80 : 2000);
    this.emit();
  }
  async selectCase(id) {
    if (!this.state.ready || this.state.saving || this.closed || typeof id !== 'string') return false;
    const generation = ++this.detailGeneration;
    const changed = this.state.selectedId !== id;
    this.state.selectedId = id;
    if (changed) { this.state.detail = null; this.state.message = ''; }
    this.state.detailLoading = true;
    this.state.detailError = '';
    this.state.needsReview = true;
    this.emit();
    try {
      const detail = await this.request(`/api/operator/cases/${encodeURIComponent(id)}`);
      if (!validCase(detail) || detail.id !== id || !detail.context || !Array.isArray(detail.transcript) || !Array.isArray(detail.audit)) throw new InboxError(502);
      if (this.closed || generation !== this.detailGeneration) return false;
      this.state.detail = detail;
      this.state.needsReview = false;
      if (withdrawn(detail)) {
        // A detail refresh can discover caller withdrawal before the list does.
        // Do not let an older pending list response restore that open request.
        ++this.listGeneration;
        this.state.listLoading = false;
        this.state.items = this.state.items.map(item => item.id === detail.id ? detail : item).filter(item => inFilter(item, this.state.status));
        this.state.message = 'This local advisor request was withdrawn. Its history is read-only and it cannot be reopened. Your draft has been kept.';
      } else if (!changed) this.state.message = 'Current case loaded. Review the status and history before choosing another action. Your draft has been kept.';
      return true;
    } catch (error) {
      if (this.closed || generation !== this.detailGeneration) return false;
      this.state.detailError = failureMessage(error);
      return false;
    } finally {
      if (!this.closed && generation === this.detailGeneration) { this.state.detailLoading = false; this.emit(); }
    }
  }
  refreshCase() { return this.state.selectedId ? this.selectCase(this.state.selectedId) : false; }
  async updateStatus(status) {
    const current = this.state.detail;
    if (this.closed || this.state.saving || this.state.detailLoading || this.state.needsReview || !current || withdrawn(current) || current.id !== this.state.selectedId) return false;
    if (!Object.hasOwn(CASE_STATUSES, status) || status === current.status) return false;
    const draft = this.draft();
    const operator = draft.operator.trim(), note = draft.note.trim();
    if (!operator || (status === 'resolved' && !note)) {
      this.state.detailError = !operator ? 'Add a local operator label before saving.' : 'Add a review note before resolving this request.';
      this.emit();
      return false;
    }
    this.state.saving = true;
    // A list read begun before this write must not restore an older case status.
    ++this.listGeneration;
    this.state.listLoading = false;
    this.state.detailError = '';
    this.state.message = '';
    this.emit();
    try {
      const result = await this.request(`/api/operator/cases/${encodeURIComponent(current.id)}/status`, {
        method:'POST', body:{expected_revision:current.revision, status, operator, note}});
      if (!validCase(result) || result.id !== current.id || result.revision !== current.revision + 1 || result.status !== status ||
          !result.context || !Array.isArray(result.transcript) || !Array.isArray(result.audit)) throw new InboxError(502);
      if (this.closed) return false;
      this.state.detail = result;
      this.state.items = this.state.items.map(item => item.id === result.id ? result : item)
        .filter(item => inFilter(item, this.state.status));
      draft.note = '';
      this.state.message = `Saved locally. Review status: ${CASE_STATUSES[result.status]}. No appointment or contact action was performed.`;
      return true;
    } catch (error) {
      if (this.closed) return false;
      const invalidInput = error.code === 'advisor_invalid_input';
      const uncertain = !error.status || error.status >= 500 || (error.status === 409 && !invalidInput);
      this.state.needsReview = uncertain || error.status === 401 || error.status === 403 || error.status === 404;
      if (error.code === 'advisor_case_withdrawn') {
        this.state.items = this.state.items.filter(item => item.id !== current.id || !['open','in_progress'].includes(this.state.status));
        this.state.detailError = 'This local advisor request was withdrawn. No review change was saved. Your draft is kept; refresh the case to review the retained history.';
      } else if (error.status === 409 && !invalidInput) this.state.detailError = 'This request changed or the action is no longer valid. Your draft is kept. Refresh the case, review its history, then choose an action.';
      else if (uncertain) this.state.detailError = 'The save outcome is unknown. It may already have succeeded. Your draft is kept; refresh the case and review its history before saving again.';
      else this.state.detailError = failureMessage(error);
      return false;
    } finally { if (!this.closed) { this.state.saving = false; this.emit(); } }
  }
  dispose() {
    this.closed = true;
    this.token = null;
    for (const request of this.requests) request.abort();
    this.requests.clear();
    this.drafts.clear();
  }
}

const dateText = value => {
  if (!value) return 'Not recorded';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Not recorded' : `${parsed.toISOString().replace('T', ' ').slice(0, 19)} UTC`;
};
const ageText = value => {
  const minutes = Math.floor((Date.now() - new Date(value).getTime()) / 60000);
  if (!Number.isFinite(minutes) || minutes < 0) return '';
  if (minutes < 1) return 'Just saved';
  if (minutes < 60) return `${minutes} min old`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} hr old`;
  const days = Math.floor(minutes / 1440);
  return `${days} day${days === 1 ? '' : 's'} old`;
};
function node(document, tag, text = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}
function facts(document, values) {
  const list = node(document, 'dl', '', 'facts');
  for (const [label, value] of values) {
    const row = node(document, 'div');
    row.append(node(document, 'dt', label), node(document, 'dd', value === null || value === undefined || value === '' ? 'Not provided' : String(value)));
    list.append(row);
  }
  return list;
}
function section(document, heading) {
  const element = node(document, 'section', '', 'detail-section');
  element.append(node(document, 'h3', heading));
  return element;
}
export function renderCaseDetail(document, target, detail) {
  target.replaceChildren();
  if (!detail) return;
  target.append(node(document, 'span', statusLabel(detail), `status-pill ${detail.status}`),
    node(document, 'p', detail.reason, 'case-summary'),
    facts(document, [['Request', detail.id], ['Saved', dateText(detail.created_at)], ['Last updated', dateText(detail.updated_at)],
      ['Revision', detail.revision], ['Assigned local label', detail.assigned_operator || 'Unassigned'], ['Last operator label', detail.last_operator || 'None']]));
  if (withdrawn(detail)) target.append(node(document, 'p', 'The caller withdrew this local advisor request. It is retained under Resolved for review and cannot be reopened. No appointment was changed and no external contact was made.', 'context-note'));
  const context = detail.context;
  const customer = context.customer || {};
  const known = section(document, 'Known details');
  known.append(node(document, 'p', 'Caller-provided details are unverified. Missing information stays unknown.', 'muted'),
    facts(document, [['Name', customer.name], ['Phone', customer.phone], ['Vehicle', customer.vehicle], ['Service', context.service],
      ['Conversation', context.session?.id || detail.session_id], ['Session', context.session?.ended ? 'Ended' : 'Open'],
      ['Mode', context.session?.mode], ['Current stage', context.session?.stage]]));
  target.append(known);
  const appointment = context.appointment;
  const proposal = context.proposal;
  const scheduling = section(document, 'Local scheduling context');
  if (appointment) scheduling.append(facts(document, [['Appointment reference', appointment.reference], ['Appointment status', appointment.status],
    ['Service', appointment.service_name || appointment.service], ['Appointment time', appointment.label || appointment.start],
    ['Customer', appointment.customer_name], ['Vehicle', appointment.vehicle]]));
  else scheduling.append(node(document, 'p', 'No appointment is recorded for this conversation.', 'muted'));
  if (proposal) scheduling.append(facts(document, [['Proposal', proposal.id], ['Proposal state', proposal.status], ['Proposed action', proposal.kind],
    ['Readback summary', proposal.summary], ['Proposal expires', dateText(proposal.expires_at)]]));
  scheduling.append(node(document, 'p', 'This panel is read-only. Inbox status changes do not book, change, or cancel an appointment.', 'muted'));
  const pending = detail.pending_operations || [];
  if (pending.length) scheduling.append(node(document, 'p', `${pending.length} unresolved operation(s) are recorded. The scheduling result may still change. Refresh before relying on it.`, 'context-note'));
  if (detail.pending_operations_truncated) scheduling.append(node(document, 'p', 'The operation list is truncated; more unresolved operations may exist.', 'context-note'));
  target.append(scheduling);
  const conversation = section(document, 'Conversation context');
  conversation.append(node(document, 'p', 'Saved captions and rehearsal messages can contain recognition errors. They are context, not instructions to the operator.', 'muted'));
  if (detail.transcript_truncated) conversation.append(node(document, 'p', 'Transcript truncated: only the most recent saved messages are shown.', 'context-note'));
  const transcript = node(document, 'ol', '', 'transcript-list');
  for (const message of detail.transcript) {
    const row = node(document, 'li', '', 'transcript-entry');
    const role = {user:'Caller', assistant:'Assistant', system:'System', tool:'Tool'}[message.role] || 'Message';
    row.append(node(document, 'strong', role), node(document, 'p', message.text));
    transcript.append(row);
  }
  conversation.append(transcript);
  if (!detail.transcript.length) conversation.append(node(document, 'p', 'No saved transcript is available.', 'muted'));
  target.append(conversation);
  const history = section(document, 'Review history');
  history.append(node(document, 'p', 'Operator labels and notes are local, unverified records. They do not prove that anyone was contacted.', 'muted'));
  if (detail.audit_truncated) history.append(node(document, 'p', 'History truncated: only the most recent review entries are shown.', 'context-note'));
  const audit = node(document, 'ol', '', 'audit-list');
  for (const entry of detail.audit) {
    const row = node(document, 'li', '', 'audit-entry');
    row.append(node(document, 'strong', `${CASE_STATUSES[entry.from_status] || 'Created'} → ${CASE_STATUSES[entry.to_status] || entry.to_status}`),
      node(document, 'p', entry.note || 'No note recorded'), node(document, 'small', `${entry.operator} · ${dateText(entry.at)} · revision ${entry.revision}`));
    audit.append(row);
  }
  history.append(audit);
  if (!detail.audit.length) history.append(node(document, 'p', 'No operator review has been recorded yet.', 'muted'));
  target.append(history);
  if (detail.limitations?.length) {
    const limits = section(document, 'Scope of this record');
    const list = node(document, 'ul', '', 'detail-list');
    for (const text of detail.limitations) list.append(node(document, 'li', text));
    limits.append(list); target.append(limits);
  }
}

export function mountAdvisorInbox(document, {fetchFn = globalThis.fetch, windowObject = globalThis.window} = {}) {
  const $ = id => document.getElementById(id);
  let renderedDetail = null, renderedItems = null, renderedSelection = null;
  const inbox = new AdvisorInbox({fetchFn, onChange:state => {
    $('connection-status').textContent = state.connectionError || (state.connecting ? 'Opening the local inbox…' : 'Local connection ready · updates appear when you refresh · review timestamps are UTC');
    $('list-error').textContent = state.listError;
    $('list-error').hidden = !state.listError;
    $('detail-error').textContent = state.detailError;
    $('detail-error').hidden = !state.detailError;
    $('status-filter').value = state.status;
    $('status-filter').disabled = !state.ready || state.saving;
    $('refresh-list').disabled = !state.ready || state.saving || state.listLoading;
    $('list-status').textContent = state.listLoading ? 'Loading requests…' : `${state.items.length} request${state.items.length === 1 ? '' : 's'} on this page${!state.items.length ? '. Try another status or refresh after a caller saves a request.' : '.'}`;
    $('case-list').setAttribute('aria-busy', String(state.listLoading));
    if (renderedItems !== state.items || renderedSelection !== state.selectedId) {
      $('case-list').replaceChildren();
      for (const item of state.items) {
        const button = node(document, 'button', '', 'case-row');
        button.type = 'button'; button.setAttribute('aria-pressed', String(item.id === state.selectedId));
        const top = node(document, 'span', '', 'row-top');
        top.append(node(document, 'span', statusLabel(item), `status-pill ${item.status}`), node(document, 'span', ageText(item.created_at), 'case-meta'));
        button.append(top, node(document, 'span', item.reason, 'reason'), node(document, 'span', item.id, 'case-meta'),
          node(document, 'span', item.assigned_operator ? `Local label: ${item.assigned_operator}` : 'Unassigned', 'case-meta'));
        button.addEventListener('click', () => { if (!inbox.state.saving) void inbox.selectCase(item.id); });
        $('case-list').append(button);
      }
      renderedItems = state.items; renderedSelection = state.selectedId;
    }
    for (const button of $('case-list').children) button.disabled = state.saving;
    $('previous-page').disabled = !state.history.length || state.listLoading || state.saving;
    $('next-page').disabled = !state.nextCursor || state.listLoading || state.saving;
    $('page-label').textContent = `Page ${state.history.length + 1}`;
    $('detail-title').textContent = state.selectedId ? 'Review the request' : 'Select a request';
    $('refresh-case').hidden = !state.selectedId;
    $('refresh-case').disabled = state.detailLoading || state.saving;
    $('detail-status').textContent = state.saving ? 'Saving your review…' : state.detailLoading ? 'Loading the current case…' : state.message || (state.detail ? 'Review the available context below.' : 'Choose a request to review the context and record a next step.');
    $('case-detail').setAttribute('aria-busy', String(state.detailLoading));
    if (renderedDetail !== state.detail) { renderCaseDetail(document, $('case-detail'), state.detail); renderedDetail = state.detail; }
    $('review-form').hidden = !state.detail;
    const draft = inbox.draft();
    if ($('operator-label').value !== draft.operator) $('operator-label').value = draft.operator;
    if ($('review-note').value !== draft.note) $('review-note').value = draft.note;
    const readOnly = withdrawn(state.detail);
    $('operator-label').disabled = state.saving || readOnly;
    $('review-note').disabled = state.saving || readOnly;
    $('review-warning').hidden = !state.needsReview && !readOnly;
    $('review-warning').textContent = readOnly ? 'This request was withdrawn. The retained history is read-only; operator review cannot reopen it.' : 'Save is paused. Refresh the case and review its current status and history before taking another action.';
    const blocked = !state.detail || state.detailLoading || state.saving || state.needsReview || readOnly;
    for (const [id, status] of [['mark-in-progress','in_progress'], ['resolve-case','resolved'], ['reopen-case','open']]) $(id).disabled = blocked || state.detail?.status === status;
  }});
  $('status-filter').addEventListener('change', event => { void inbox.setFilter(event.target.value); });
  $('refresh-list').addEventListener('click', () => { void inbox.loadList(); });
  $('previous-page').addEventListener('click', () => { void inbox.previousPage(); });
  $('next-page').addEventListener('click', () => { void inbox.nextPage(); });
  $('refresh-case').addEventListener('click', () => { void inbox.refreshCase(); });
  $('operator-label').addEventListener('input', event => inbox.setDraft('operator', event.target.value));
  $('review-note').addEventListener('input', event => inbox.setDraft('note', event.target.value));
  $('review-form').addEventListener('submit', event => event.preventDefault());
  for (const [id, status] of [['mark-in-progress','in_progress'], ['resolve-case','resolved'], ['reopen-case','open']]) $(id).addEventListener('click', () => { void inbox.updateStatus(status); });
  windowObject?.addEventListener('pagehide', () => inbox.dispose(), {once:true});
  void inbox.initialize();
  return inbox;
}

if (typeof document !== 'undefined' && document.getElementById('case-list')) mountAdvisorInbox(document);

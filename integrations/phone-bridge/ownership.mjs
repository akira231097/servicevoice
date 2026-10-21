// Durable private ownership: a restart must never erase an unknown paid leg.
import fs from 'node:fs';
import path from 'node:path';
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export class PhoneOwnershipJournal {
  constructor(filename) {
    if (typeof filename !== 'string' || !path.isAbsolute(filename)) throw new Error('phone_ownership_path');
    this.filename = filename; this.lock = filename + '.lock'; this.closed = false;
    const directory = fs.lstatSync(path.dirname(filename));
    if (!directory.isDirectory() || directory.isSymbolicLink() || (process.platform !== 'win32' && directory.mode & 0o077)
      || (process.getuid && directory.uid !== process.getuid())) throw new Error('phone_ownership_directory');
    const descriptor = fs.openSync(this.lock, 'wx', 0o600); fs.closeSync(descriptor);
    try {
      if (fs.existsSync(filename)) {
        const info = fs.lstatSync(filename);
        if (!info.isFile() || info.isSymbolicLink() || (process.platform !== 'win32' && info.mode & 0o077) || info.size > 262144) throw new Error('phone_ownership_file');
        const saved = JSON.parse(fs.readFileSync(filename, 'utf8'));
        if (saved.schema !== 'phone-owned-legs-v1' || !Array.isArray(saved.pending)) throw new Error('phone_ownership_schema');
        if (saved.pending.length) throw new Error('phone_unresolved_ownership_requires_readback');
      }
      this.rows = new Map(); this.persist();
    } catch (error) { fs.unlinkSync(this.lock); throw error; }
  }
  persist() {
    if (this.closed) throw new Error('phone_ownership_closed');
    const temporary = this.filename + '.updating';
    const descriptor = fs.openSync(temporary, 'wx', 0o600);
    try { fs.writeFileSync(descriptor, JSON.stringify({ schema: 'phone-owned-legs-v1', pending: [...this.rows.values()] }) + '\n'); fs.fsyncSync(descriptor); }
    finally { fs.closeSync(descriptor); }
    fs.renameSync(temporary, this.filename);
    if (process.platform !== 'win32') {
      const directory = fs.openSync(path.dirname(this.filename), 'r');
      try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    }
  }
  reserve(outerCallId) {
    if (!uuid.test(outerCallId) || this.rows.has(outerCallId) || this.rows.size >= 4) throw new Error('phone_ownership_reservation');
    this.rows.set(outerCallId, { outer_call_id: outerCallId, allocation_attempted: false, inner_call_id: null }); this.persist();
  }
  allocating(outerCallId) { const row = this.rows.get(outerCallId); if (!row) throw new Error('phone_ownership_missing'); row.allocation_attempted = true; this.persist(); }
  allocated(outerCallId, innerCallId) {
    const row = this.rows.get(outerCallId);
    if (!row?.allocation_attempted || !uuid.test(innerCallId) || (row.inner_call_id && row.inner_call_id !== innerCallId)) throw new Error('phone_ownership_binding');
    row.inner_call_id = innerCallId; this.persist();
  }
  release(outerCallId, { outerEnded, innerEnded }) {
    const row = this.rows.get(outerCallId);
    if (!row || outerEnded !== true || (row.allocation_attempted && (!row.inner_call_id || innerEnded !== true))) throw new Error('phone_ownership_end_unverified');
    this.rows.delete(outerCallId); this.persist();
  }
  close() { if (this.closed) return; this.closed = true; fs.unlinkSync(this.lock); }
}

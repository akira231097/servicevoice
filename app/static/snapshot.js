// Coalesce reads and reject stale completions when the user changes sessions.
// The caller supplies network access so this coordinator is testable offline.
export class SnapshotChannel {
  constructor({ read, apply, onError = () => {} }) {
    this.read = read;
    this.apply = apply;
    this.onError = onError;
    this.target = null;
  }

  bind(session) {
    this.target?.controller?.abort();
    this.target = session ? { session, controller: null, pending: null } : null;
  }

  refresh({ fresh = false, timeoutMs = 20000, reportErrors = true } = {}) {
    const target = this.target;
    if (!target) return Promise.resolve(null);
    if (target.pending) {
      if (!fresh) return target.pending;
      // An update arriving during a read deserves one read after that completes.
      return target.pending.then(() => this.target === target
        ? this.refresh({ timeoutMs, reportErrors }) : null);
    }
    const controller = new AbortController();
    target.controller = controller;
    target.pending = Promise.resolve().then(() => this.read(target.session, { signal: controller.signal, timeoutMs }))
      .then(snapshot => {
        if (this.target !== target || controller.signal.aborted) return null;
        this.apply(snapshot);
        return snapshot;
      }).catch(error => {
        if (this.target === target && !controller.signal.aborted && reportErrors) this.onError(error);
        return null;
      }).finally(() => {
        target.pending = null;
        target.controller = null;
      });
    return target.pending;
  }
}

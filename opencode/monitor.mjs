import { classifyTask, finalAssistantText } from './lifecycle.mjs';

export function lifecycleEvent(type) {
  return /^(?:session\.(?:execution\.|inbox\.|created$|deleted$|moved$|shell\.)|form\.|permission\.|shell\.|server\.connected$)/.test(type);
}

function refreshing(result) {
  const { completionKey, ...rest } = result;
  return { ...rest, state: 'working', complete: false, reason: 'refreshing' };
}

export class TaskMonitor {
  constructor({ load, notifier, now = Date.now, logger = console, previous }) {
    this.load = load;
    this.notifier = notifier;
    this.now = now;
    this.logger = logger;
    this.revision = 0;
    this.aliases = new Map();
    this.cache = new Map();
    this.interest = new Map();
    this.inflight = new Map();
    this.touched = new Set();
    this.completions = new Set();
    this.settled = new Map();
    this.shellOwners = new Map();
    this.shells = new Map();
    this.sent = new Set();
    this.terminal = new Set();
    this.retryAt = new Map();
    this.publishing = new Map();
    if (previous) {
      // Revoke old asynchronous admissions, preserving only observation metadata.
      // In-flight reads/publications belong to the old generation, never the new one.
      this.revision = ++previous.revision;
      for (const name of ['aliases', 'cache', 'interest', 'settled', 'shellOwners', 'shells', 'retryAt']) {
        this[name] = new Map(previous[name] || []);
      }
      for (const name of ['touched', 'completions', 'sent', 'terminal']) {
        this[name] = new Set(previous[name] || []);
      }
    }
  }

  observe(event) {
    if (!lifecycleEvent(event.type)) return;
    this.revision++;
    const data = event.data || {};
    let id = data.sessionID || data.form?.sessionID || data.info?.metadata?.sessionID;
    if (event.type === 'shell.created' && id) {
      this.shellOwners.set(data.info.id, id);
      this.shells.set(data.info.id, { ...data.info, location: event.location });
    }
    if (event.type === 'shell.exited') {
      id = this.shellOwners.get(data.id);
      this.shellOwners.delete(data.id);
      if (id) this.settled.set(id, Math.max(this.settled.get(id) || 0, event.created || this.now()));
      const shell = this.shells.get(data.id);
      if (shell) this.shells.set(data.id, { ...shell, status: data.status, time: { ...shell.time, completed: event.created || this.now() } });
    }
    if (event.type === 'session.shell.ended' && id) {
      this.settled.set(id, Math.max(this.settled.get(id) || 0, event.created || this.now()));
    }
    if (event.type === 'server.connected') for (const root of this.cache.keys()) this.touched.add(root);
    if (typeof id !== 'string' || !id.startsWith('ses_')) return;
    const root = this.aliases.get(id) || id;
    if (event.type === 'session.deleted') {
      this.completions.delete(id);
      this.cache.delete(id);
      this.touched.delete(id);
      if (root !== id) this.touched.add(root);
      return;
    }
    this.touched.add(root);
    this.interest.set(root, this.now() + 60_000);
    if (event.type === 'session.execution.succeeded') this.completions.add(id);
    if (['session.execution.started', 'session.execution.failed', 'session.execution.interrupted', 'session.inbox.enqueued'].includes(event.type)) {
      this.completions.delete(id);
    }
  }

  async inspect(sessionID, force = false) {
    const key = this.aliases.get(sessionID) || sessionID;
    if (!force) this.interest.set(key, this.now() + 60_000);
    const cached = this.cache.get(key);
    if (!force && cached?.revision === this.revision && this.now() - cached.time < 500) return cached.result;
    const current = this.inflight.get(key);
    if (current?.revision === this.revision) return current.promise;
    const revision = this.revision;
    const pending = { revision, promise: this.read(key, revision) };
    this.inflight.set(key, pending);
    try { return await pending.promise; }
    catch (error) {
      if (error?._tag !== 'SessionNotFoundError' && error?.status !== 404) this.touched.add(key);
      throw error;
    }
    finally { if (this.inflight.get(key) === pending) this.inflight.delete(key); }
  }

  async read(sessionID, revision) {
    const snapshot = await this.load(sessionID, { knownShells: [...this.shells.values()] });
    for (const session of snapshot.sessions) this.aliases.set(session.id, snapshot.root.id);
    for (const shell of snapshot.shells) {
      this.shells.set(shell.id, shell);
      if (shell.status === 'running') this.shellOwners.set(shell.id, shell.metadata.sessionID);
    }
    snapshot.settledAfter = Math.max(0, ...snapshot.sessions.map((session) => this.settled.get(session.id) || 0));
    let result = classifyTask(snapshot);
    if (revision !== this.revision) {
      result = refreshing(result);
    } else {
      this.touched.delete(sessionID);
    }
    const previous = this.cache.get(result.rootID);
    if (previous && result.complete && snapshot.root.time.idle > previous.rootIdle) this.completions.add(result.rootID);
    if (!previous || previous.revision <= revision) {
      this.cache.set(result.rootID, {
        result, time: this.now(), revision,
        // An invalidated read is not a new authoritative recovery watermark.
        rootIdle: revision === this.revision ? (snapshot.root.time?.idle || 0) : previous?.rootIdle,
      });
    }
    if (revision === this.revision && !/monitor|task-notification/i.test(result.title) && !result.location.directory.includes('monitor')) {
      for (const questionID of result.questionIDs) {
        if (revision !== this.revision) break;
        void this.publish({ key: `question:${result.rootID}:${questionID}`, title: `Blocked: ${result.title}`, message: 'OpenCode is waiting for your answer to a question.' },
          () => revision === this.revision && this.cache.get(result.rootID)?.result.questionIDs.includes(questionID));
      }
      if (result.complete && this.completions.has(result.rootID) && revision === this.revision) {
        void this.publish({ key: result.completionKey, title: `Done: ${result.title}`, message: finalAssistantText(snapshot.lastAssistant) }, () => {
          const latest = this.cache.get(result.rootID)?.result;
          return revision === this.revision && latest?.complete === true && latest.completionKey === result.completionKey;
        }).then((settled) => {
          if (settled && revision === this.revision && this.cache.get(result.rootID)?.result.completionKey === result.completionKey) {
            this.completions.delete(result.rootID);
          }
        });
      }
    }
    return revision === this.revision ? result : refreshing(result);
  }

  async publish(message, isCurrent = () => true) {
    if (this.sent.has(message.key) || this.terminal.has(message.key)) return true;
    if ((this.retryAt.get(message.key) || 0) > this.now()) return false;
    if (this.publishing.has(message.key)) return this.publishing.get(message.key);
    const task = (async () => {
      try {
        const result = await this.notifier.publish(message, { isCurrent });
        const delivered = result.status === 'sent' || (result.status === 'duplicate' && result.reason === 'sent');
        if (delivered) this.sent.add(message.key);
        if (result.terminal) this.terminal.add(message.key);
        if (delivered || result.terminal) this.retryAt.delete(message.key);
        else if (result.retryAt) this.retryAt.set(message.key, result.retryAt);
        else if (['failed', 'disabled'].includes(result.status)) this.retryAt.set(message.key, this.now() + 30_000);
        return delivered || result.terminal === true;
      } catch (error) {
        this.retryAt.set(message.key, this.now() + 30_000);
        this.logger.warn('[task-lifecycle] notification failed', { type: error?.name || 'Error' });
        return false;
      }
    })();
    this.publishing.set(message.key, task);
    try { return await task; } finally { this.publishing.delete(message.key); }
  }

  async refresh() {
    const roots = new Set(this.touched);
    for (const [root, entry] of this.cache) {
      if (entry.result.activeCount || entry.result.pendingCount || entry.result.reason === 'awaiting-parent' ||
          entry.result.questionIDs.length || this.completions.has(root) || (this.interest.get(root) || 0) > this.now()) roots.add(root);
      else this.cache.delete(root);
    }
    // Snapshots are bounded internally; avoid multiplying their fan-out across roots.
    for (const root of roots) {
      try { await this.inspect(root, true); }
      catch (error) {
        this.logger.warn('[task-lifecycle] snapshot unavailable', { type: error?.name || 'Error' });
        if (error?._tag === 'SessionNotFoundError' || error?.status === 404) {
          this.touched.delete(root);
          this.cache.delete(root);
          this.completions.delete(root);
        } else {
          this.touched.add(root);
        }
      }
    }
  }
}

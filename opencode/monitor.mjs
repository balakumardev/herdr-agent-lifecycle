import { classifyTask, finalAssistantText } from './lifecycle.mjs';

export function lifecycleEvent(type) {
  return /^(?:session\.(?:execution\.|inbox\.|created$|deleted$|moved$|shell\.)|form\.|permission\.|shell\.|server\.connected$)/.test(type);
}

function refreshing(result) {
  const { completionKey, ...rest } = result;
  // Keep the v1.0.2 wire enum: old location registrations outlive shared-monitor
  // upgrades. reason=refreshing marks unavailable evidence, NOT an execution;
  // clients must retry instead of forwarding its legacy state value to Herdr.
  return { ...rest, state: 'working', complete: false, reason: 'refreshing' };
}

export class TaskMonitor {
  constructor({ load, notifier, now = Date.now, logger = console, previous }) {
    this.load = load;
    this.notifier = notifier;
    this.now = now;
    this.logger = logger;
    this.revision = 0;
    this.globalRevision = 0;
    this.rootRevisions = new Map();
    this.locationRevisions = new Map();
    this.sessionLocations = new Map();
    this.dependencies = new Map();
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
      previous.globalRevision = this.globalRevision = this.revision;
      for (const name of ['aliases', 'cache', 'interest', 'settled', 'shellOwners', 'shells', 'retryAt',
        'rootRevisions', 'locationRevisions', 'sessionLocations', 'dependencies']) {
        this[name] = new Map(previous[name] || []);
      }
      for (const name of ['touched', 'completions', 'sent', 'terminal']) {
        this[name] = new Set(previous[name] || []);
      }
    }
  }

  revisionFor(sessionID, directories) {
    const root = this.aliases.get(sessionID) || sessionID;
    return Math.max(this.globalRevision, this.rootRevisions.get(root) || 0,
      ...[...(directories || this.dependencies.get(root) || [])].map(directory => this.locationRevisions.get(directory) || 0));
  }

  observe(event) {
    if (!lifecycleEvent(event.type)) return;
    this.revision++;
    const data = event.data || {};
    let id = data.sessionID || data.form?.sessionID || data.info?.metadata?.sessionID ||
      (event.type.startsWith('shell.') && this.shells.get(data.id)?.metadata?.sessionID);
    if (event.type === 'shell.created' && id) {
      this.shellOwners.set(data.info.id, id);
      this.shells.set(data.info.id, { ...data.info, location: event.location });
    }
    if (event.type === 'shell.exited') {
      id = this.shellOwners.get(data.id) || id;
      this.shellOwners.delete(data.id);
      if (id) this.settled.set(id, Math.max(this.settled.get(id) || 0, event.created || this.now()));
      const shell = this.shells.get(data.id);
      if (shell) this.shells.set(data.id, { ...shell, status: data.status, time: { ...shell.time, completed: event.created || this.now() } });
    }
    if (event.type === 'session.shell.ended' && id) {
      this.settled.set(id, Math.max(this.settled.get(id) || 0, event.created || this.now()));
    }
    if (event.type === 'session.created' && typeof id === 'string') {
      // A new child can arrive after the tree was enumerated. Only a known
      // parent's ancestry or an explicitly parentless creation proves its root.
      const root = data.parentID ? this.aliases.get(data.parentID) : id;
      if (root) this.aliases.set(id, root);
    }
    const owner = this.aliases.get(id);
    const directory = data.location?.directory || event.location?.directory || this.sessionLocations.get(id);
    if (typeof directory === 'string') {
      const previous = this.sessionLocations.get(id);
      if (previous && previous !== directory) this.locationRevisions.set(previous, this.revision);
      if (id) this.sessionLocations.set(id, directory);
      this.locationRevisions.set(directory, this.revision);
    }
    if (owner && typeof directory === 'string') this.rootRevisions.set(owner, this.revision);
    // Reconnection or unresolved ownership/location still fences every task.
    // Another root can retain a shared runtime, so directory dependants must
    // also revalidate the absence proof used by retired-worktree reconciliation.
    else this.globalRevision = this.revision;
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
    const requiredRevision = this.revisionFor(key);
    if (!force && cached && cached.revision >= requiredRevision && this.now() - cached.time < 500) return cached.result;
    const current = this.inflight.get(key);
    if (current && current.revision >= requiredRevision) return current.promise;
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
    const dependencies = new Set([...snapshot.sessions.map(session => session.location?.directory),
      ...snapshot.shells.map(shell => shell.location?.directory)].filter(directory => typeof directory === 'string'));
    const fresh = () => revision >= this.revisionFor(snapshot.root.id, dependencies);
    let result = classifyTask(snapshot);
    if (!fresh()) {
      result = refreshing(result);
    } else {
      this.touched.delete(sessionID);
    }
    const previous = this.cache.get(result.rootID);
    if (previous && result.complete && snapshot.root.time.idle > previous.rootIdle) this.completions.add(result.rootID);
    if (!previous || previous.revision <= revision) {
      this.dependencies.set(result.rootID, dependencies);
      if (fresh()) for (const session of snapshot.sessions) {
        if (typeof session.location?.directory === 'string') this.sessionLocations.set(session.id, session.location.directory);
      }
      this.cache.set(result.rootID, {
        result, time: this.now(), revision,
        // An invalidated read is not a new authoritative recovery watermark.
        rootIdle: fresh() ? (snapshot.root.time?.idle || 0) : previous?.rootIdle,
      });
    }
    if (fresh() && !/monitor|task-notification/i.test(result.title) && !result.location.directory.includes('monitor')) {
      for (const questionID of result.questionIDs) {
        if (!fresh()) break;
        void this.publish({ key: `question:${result.rootID}:${questionID}`, title: `Blocked: ${result.title}`, message: 'OpenCode is waiting for your answer to a question.' },
          () => fresh() && this.cache.get(result.rootID)?.result.questionIDs.includes(questionID));
      }
      if (result.complete && this.completions.has(result.rootID) && fresh()) {
        void this.publish({ key: result.completionKey, title: `Done: ${result.title}`, message: finalAssistantText(snapshot.lastAssistant) }, () => {
          const latest = this.cache.get(result.rootID)?.result;
          return fresh() && latest?.complete === true && latest.completionKey === result.completionKey;
        }).then((settled) => {
          if (settled && fresh() && this.cache.get(result.rootID)?.result.completionKey === result.completionKey) {
            this.completions.delete(result.rootID);
          }
        });
      }
    }
    return fresh() ? result : refreshing(result);
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

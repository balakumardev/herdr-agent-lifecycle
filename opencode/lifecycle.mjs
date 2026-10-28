// Use only the latest root message supplied by loadTask; never search backward
// for a readable answer. Keep this body out of the public classification result.
export function finalAssistantText(message) {
  if (message?.type !== 'assistant' || message.finish !== 'stop' || !message.time?.completed || message.error ||
      !Array.isArray(message.content) ||
      message.content.some((part) => part.type === 'tool' && ['running', 'streaming'].includes(part.state?.status))) return;
  const text = message.content.filter((part) => part.type === 'text' && typeof part.text === 'string')
    .map((part) => part.text).join('\n\n').trim();
  if (!text) return;
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length <= 4000) return text;
  // Reserve three UTF-8 bytes for the ellipsis and stop before a partial code point.
  let end = 4000 - Buffer.byteLength('…', 'utf8');
  while ((bytes[end] & 0xc0) === 0x80) end--;
  return bytes.subarray(0, end).toString('utf8') + '…';
}

export function classifyTask(input) {
  const { root, sessions, lastAssistant } = input;
  const members = new Set(sessions.map((session) => session.id));
  const active = input.active.filter((id) => members.has(id));
  const forms = input.forms.filter((form) => members.has(form.sessionID));
  const permissions = input.permissions.filter((request) => members.has(request.sessionID));
  const pending = input.inbox.filter((item) => members.has(item.sessionID));
  const ownedShells = input.shells.filter((shell) => members.has(shell.metadata?.sessionID));
  const shells = ownedShells.filter((shell) => shell.status === 'running');
  const result = {
    rootID: root.id, title: root.title || 'OpenCode task', location: { directory: root.location.directory },
    state: 'working', complete: false, reason: 'running',
    activeCount: active.length, pendingCount: pending.length + shells.length,
    questionIDs: forms.map((form) => form.id).sort(),
  };
  if (forms.length || permissions.length) return { ...result, state: 'blocked', reason: forms.length ? 'question' : 'permission' };
  if (active.length || pending.length || shells.length) return result;
  if (root.outcome === 'failed' || root.outcome === 'interrupted') {
    return { ...result, state: 'blocked', reason: `execution-${root.outcome}` };
  }

  // A child finishing is not the parent finishing. The root must have answered
  // after the latest descendant settled, including when this plugin reloads.
  const settledAfter = Math.max(input.settledAfter || 0,
    ...ownedShells.filter((shell) => shell.status !== 'running').map((shell) => shell.time?.completed || 0), ...sessions
    .filter((session) => session.id !== root.id)
    .map((session) => session.time?.idle || session.time?.created || 0));
  const final = finalAssistantText(lastAssistant);
  if (root.outcome === 'succeeded' && final &&
      root.time?.idle >= settledAfter && lastAssistant.time.completed >= settledAfter) {
    return { ...result, state: 'idle', complete: true, reason: 'done', completionKey: `done:${root.id}:${lastAssistant.id}` };
  }
  if (!lastAssistant && !root.outcome && sessions.length === 1) return { ...result, state: 'idle', reason: 'new-session' };
  return { ...result, reason: 'awaiting-parent' };
}

async function mapBounded(values, work, width = 6) {
  const result = new Array(values.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(width, values.length) }, async () => {
    for (let index; (index = next++) < values.length;) result[index] = await work(values[index]);
  }));
  return result;
}

export async function loadTask(client, sessionID, { signal = AbortSignal.timeout(10_000), knownShells = [] } = {}) {
  const request = { signal };
  async function runtimeDirectories() {
    const locations = await client.debug.location.list(request);
    if (!Array.isArray(locations) || locations.some((location) => typeof location?.directory !== 'string')) {
      throw new Error('Invalid runtime location list');
    }
    return new Set(locations.map((location) => location.directory));
  }
  const known = new Map();
  async function get(id) {
    if (!known.has(id)) known.set(id, await client.session.get({ sessionID: id }, request));
    return known.get(id);
  }
  async function ancestor(id) {
    const seen = new Set();
    while (id) {
      if (seen.has(id) || seen.size >= 100) throw new Error('Invalid session ancestry');
      seen.add(id);
      const session = await get(id);
      if (!session.parentID) return session;
      id = session.parentID;
    }
    throw new Error('Missing session ancestry');
  }
  const root = await ancestor(sessionID);
  const tree = new Map([[root.id, root]]);
  let level = [root.id];
  while (level.length) {
    const children = (await mapBounded(level, async (parentID) => {
      const result = [];
      const cursors = new Set();
      let cursor;
      do {
        const page = await client.session.list({ parentID, limit: 100, ...(cursor ? { cursor } : {}) }, request);
        if (!Array.isArray(page.data)) throw new Error('Invalid session list');
        result.push(...page.data);
        if (result.length > 512) throw new Error('Task tree exceeds snapshot limit');
        if (!page.data.length || !page.cursor?.next) break;
        cursor = page.cursor.next;
        if (cursors.has(cursor)) throw new Error('Non-advancing session cursor');
        cursors.add(cursor);
      } while (true);
      return result;
    })).flat();
    level = [];
    for (const child of children) {
      if (tree.has(child.id)) continue;
      known.set(child.id, child);
      tree.set(child.id, child);
      level.push(child.id);
    }
    if (tree.size > 512) throw new Error('Task tree exceeds snapshot limit');
  }

  // The active map is process-wide. Resolve unfamiliar active ancestry as well:
  // a child can start while the paged family reads are in flight.
  const activeMap = await client.session.active(request);
  await mapBounded(Object.keys(activeMap), async (id) => {
    if ((await ancestor(id)).id !== root.id) return;
    for (let node = await get(id); node && !tree.has(node.id); node = known.get(node.parentID)) tree.set(node.id, node);
  });
  const sessions = [...tree.values()];
  // Forms, permissions and shells belong to live location services. Asking an
  // old session for them boots its location again, which fails after a temporary
  // worktree was removed. The registry bounds possible stores, but also includes
  // in-flight builds. Persisted history and inboxes are still read for every node.
  const runtimeBefore = await runtimeDirectories();
  const hasRuntime = (session) => runtimeBefore.has(session.location.directory);
  const directories = new Set(sessions.map((session) => session.location.directory));
  const excluded = new Set([...directories].filter((directory) => !runtimeBefore.has(directory)));
  // An unrelated active root can retain a detached shared graph that still
  // owns this task's background work. Ownership checks must be process-wide,
  // even though classification and activity counts remain task-scoped.
  const activeOwner = (directory, activity) => Object.keys(activity).some((id) => known.get(id)?.location.directory === directory);
  const failures = new Map();
  const observed = new Set();
  async function readRuntime(directory, read) {
    // Settle every issued read before checking whether a failed build was
    // evicted. One sibling request may otherwise still be acquiring the store.
    const [result] = await Promise.allSettled([Promise.resolve().then(read)]);
    if (result.status === 'rejected') failures.set(directory, result.reason);
    else observed.add(directory);
    return result;
  }
  if ([...excluded].some((directory) => activeOwner(directory, activeMap))) {
    throw new Error('Active task location is unavailable');
  }
  const records = await mapBounded(sessions, async (session) => {
    const input = { sessionID: session.id };
    const directory = session.location.directory;
    const [inbox, reads] = await Promise.all([
      client.session.inbox.list(input, request),
      hasRuntime(session) ? Promise.all([
        readRuntime(directory, () => client.session.form.list(input, request)),
        readRuntime(directory, () => client.permission.list(input, request)),
      ]) : [],
    ]);
    return { inbox, forms: reads[0]?.status === 'fulfilled' ? reads[0].value : [],
      permissions: reads[1]?.status === 'fulfilled' ? reads[1].value : [] };
  });
  const [messages, locations] = await Promise.all([
    client.message.list({ sessionID: root.id, type: 'assistant', order: 'desc', limit: 1 }, request),
    mapBounded([...directories].filter((directory) => runtimeBefore.has(directory)),
      (directory) => readRuntime(directory, () => client.shell.list({ location: { directory } }, request))),
  ]);
  signal.throwIfAborted();
  if (failures.size) {
    const [registered, activity] = await Promise.all([runtimeDirectories(), client.session.active(request)]);
    signal.throwIfAborted();
    if (Object.keys(activity).some((id) => !Object.hasOwn(activeMap, id))) throw new Error('Activity changed during snapshot');
    for (const [directory, error] of failures) {
      const unresolvedShell = knownShells.some((shell) => tree.has(shell.metadata?.sessionID) && shell.status === 'running' &&
        (shell.location || tree.get(shell.metadata.sessionID).location).directory === directory);
      // Only successful absence reads can retire a failed acquisition. HTTP
      // failure alone, mixed success, or owned work never means an empty store.
      if (registered.has(directory) || observed.has(directory) || activeOwner(directory, activeMap) ||
          activeOwner(directory, activity) || unresolvedShell) throw error;
      excluded.add(directory);
    }
  }
  const shells = new Map(locations.filter((result) => result.status === 'fulfilled')
    .flatMap(({ value }) => value.data.map((shell) => ({ ...shell, location: value.location })))
    .filter((shell) => tree.has(shell.metadata?.sessionID)).map((shell) => [shell.id, shell]));
  // shell.list is running-only. Refetch disappeared commands by their original
  // location so an SSE disconnect or session move cannot erase their finish time.
  await mapBounded(knownShells.filter((shell) => tree.has(shell.metadata?.sessionID) && !shells.has(shell.id)), async (shell) => {
    if (shell.status !== 'running') { shells.set(shell.id, shell); return; }
    const location = shell.location || tree.get(shell.metadata.sessionID).location;
    const result = await client.shell.get({ id: shell.id, location }, request);
    shells.set(shell.id, { ...result.data, location: result.location });
  });
  const [runtimeAfter, activeNow] = await Promise.all([runtimeDirectories(), client.session.active(request)]);
  signal.throwIfAborted();
  // Recheck after all volatile and known-shell reads, including newly retired
  // failed builds. Activity or a replacement store invalidates absence evidence.
  if (Object.keys(activeNow).some((id) => !Object.hasOwn(activeMap, id))) throw new Error('Activity changed during snapshot');
  if ([...excluded].some((directory) => runtimeAfter.has(directory))) throw new Error('Task runtime locations changed during snapshot');
  return {
    root, sessions, active: Object.keys(activeNow).filter((id) => tree.has(id)),
    inbox: records.flatMap((record) => record.inbox), forms: records.flatMap((record) => record.forms),
    permissions: records.flatMap((record) => record.permissions),
    shells: [...shells.values()],
    lastAssistant: messages.data[0],
  };
}

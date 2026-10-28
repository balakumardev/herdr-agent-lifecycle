import { OpenCode } from '@opencode/client';
import { Service } from '@opencode/client/service';
import { setTimeout as delay } from 'node:timers/promises';
import { LifecycleRPC } from './contract.mjs';
import { loadTask } from './lifecycle.mjs';
import { TaskMonitor } from './monitor.mjs';
import { createNotifier } from './notify.mjs';

// Plugins load once per location. The HTTP/event monitor must instead be owned
// once by this service process, regardless of the number of open worktrees.
const sharedKey = Symbol.for('local.opencode.task-lifecycle.v3');
const build = '1.0.2';
// Increase when the process-owned implementation changes within a release.
const revision = 3;

function acquire() {
  let shared = globalThis[sharedKey];
  if (!shared) {
    shared = { refs: 0, controller: new AbortController(), monitor: undefined, build, revision };
    globalThis[sharedKey] = shared;
    void connect(shared);
  } else if ((shared.revision || 0) < revision) {
    // One watched plugin reload upgrades the process-owned monitor, even while
    // other locations retain references. Keep pending observations through reload.
    shared.controller.abort();
    shared.controller = new AbortController();
    shared.monitor = shared.monitor ? new TaskMonitor({
      previous: shared.monitor, load: shared.monitor.load, notifier: createNotifier(),
    }) : undefined;
    shared.build = build;
    shared.revision = revision;
    void connect(shared);
  }
  shared.refs++;
  return shared;
}

async function connect(shared) {
  const { signal } = shared.controller;
  const notifier = createNotifier();
  let interval;
  let refreshing = false;
  while (!signal.aborted) {
    try {
      // Discovery never starts or restarts OpenCode. Defer it until after setup:
      // waiting for the server's health endpoint during its setup would deadlock.
      const endpoint = await Service.discover();
      if (!endpoint) throw new Error('ServiceNotReady');
      const client = OpenCode.make({ baseUrl: endpoint.url, headers: Service.headers(endpoint) });
      const info = await client.server.info({ signal });
      if (info.pid !== process.pid) throw new Error('DifferentServiceProcess');
      const load = (id, options) => loadTask(client, id, { ...options, signal: AbortSignal.any([signal, AbortSignal.timeout(10_000)]) });
      shared.monitor ||= new TaskMonitor({ load, notifier });
      shared.monitor.load = load;
      shared.monitor.observe({ type: 'server.connected' });
      const refresh = () => {
        if (refreshing || signal.aborted) return;
        refreshing = true;
        void shared.monitor.refresh().finally(() => { refreshing = false; });
      };
      interval = setInterval(refresh, 1500);
      interval.unref?.();
      // Native subscribers share one stream per client. Begin consumption before
      // the initial active-session hydration so subsequent events cannot be lost.
      const seed = (async () => {
        const active = await client.session.active({ signal });
        for (const id of Object.keys(active)) {
          if (signal.aborted) break;
          await shared.monitor.inspect(id).catch(() => {});
        }
      })().catch(() => {});
      for await (const event of client.event.subscribe({ signal })) shared.monitor.observe(event);
      await seed;
    } catch (error) {
      if (!signal.aborted) console.warn('[task-lifecycle] event monitor reconnecting', { type: error?.name || 'Error' });
    } finally {
      clearInterval(interval);
    }
    if (!signal.aborted) await delay(1500, undefined, { signal }).catch(() => {});
  }
}

export default {
  id: 'local.task-lifecycle',
  async setup(ctx) {
    const shared = acquire();
    try {
      await ctx.rpc.register(LifecycleRPC, {
        health: async () => ({ version: shared.build, revision: shared.revision, connected: !!shared.monitor, references: shared.refs }),
        snapshot: async ({ sessionID }) => {
          if (!shared.monitor) throw new Error('Lifecycle monitor is connecting');
          return shared.monitor.inspect(sessionID);
        },
      });
    } catch (error) {
      release(shared);
      throw error;
    }
    return () => release(shared);
  },
};

function release(shared) {
  if (--shared.refs !== 0) return;
  shared.controller.abort();
  if (globalThis[sharedKey] === shared) delete globalThis[sharedKey];
}

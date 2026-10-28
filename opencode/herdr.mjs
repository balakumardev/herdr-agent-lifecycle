import { randomUUID } from "node:crypto";
import net from "node:net";

const SOURCE = "custom:opencode-v2-lifecycle";
const AGENT = "opencode-v2";
// Survives module reloads in this TUI process. Each pane has exactly one owner;
// replacing it aborts old sockets and fences queued callbacks.
const registry = globalThis[Symbol.for(`${SOURCE}:reporters`)] ??= { sequence: 0, owners: new Map() };

function request(socketPath, payload, { signal, isCurrent, timeoutMs }) {
  return new Promise((resolve) => {
    let settled = false;
    let buffer = "";
    let bytes = 0;
    const socket = net.createConnection(socketPath);
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal.removeEventListener("abort", aborted);
      socket.destroy();
      resolve(ok);
    };
    const aborted = () => finish(false);
    // Unlike socket.setTimeout, this also bounds a connection that never opens.
    const timer = setTimeout(() => finish(false), timeoutMs);
    timer.unref?.();
    socket.setEncoding("utf8");
    socket.on("connect", () => {
      if (signal.aborted || !isCurrent()) return finish(false);
      socket.write(`${JSON.stringify(payload)}\n`);
    });
    socket.on("data", (chunk) => {
      bytes += Buffer.byteLength(chunk);
      if (bytes > 65_536) return finish(false);
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      try {
        const ack = JSON.parse(buffer.slice(0, newline));
        finish(ack?.id === payload.id && ack.error == null && ack.result?.type === "ok");
      } catch {
        finish(false);
      }
    });
    socket.on("error", () => finish(false));
    socket.on("end", () => finish(false));
    socket.on("close", () => finish(false));
    signal.addEventListener("abort", aborted, { once: true });
    if (signal.aborted) aborted();
  });
}

export function createHerdrReporter(env = process.env, { timeoutMs = 750, command = loadConfig({ env }).opencode.command } = {}) {
  // Capture the TUI's environment once, never the shared server's environment.
  const paneID = env.HERDR_PANE_ID;
  const socketPath = env.HERDR_SOCKET_PATH;
  if (env.HERDR_ENV !== "1" || !paneID || !socketPath) {
    return { enabled: false, report: async () => false, dispose() {} };
  }
  const ownerKey = JSON.stringify([socketPath, paneID]);
  const lifetime = new AbortController();
  registry.owners.get(ownerKey)?.abort();
  registry.owners.set(ownerKey, lifetime);
  let acknowledged;
  let queue = Promise.resolve();

  return {
    enabled: true,
    report(snapshot, { signal, isCurrent = () => true } = {}) {
      const current = () => !lifetime.signal.aborted && !signal?.aborted &&
        registry.owners.get(ownerKey) === lifetime && isCurrent();
      const delivery = queue.then(async () => {
        if (!current() || !["working", "blocked", "idle", "unknown"].includes(snapshot?.state) ||
            (!snapshot.rootID && snapshot.state !== "unknown")) return false;
        const key = JSON.stringify([snapshot.rootID, snapshot.state]);
        if (key === acknowledged) return true;
        // A lost acknowledgement does not prove Herdr ignored the write. Until
        // a new write is acknowledged even the previous state must be resent.
        acknowledged = undefined;
        const seq = registry.sequence = Math.max(registry.sequence + 1, Date.now() * 1000);
        const ok = await request(socketPath, {
          id: randomUUID(),
          method: "pane.report_agent",
          params: {
            pane_id: paneID,
            source: SOURCE,
            agent: AGENT,
            seq,
            state: snapshot.state,
            ...(snapshot.rootID ? {
              agent_session_id: snapshot.rootID,
              resume_argv: [command, "--session", snapshot.rootID],
            } : {}),
            message: snapshot.reason,
          },
        }, {
          signal: signal ? AbortSignal.any([lifetime.signal, signal]) : lifetime.signal,
          isCurrent: current,
          timeoutMs,
        });
        if (!ok || !current()) return false;
        acknowledged = key;
        return true;
      });
      queue = delivery.catch(() => false);
      return queue;
    },
    dispose() {
      lifetime.abort();
      if (registry.owners.get(ownerKey) === lifetime) registry.owners.delete(ownerKey);
      // Deliberately do not release or synthesize idle. Herdr 0.9.3's release
      // compares only shared source/agent, not this instance or session. A late
      // release could clear a replacement TUI's authority (including another
      // process). Only an authoritative snapshot may declare a root idle.
    },
  };
}
import { loadConfig } from './config.mjs';

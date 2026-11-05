import { randomUUID } from "node:crypto";
import net from "node:net";

const SOURCE = "custom:opencode-background-work";
// Release 1.0.3 held panes under this source and never released; clearing it frees panes it left on `unknown`.
const LEGACY_SOURCE = "custom:opencode-v2-lifecycle";
const LEGACY_AGENT = "opencode-v2";
// Must equal the label Herdr detects, otherwise the sidebar label flips while the pane is held.
const AGENT = "opencode";
// Shared across plugin hot reloads in this process: only the newest instance may touch the pane.
const shared = globalThis[Symbol.for(SOURCE)] ??= { seq: 0, owner: undefined };

// One JSON-line request per connection; ok only on an `ok` acknowledgement, code is Herdr's error code if it sent one.
function request(socketPath, method, params, timeoutMs) {
  return new Promise((resolve) => {
    const id = randomUUID();
    let buffer = "";
    const socket = net.createConnection(socketPath);
    const finish = (ok, code) => { clearTimeout(timer); socket.destroy(); resolve({ ok, code }); };
    const timer = setTimeout(() => finish(false), timeoutMs);
    timer.unref?.();
    socket.setEncoding("utf8");
    socket.on("connect", () => socket.write(`${JSON.stringify({ id, method, params })}\n`));
    socket.on("data", (chunk) => {
      buffer += chunk;
      const end = buffer.indexOf("\n");
      if (end < 0) return;
      try {
        const ack = JSON.parse(buffer.slice(0, end));
        finish(ack.id === id && ack.error == null && ack.result?.type === "ok", ack.error?.code);
      } catch { finish(false); }
    });
    socket.on("error", () => finish(false));
    socket.on("close", () => finish(false));
  });
}

// Returns undefined outside a Herdr pane. env is a parameter so tests never reach a real pane.
export function createHerdr(env, { timeoutMs = 750 } = {}) {
  const { HERDR_ENV, HERDR_PANE_ID: pane, HERDR_SOCKET_PATH: socketPath } = env;
  if (HERDR_ENV !== "1" || !pane || !socketPath) return;
  const owner = shared.owner = {};
  let queue = Promise.resolve();

  // A Herdr that does not know clear_agent_authority answers invalid_request; release_agent is then used for the rest of the process.
  let clearUnsupported = false;

  // Serialized so a late hold can never land after a release. `work` gets a call(method, params) that adds a fresh,
  // increasing seq and resolves { ok, code }.
  function send(work) {
    const result = queue.then(() => {
      if (shared.owner !== owner) return false;
      return work((method, params) => {
        shared.seq = Math.max(shared.seq + 1, Date.now() * 1000);
        return request(socketPath, method, { ...params, seq: shared.seq }, timeoutMs);
      });
    });
    queue = result.catch(() => false);
    return queue;
  }

  // clear_agent_authority hands the pane back to screen detection and keeps Herdr's agent identity.
  // pane.release_agent drops the identity and leaves the pane `unknown` for ~3 s while Herdr re-detects.
  function clear(source, agent) {
    return send(async (call) => {
      if (!clearUnsupported) {
        const result = await call("pane.clear_agent_authority", { pane_id: pane, source });
        if (result.code !== "invalid_request") return result.ok;
        clearUnsupported = true;
      }
      return (await call("pane.release_agent", { pane_id: pane, source, agent })).ok;
    });
  }

  return {
    hold: (state, message) => send(async (call) => (await call("pane.report_agent",
      { pane_id: pane, source: SOURCE, agent: AGENT, state, message })).ok),
    release: () => clear(SOURCE, AGENT),
    clearLegacy: () => clear(LEGACY_SOURCE, LEGACY_AGENT),
  };
}

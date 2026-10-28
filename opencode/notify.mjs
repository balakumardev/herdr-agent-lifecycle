import { createHash, randomUUID } from "node:crypto";
import { access, appendFile, mkdir, open, readFile, readdir, readlink, rename, rm, symlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

// Retain observed server deadlines if a metadata write fails. A later call
// repairs the durable record before admission; plugin reloads share this guard.
const writebacksKey = Symbol.for("local.opencode.task-lifecycle.endpoint-writebacks.v1");
const writebacks = globalThis[writebacksKey] ||= new Map();

async function exists(path) {
  try { await access(path); return true; }
  catch (error) { if (error.code === "ENOENT") return false; throw error; }
}

async function readState(path) {
  try { return JSON.parse(await readFile(path, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return {}; throw error; }
}

async function retryState(directory) {
  let files;
  try { files = await readdir(directory); }
  catch (error) { if (error.code === "ENOENT") return {}; throw error; }
  let latest = -1;
  for (const name of files) {
    const match = /^claim-(\d+)\.result\.json$/.exec(name);
    if (match && Number.isSafeInteger(Number(match[1]))) latest = Math.max(latest, Number(match[1]));
  }
  // Immutable claim-numbered results fence late writers: an expired publisher
  // can never overwrite a newer owner's cooldown or success reset.
  return readState(join(directory, latest >= 0 ? `claim-${latest}.result.json` : "retry.json"));
}

async function processIdentity(pid) {
  try {
    const [boot, stat] = await Promise.all([
      readFile("/proc/sys/kernel/random/boot_id", "utf8"), readFile(`/proc/${pid}/stat`, "utf8"),
    ]);
    const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    return { alive: !["Z", "X"].includes(fields[0]), identity: `${boot.trim()}:${fields[19]}` };
  } catch {
    try { process.kill(pid, 0); return { alive: true }; }
    catch (error) { return { alive: error.code !== "ESRCH" }; }
  }
}

async function claim(directory, leaseMs) {
  const own = await processIdentity(process.pid);
  const cursor = await readState(join(directory, "cursor.json"));
  // Slots are immutable. Never unlink/reuse a stale lock: two reclaimers could
  // otherwise delete a new owner's lock between comparing and unlinking it.
  for (let slot = Number.isSafeInteger(cursor.slot) && cursor.slot >= 0 ? cursor.slot : 0; ; slot++) {
    if (await exists(join(directory, "sent.json"))) return { reason: "sent" };
    const path = join(directory, `claim-${slot}`);
    if (await exists(`${path}.released`)) continue;
    const owner = { pid: process.pid, identity: own.identity, expires: Date.now() + leaseMs };
    try {
      await symlink(JSON.stringify(owner), path);
      // This is a forward-scan hint, not lock ownership. Older hints are safe.
      await saveJSON(join(directory, "cursor.json"), { slot });
      return { path, expires: owner.expires, slot };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
    }
    let previous;
    try { previous = JSON.parse(await readlink(path)); }
    catch (error) {
      if (error instanceof SyntaxError || ["ENOENT", "EINVAL"].includes(error.code)) continue;
      throw error;
    }
    if (!Number.isSafeInteger(previous.pid) || previous.pid <= 0 || !(previous.expires > Date.now())) continue;
    const current = await processIdentity(previous.pid);
    if (current.alive && (!previous.identity || !current.identity || previous.identity === current.identity)) {
      return { reason: "inflight" };
    }
  }
}

async function saveJSON(path, receipt) {
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    const file = await open(temporary, "wx", 0o600);
    try { await file.writeFile(JSON.stringify(receipt)); await file.sync(); }
    finally { await file.close(); }
    await rename(temporary, path);
    const directory = await open(dirname(path), "r");
    try { await directory.sync(); } finally { await directory.close(); }
  } finally { await rm(temporary, { force: true }); }
}

function retryAfter(value, now) {
  if (typeof value !== "string") return 0;
  const text = value.trim();
  const at = /^\d+$/.test(text) ? now + Number(text) * 1000
    : /^[A-Za-z]{3},/.test(text) ? Date.parse(text) : 0;
  return Number.isSafeInteger(at) && at > now ? at : 0;
}

/** Transport only: callers supply the lifecycle decision and stable dedup key. */
export function createNotifier({
  config = loadConfig(),
  stateDirectory = join(process.env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "opencode", "task-lifecycle"),
  topicFile,
  topic: configuredTopic = config.notifications.enabled ? config.notifications.topic : undefined,
  endpoint = config.notifications.endpoint,
  fetchImpl = globalThis.fetch,
  timeoutMs = 5000,
  retryBaseMs = 30_000,
  retryMaxMs = 900_000,
  rateLimitMs = 60_000,
  leaseMs = timeoutMs + 60_000,
  now = Date.now,
  random = Math.random,
} = {}) {
  for (const [name, value, minimum, maximum] of [
    ["timeoutMs", timeoutMs, 1, 60_000], ["retryBaseMs", retryBaseMs, 1, 3_600_000],
    ["retryMaxMs", retryMaxMs, 1, 86_400_000], ["rateLimitMs", rateLimitMs, 1, 86_400_000],
    ["leaseMs", leaseMs, 1, 3_600_000],
  ]) {
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new TypeError(`Invalid ${name}`);
  }
  let base;
  try { base = new URL(endpoint); } catch { throw new TypeError("Invalid notification endpoint"); }
  if (!["https:", "http:"].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
    throw new TypeError("Invalid notification endpoint");
  }
  if (typeof fetchImpl !== "function" || typeof now !== "function" || typeof random !== "function") throw new TypeError("Invalid callback");
  if (retryMaxMs < retryBaseMs) throw new TypeError("retryMaxMs must cover retryBaseMs");
  const endpointHash = createHash("sha256").update(base.origin).digest("hex");
  const endpointDirectory = join(stateDirectory, `endpoint-${endpointHash}`);

  async function checkpointEndpoint(lease, state) {
    const pending = { slot: lease.slot, retryAt: state.retryAt || 0, path: `${lease.path}.result.json`, state, dirty: true };
    if (!writebacks.has(endpointDirectory) || writebacks.get(endpointDirectory).slot <= lease.slot) writebacks.set(endpointDirectory, pending);
    await saveJSON(pending.path, state);
    pending.dirty = false;
  }

  async function log(receipt) {
    try {
      await mkdir(stateDirectory, { recursive: true, mode: 0o700 });
      await appendFile(join(stateDirectory, "receipts.jsonl"), JSON.stringify(receipt) + "\n", { mode: 0o600 });
      return receipt;
    } catch { return { ...receipt, logFailed: true }; }
  }

  async function send(url, title, message) {
    const controller = new AbortController();
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(() => fetchImpl(url, {
          method: "POST", redirect: "manual", signal: controller.signal,
          headers: { Title: title, Priority: "3", "Content-Type": "text/plain; charset=utf-8" }, body: message,
        })).then((response) => {
          // Acceptance is determined by status; never retain/log private response bodies.
          void response.body?.cancel().catch(() => {});
          return { httpStatus: response.status, reason: response.ok ? undefined : "http",
            retryAfter: retryAfter(response.headers?.get("retry-after"), now()) };
        }),
        new Promise((resolve) => {
          timer = setTimeout(() => { controller.abort(); resolve({ reason: "timeout" }); }, timeoutMs);
        }),
      ]);
    } catch { return { reason: controller.signal.aborted ? "timeout" : "network" }; }
    finally { clearTimeout(timer); }
  }

  return {
    async publish({ key, title, message } = {}, { isCurrent = () => true } = {}) {
      const keyHash = createHash("sha256").update(typeof key === "string" ? key : "").digest("hex");
      const receipt = (status, details = {}) => ({ keyHash, time: new Date(now()).toISOString(), event: "publish", status, ...details });
      let owned;
      let endpointClaim;
      let result;
      let record = true;
      let observedRetryAt = 0;
      let releaseFailed = false;
      try {
        result = await (async () => {
          if (!key || typeof key !== "string" || typeof title !== "string" || typeof message !== "string") {
            return receipt("failed", { reason: "invalid_input" });
          }
          let topic = configuredTopic;
          if (topicFile !== undefined) {
            let source;
            try { source = await readFile(topicFile, "utf8"); }
            catch (error) { if (error.code === "ENOENT") return receipt("disabled", { reason: "no_topic" }); throw error; }
            const match = source.match(/^\s*TOPIC=(?:"([A-Za-z0-9_-]+)"|'([A-Za-z0-9_-]+)'|([A-Za-z0-9_-]+))\s*(?:#.*)?$/m);
            topic = match?.[1] || match?.[2] || match?.[3];
          }
          if (typeof topic !== "string" || !/^[A-Za-z0-9_-]{1,256}$/.test(topic)) return receipt("disabled", { reason: "no_topic" });
          const directory = join(stateDirectory, keyHash);
          const check = async () => {
            const buffered = writebacks.get(endpointDirectory);
            if (buffered?.dirty) {
              observedRetryAt = Math.max(observedRetryAt, buffered.retryAt);
              try { await saveJSON(buffered.path, buffered.state); buffered.dirty = false; }
              catch { /* The in-process guard retains the observed deadline. */ }
            }
            const [pending, endpointState] = await Promise.all([retryState(directory), retryState(endpointDirectory)]);
            if (pending.terminal) {
              record = false;
              return receipt("failed", { reason: pending.reason, httpStatus: pending.httpStatus, terminal: true, attempts: pending.attempts });
            }
            const retryAt = Math.max(pending.retryAt || 0, endpointState.retryAt || 0, writebacks.get(endpointDirectory)?.retryAt || 0);
            if (retryAt > now()) {
              record = false;
              return receipt("deferred", { reason: "cooldown", retryAt });
            }
          };
          // Success wins over a later, unrelated endpoint cooldown.
          if (await exists(join(directory, "sent.json"))) return receipt("duplicate", { reason: "sent" });
          let paused = await check();
          if (paused) return paused;
          await mkdir(directory, { recursive: true, mode: 0o700 });
          owned = await claim(directory, leaseMs);
          if (!owned.path) {
            record = owned.reason === "sent";
            return receipt("duplicate", { reason: owned.reason, ...(owned.reason === "inflight" ? { retryAt: now() + 1500 } : {}) });
          }
          await mkdir(endpointDirectory, { recursive: true, mode: 0o700 });
          endpointClaim = await claim(endpointDirectory, leaseMs);
          if (!endpointClaim.path) {
            record = false;
            return receipt("deferred", { reason: "endpoint_inflight", retryAt: now() + 1500 });
          }
          // Another process may have written a cooldown while we claimed.
          paused = await check();
          if (paused) return paused;
          if (!isCurrent()) { record = false; return receipt("cancelled", { reason: "state_changed" }); }
          if (Date.now() >= Math.min(owned.expires, endpointClaim.expires)) return receipt("failed", { reason: "claim_expired", retryAt: now() + retryBaseMs });
          const [pending, endpointState] = await Promise.all([retryState(directory), retryState(endpointDirectory)]);
          let header = title.replace(/[\r\n\0]/g, " ").slice(0, 100);
          if (/[^\x20-\x7e]/.test(header)) header = `=?UTF-8?B?${Buffer.from(header).toString("base64")}?=`;
          if (!isCurrent()) { record = false; return receipt("cancelled", { reason: "state_changed" }); }
          if (Date.now() >= Math.min(owned.expires, endpointClaim.expires)) return receipt("failed", { reason: "claim_expired", retryAt: now() + retryBaseMs });
          const { retryAfter: minimumRetryAt = 0, ...response } = await send(`${base.href.replace(/\/$/, "")}/${topic}`, header, message);
          const expired = Date.now() >= Math.min(owned.expires, endpointClaim.expires);
          const attempts = (pending.attempts || 0) + 1;
          const current = receipt(response.reason ? "failed" : "sent", { ...response, attempts });
          if (!response.reason) {
            // HTTP acceptance is durable even when the owner was suspended past
            // its lease. Only the shared endpoint reset requires current ownership.
            await saveJSON(join(directory, "sent.json"), current);
            if (!expired) await checkpointEndpoint(endpointClaim, {});
            return current;
          }
          if (expired) return receipt("failed", { reason: "claim_expired", retryAt: now() + retryBaseMs });
          const transient = response.reason !== "http" || response.httpStatus === 408 || response.httpStatus === 429 || response.httpStatus >= 500;
          if (!transient) {
            current.terminal = true;
            await checkpointEndpoint(endpointClaim, endpointState);
          } else {
            const failures = Math.max(attempts, (endpointState.failures || 0) + 1);
            const backoff = Math.min(retryMaxMs, retryBaseMs * 2 ** Math.min(failures - 1, 30));
            const wait = Math.max(response.httpStatus === 429 ? rateLimitMs : 0, backoff);
            current.retryAt = Math.max(minimumRetryAt, now() + Math.ceil(wait * (1 + 0.2 * random())));
            observedRetryAt = current.retryAt;
            await checkpointEndpoint(endpointClaim, { failures, retryAt: current.retryAt });
          }
          await saveJSON(`${owned.path}.result.json`, current);
          return current;
        })();
      } catch { result = receipt("failed", { reason: "storage", retryAt: Math.max(now() + retryBaseMs, observedRetryAt) }); }
      finally {
        for (const lease of [endpointClaim, owned]) if (lease?.path) {
          try { await writeFile(`${lease.path}.released`, "", { mode: 0o600 }); }
          catch { releaseFailed = true; }
        }
      }
      const recorded = record ? await log(result) : result;
      return releaseFailed ? { ...recorded, releaseFailed: true } : recorded;
    },
  };
}
import { loadConfig } from './config.mjs';

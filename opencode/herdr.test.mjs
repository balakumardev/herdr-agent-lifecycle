import assert from "node:assert/strict";
import { test } from "node:test";
import { createHerdr } from "./herdr.mjs";
import { quiet, socketFixture } from "./fixtures.mjs";

test("is disabled outside a Herdr pane", () => {
  const env = { HERDR_ENV: "1", HERDR_PANE_ID: "pane-one", HERDR_SOCKET_PATH: "/unused" };
  assert.equal(createHerdr({}), undefined);
  assert.equal(createHerdr({ ...env, HERDR_ENV: "0" }), undefined);
  assert.equal(createHerdr({ ...env, HERDR_PANE_ID: "" }), undefined);
  assert.equal(createHerdr({ ...env, HERDR_SOCKET_PATH: undefined }), undefined);
});

test("hold reports the state for the opencode label under the overlay source, with no session data", async (t) => {
  const { requests, env } = await socketFixture(t);
  const herdr = createHerdr(env);
  assert.equal(await herdr.hold("working", "background work running"), true);
  assert.equal(requests.length, 1);
  const { id, method, params } = requests[0];
  assert.equal(typeof id, "string");
  assert.equal(method, "pane.report_agent");
  assert.ok(Number.isSafeInteger(params.seq));
  assert.deepEqual({ ...params, seq: undefined }, {
    pane_id: "pane-one", source: "custom:opencode-background-work", agent: "opencode",
    seq: undefined, state: "working", message: "background work running",
  });
});

test("release clears only this source's authority, without dropping Herdr's agent identity", async (t) => {
  const { requests, env } = await socketFixture(t);
  const herdr = createHerdr(env);
  assert.equal(await herdr.release(), true);
  const { method, params } = requests[0];
  assert.equal(method, "pane.clear_agent_authority");
  assert.deepEqual({ ...params, seq: undefined }, {
    pane_id: "pane-one", source: "custom:opencode-background-work", seq: undefined,
  });
});

test("clearLegacy clears only release 1.0.3's source", async (t) => {
  const { requests, env } = await socketFixture(t);
  assert.equal(await createHerdr(env).clearLegacy(), true);
  const { method, params } = requests[0];
  assert.equal(method, "pane.clear_agent_authority");
  assert.deepEqual({ ...params, seq: undefined }, {
    pane_id: "pane-one", source: "custom:opencode-v2-lifecycle", seq: undefined,
  });
});

test("seq strictly increases across calls and across instances", async (t) => {
  const { requests, env } = await socketFixture(t);
  const first = createHerdr(env);
  await first.hold("working", "a");
  await first.release();
  const second = createHerdr(env);
  await second.hold("blocked", "b");
  await second.release();
  const seqs = requests.map((request) => request.params.seq);
  assert.equal(seqs.length, 4);
  for (let i = 1; i < seqs.length; i++) assert.ok(seqs[i] > seqs[i - 1], `seq ${seqs[i]} must exceed ${seqs[i - 1]}`);
});

test("a delivery failure resolves false instead of throwing", async (t) => {
  const refused = await socketFixture(t, (request, socket) => socket.end(`${JSON.stringify({ id: request.id, error: "no" })}\n`));
  assert.equal(await createHerdr(refused.env).hold("working", "x"), false);
  const wrongId = await socketFixture(t, (request, socket) => socket.end(`${JSON.stringify({ id: "other", result: { type: "ok" } })}\n`));
  assert.equal(await createHerdr(wrongId.env).hold("working", "x"), false);
  const garbage = await socketFixture(t, (request, socket) => socket.end("not json\n"));
  assert.equal(await createHerdr(garbage.env).hold("working", "x"), false);
  const missing = createHerdr({ HERDR_ENV: "1", HERDR_PANE_ID: "p", HERDR_SOCKET_PATH: "/nonexistent/herdr.sock" });
  assert.equal(await missing.hold("working", "x"), false);
});

test("a server that never answers is abandoned after the timeout", async (t) => {
  const { env } = await socketFixture(t, () => {});
  const started = Date.now();
  assert.equal(await createHerdr(env, { timeoutMs: 80 }).hold("working", "x"), false);
  assert.ok(Date.now() - started < 1_000);
});

test("requests are serialized: a release queued behind a slow hold arrives after it", async (t) => {
  const { requests, env } = await socketFixture(t, (request, socket) => {
    const answer = () => socket.end(`${JSON.stringify({ id: request.id, result: { type: "ok" } })}\n`);
    if (request.method === "pane.report_agent") setTimeout(answer, 80); else answer();
  });
  const herdr = createHerdr(env);
  const hold = herdr.hold("working", "x");
  const release = herdr.release();
  await quiet(30);
  assert.deepEqual(requests.map((request) => request.method), ["pane.report_agent"]);
  assert.deepEqual(await Promise.all([hold, release]), [true, true]);
  assert.deepEqual(requests.map((request) => request.method), ["pane.report_agent", "pane.clear_agent_authority"]);
});

test("a superseded instance sends nothing, so a hot-reloaded predecessor cannot clear its successor", async (t) => {
  const { requests, env } = await socketFixture(t);
  const old = createHerdr(env);
  const current = createHerdr(env);
  assert.equal(await old.release(), false);
  assert.equal(await old.hold("working", "x"), false);
  assert.equal(requests.length, 0);
  assert.equal(await current.hold("working", "x"), true);
  assert.equal(requests.length, 1);
});

const answer = (request, socket, error) => socket.end(`${JSON.stringify(error
  ? { id: request.id, error: { code: error, message: error } } : { id: request.id, result: { type: "ok" } })}\n`);
// A Herdr that does not know clear_agent_authority, like an older or newer release might not.
const withoutClear = (request, socket) => answer(request, socket, request.method === "pane.clear_agent_authority" ? "invalid_request" : undefined);

test("falls back to release_agent when Herdr rejects clear_agent_authority, and stays on it", async (t) => {
  const { requests, env } = await socketFixture(t, withoutClear);
  const herdr = createHerdr(env);
  assert.equal(await herdr.release(), true);
  assert.deepEqual(requests.map((request) => request.method), ["pane.clear_agent_authority", "pane.release_agent"]);
  const { params } = requests[1];
  assert.deepEqual({ ...params, seq: undefined }, {
    pane_id: "pane-one", source: "custom:opencode-background-work", agent: "opencode", seq: undefined,
  });
  assert.ok(params.seq > requests[0].params.seq, "each attempt carries a fresh seq");
  requests.length = 0;
  assert.equal(await herdr.release(), true);
  assert.deepEqual(requests.map((request) => request.method), ["pane.release_agent"], "the unsupported method is not retried");
  assert.equal(await herdr.hold("working", "x"), true, "holding is unaffected");
});

test("the legacy clear falls back with the legacy agent label", async (t) => {
  const { requests, env } = await socketFixture(t, withoutClear);
  assert.equal(await createHerdr(env).clearLegacy(), true);
  assert.deepEqual(requests.map((request) => request.method), ["pane.clear_agent_authority", "pane.release_agent"]);
  assert.deepEqual({ ...requests[1].params, seq: undefined }, {
    pane_id: "pane-one", source: "custom:opencode-v2-lifecycle", agent: "opencode-v2", seq: undefined,
  });
});

test("a failed fallback reports false instead of throwing", async (t) => {
  const { requests, env } = await socketFixture(t, (request, socket) => answer(request, socket, "invalid_request"));
  assert.equal(await createHerdr(env).release(), false);
  assert.deepEqual(requests.map((request) => request.method), ["pane.clear_agent_authority", "pane.release_agent"]);
});

test("only an unknown-method style rejection switches methods; other failures keep clear_agent_authority", async (t) => {
  const missing = await socketFixture(t, (request, socket) => answer(request, socket, "pane_not_found"));
  const herdr = createHerdr(missing.env);
  assert.equal(await herdr.release(), false);
  assert.equal(await herdr.release(), false);
  assert.deepEqual(missing.requests.map((request) => request.method), ["pane.clear_agent_authority", "pane.clear_agent_authority"]);
  const silent = await socketFixture(t, () => {});
  const timedOut = createHerdr(silent.env, { timeoutMs: 60 });
  assert.equal(await timedOut.release(), false);
  assert.equal(await timedOut.release(), false);
  assert.deepEqual(silent.requests.map((request) => request.method), ["pane.clear_agent_authority", "pane.clear_agent_authority"]);
});

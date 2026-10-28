import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { join } from "node:path";
import { test } from "node:test";
import { createHerdrReporter } from "./herdr.mjs";

const working = { rootID: "ses_root", state: "working", reason: "child-active" };

async function socketFixture(t, reply = (request, socket) => {
  socket.end(`${JSON.stringify({ id: request.id, result: { type: "ok" } })}\n`);
}) {
  const directory = await mkdtemp("/tmp/opencode/lifecycle-herdr-");
  const path = join(directory, "s");
  const requests = [];
  const sockets = new Set();
  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.on("error", () => {});
    socket.on("close", () => sockets.delete(socket));
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk;
      if (!buffer.includes("\n")) return;
      const request = JSON.parse(buffer.slice(0, buffer.indexOf("\n")));
      requests.push(request);
      reply(request, socket);
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(path, resolve);
  });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  return { requests, env: { HERDR_ENV: "1", HERDR_SOCKET_PATH: path, HERDR_PANE_ID: "pane-one" } };
}

function reporter(t, env, options) {
  const client = createHerdrReporter(env, options);
  t.after(() => client.dispose());
  return client;
}

test("reports the root identity and resumable V2 command on the pane captured at setup", async (t) => {
  const { requests, env } = await socketFixture(t);
  const client = reporter(t, env);
  env.HERDR_PANE_ID = "wrong-pane";
  env.HERDR_SOCKET_PATH = "/not-the-captured-socket";
  assert.equal(await client.report(working), true);
  assert.equal(requests.length, 1);
  const { id, method, params: { seq, ...params } } = requests[0];
  assert.equal(typeof id, "string");
  assert.equal(method, "pane.report_agent");
  assert.ok(Number.isSafeInteger(seq));
  assert.deepEqual(params, {
    pane_id: "pane-one",
    source: "custom:opencode-v2-lifecycle",
    agent: "opencode-v2",
    state: "working",
    agent_session_id: "ses_root",
    resume_argv: ["opencode2", "--session", "ses_root"],
    message: "child-active",
  });
});

test("skips reporting outside a fully identified Herdr pane", async (t) => {
  const { requests, env } = await socketFixture(t);
  for (const field of ["HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH"]) {
    const client = reporter(t, { ...env, [field]: "" });
    assert.equal(client.enabled, false);
    assert.equal(await client.report(working), false);
  }
  assert.equal(requests.length, 0);
});

test("coalesces acknowledged identical states but orders changed roots and states", async (t) => {
  const { requests, env } = await socketFixture(t);
  const client = reporter(t, env);
  await Promise.all([client.report(working), client.report(working), client.report(working)]);
  await client.report({ ...working, state: "blocked", reason: "question" });
  await client.report({ ...working, rootID: "ses_other" });
  assert.deepEqual(requests.map(({ params }) => [params.agent_session_id, params.state]), [
    ["ses_root", "working"], ["ses_root", "blocked"], ["ses_other", "working"],
  ]);
  assert.ok(requests[0].params.seq < requests[1].params.seq);
  assert.ok(requests[1].params.seq < requests[2].params.seq);
  assert.equal(new Set(requests.map((request) => request.id)).size, 3);
});

test("a JSON error reply is a failed delivery and the same state is retried", async (t) => {
  let attempt = 0;
  const { requests, env } = await socketFixture(t, (request, socket) => {
    socket.end(`${JSON.stringify(++attempt === 1
      ? { id: request.id, error: { code: "invalid_params", message: "rejected" } }
      : { id: request.id, result: { type: "ok" } })}\n`);
  });
  const client = reporter(t, env);
  assert.equal(await client.report(working), false);
  assert.equal(await client.report(working), true);
  assert.equal(requests.length, 2);
  assert.ok(requests[1].params.seq > requests[0].params.seq);
});

test("an uncertain delivery invalidates the prior acknowledgement before state reverses", async (t) => {
  let attempt = 0;
  const { requests, env } = await socketFixture(t, (request, socket) => {
    // The report can have reached Herdr even if its acknowledgement is lost.
    if (++attempt === 2) return socket.end();
    socket.end(`${JSON.stringify({ id: request.id, result: { type: "ok" } })}\n`);
  });
  const client = reporter(t, env);
  assert.equal(await client.report(working), true);
  assert.equal(await client.report({ ...working, state: "blocked" }), false);
  assert.equal(await client.report(working), true);
  assert.deepEqual(requests.map(({ params }) => params.state), ["working", "blocked", "working"]);
});

test("waits for a complete newline-framed acknowledgement across chunks", async (t) => {
  const { env } = await socketFixture(t, (request, socket) => {
    const ack = JSON.stringify({ id: request.id, result: { type: "ok" } });
    socket.write(ack.slice(0, 7));
    setImmediate(() => socket.end(`${ack.slice(7)}\n`));
  });
  assert.equal(await reporter(t, env).report(working), true);
});

for (const [name, reply] of [
  ["malformed JSON", (_request, socket) => socket.end("not-json\n")],
  ["a mismatched request id", (_request, socket) => socket.end('{"id":"other","result":{"type":"ok"}}\n')],
  ["a missing success result", (request, socket) => socket.end(`${JSON.stringify({ id: request.id })}\n`)],
  ["an unrelated result", (request, socket) => socket.end(`${JSON.stringify({ id: request.id, result: { type: "pane" } })}\n`)],
  ["an early disconnect", (_request, socket) => socket.end()],
  ["an oversized reply", (_request, socket) => socket.end("x".repeat(70_000))],
]) {
  test(`does not acknowledge ${name}`, async (t) => {
    const { env } = await socketFixture(t, reply);
    assert.equal(await reporter(t, env).report(working), false);
  });
}

test("bounds a connected socket that never acknowledges", async (t) => {
  const { env } = await socketFixture(t, () => {});
  const started = Date.now();
  assert.equal(await reporter(t, env, { timeoutMs: 40 }).report(working), false);
  assert.ok(Date.now() - started < 500);
});

test("a missing socket is a failed delivery rather than a cached success", async (t) => {
  const { env } = await socketFixture(t);
  const client = reporter(t, { ...env, HERDR_SOCKET_PATH: `${env.HERDR_SOCKET_PATH}-missing` });
  assert.equal(await client.report(working), false);
  assert.equal(await client.report(working), false);
});

test("independent pane reporters retain their own identities", async (t) => {
  const { requests, env } = await socketFixture(t);
  const first = reporter(t, env);
  const second = reporter(t, { ...env, HERDR_PANE_ID: "pane-two" });
  await Promise.all([first.report(working), second.report({ ...working, rootID: "ses_two" })]);
  assert.deepEqual(requests.map(({ params }) => [params.pane_id, params.agent_session_id]).sort(), [
    ["pane-one", "ses_root"], ["pane-two", "ses_two"],
  ]);
});

test("aborted selections and disposed reporters cannot deliver queued reports", async (t) => {
  let received;
  const arrival = new Promise((resolve) => { received = resolve; });
  const { requests, env } = await socketFixture(t, received);
  const client = reporter(t, env);
  const controller = new AbortController();
  const first = client.report(working, { signal: controller.signal });
  await arrival;
  const queued = client.report({ ...working, state: "idle" });
  controller.abort();
  client.dispose();
  assert.deepEqual(await Promise.all([first, queued]), [false, false]);
  assert.equal(await client.report(working), false);
  assert.equal(requests.length, 1);
});

test("a replacement reporter fences old reports and old cleanup never releases its authority", async (t) => {
  const { requests, env } = await socketFixture(t);
  const old = reporter(t, env);
  await old.report(working);
  const next = reporter(t, env);
  await next.report({ ...working, rootID: "ses_replacement" });
  old.dispose();
  assert.equal(await old.report({ ...working, state: "idle" }), false);
  await next.report({ ...working, rootID: "ses_replacement", state: "blocked" });
  assert.deepEqual(requests.map(({ method, params }) => [method, params.agent_session_id, params.state]), [
    ["pane.report_agent", "ses_root", "working"],
    ["pane.report_agent", "ses_replacement", "working"],
    ["pane.report_agent", "ses_replacement", "blocked"],
  ]);
  assert.ok(requests[2].params.seq > requests[1].params.seq);
});

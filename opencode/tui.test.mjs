import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import plugin from "./tui.js";
import { LifecycleRPC } from "./contract.mjs";

function snapshot(overrides = {}) {
  return {
    rootID: "ses_root", state: "working", complete: false, reason: "awaiting-parent",
    activeCount: 1, pendingCount: 0, questionIDs: [], title: "Task",
    location: { directory: "/project" }, ...overrides,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function until(predicate, description) {
  const end = Date.now() + 1_500;
  while (!predicate() && Date.now() < end) await delay(5);
  assert.ok(predicate(), description);
}

async function socketFixture(t, reply) {
  const directory = await mkdtemp(join(tmpdir(), "lifecycle-tui-"));
  const path = join(directory, "s");
  const reports = [];
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
      reports.push(request);
      const response = reply?.(request, reports.length) ?? { id: request.id, result: { type: "ok" } };
      socket.end(`${JSON.stringify(response)}\n`);
    });
  });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(path, resolve); });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  return { reports, env: { HERDR_ENV: "1", HERDR_SOCKET_PATH: path, HERDR_PANE_ID: "pane-test" } };
}

function host(overrides = {}) {
  const listeners = new Set();
  const calls = [];
  const state = {
    route: { type: "session", sessionID: "ses_root" },
    response: snapshot(),
    resolve: undefined,
    sessionLocation: { directory: "/selected-project" },
    ...overrides,
  };
  const api = {
    options: { pollIntervalMs: 20, refreshIntervalMs: 10_000, retryIntervalMs: 30, requestTimeoutMs: 200, ...overrides.options },
    location: { directory: "/tui-project" },
    ui: { router: { current: () => state.route } },
    data: {
      location: { default: () => ({ directory: "/default-project" }) },
      session: {
        get: (id) => ({ id, location: state.sessionLocation }),
        // Root inactivity must not override authoritative descendant activity.
        status: () => "idle",
      },
      listen(callback) { listeners.add(callback); return () => listeners.delete(callback); },
    },
    client: {
      rpc(definition) {
        assert.equal(definition, LifecycleRPC);
        return {
          snapshot(input, options) {
            assert.equal(typeof input.sessionID, "string");
            assert.equal(typeof options.location.directory, "string");
            assert.ok(options.signal instanceof AbortSignal);
            calls.push({ input, options });
            return state.resolve ? state.resolve(input, options) : Promise.resolve(state.response);
          },
        };
      },
    },
  };
  return {
    api, state, calls, listeners,
    emit(type, data = { sessionID: state.route.sessionID }) {
      for (const listener of listeners) listener({ details: { type, data, location: { directory: "/project" } } });
    },
  };
}

function start(t, ui, env) {
  const original = Object.fromEntries(Object.keys(env).map((key) => [key, process.env[key]]));
  Object.assign(process.env, env);
  let stop;
  try { stop = plugin.setup(ui.api); } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
  t.after(() => stop?.());
  return stop;
}

function states(reports) {
  return reports.map(({ params }) => [params.agent_session_id, params.state]);
}

test("root inactivity and child navigation still bind authoritative working state to the root", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host({ route: { type: "session", sessionID: "ses_child" } });
  start(t, ui, env);
  await until(() => reports.length === 1, "root report");
  assert.equal(plugin.id, "local.task-lifecycle.tui");
  assert.deepEqual(ui.calls[0].input, { sessionID: "ses_child" });
  assert.deepEqual(ui.calls[0].options.location, { directory: "/selected-project" });
  assert.deepEqual(states(reports), [["ses_root", "working"]]);
  assert.deepEqual(reports[0].params.resume_argv, ["opencode", "--session", "ses_root"]);
  ui.emit("session.execution.succeeded", { sessionID: "ses_root" });
  await until(() => ui.calls.length === 2, "refresh after root exit");
  await delay(35);
  // The event may race the first socket acknowledgement; retrying the same
  // state is legitimate. The business invariant is that no child/root pause
  // ever replaces the pane identity or reports idle.
  assert.ok(reports.every(({ params }) => params.agent_session_id === "ses_root" && params.state === "working"));
});

test("child questions report blocked and only a completed root snapshot reports done as idle", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host();
  start(t, ui, env);
  await until(() => reports.length === 1, "working report");
  ui.state.response = snapshot({ state: "blocked", reason: "question", questionIDs: ["question-child"] });
  ui.emit("form.created", { form: { id: "question-child", sessionID: "ses_child", type: "question" } });
  await until(() => reports.length === 2, "blocked report");
  ui.state.response = snapshot({ state: "idle", complete: true, reason: "done", activeCount: 0, completionKey: "done:ses_root:msg_final" });
  ui.emit("form.replied", { id: "question-child", sessionID: "ses_child", answer: { answers: [["yes"]] } });
  ui.emit("session.execution.succeeded", { sessionID: "ses_root" });
  await until(() => reports.length === 3, "completed report");
  assert.deepEqual(states(reports), [["ses_root", "working"], ["ses_root", "blocked"], ["ses_root", "idle"]]);
});

test("a background shell exit refreshes authoritative completion without a session execution event", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host({ response: snapshot({ activeCount: 0, pendingCount: 1, reason: "running" }) });
  start(t, ui, env);
  await until(() => reports.length === 1, "pending shell report");
  ui.state.response = snapshot({ state: "idle", activeCount: 0, complete: true, reason: "done" });
  ui.emit("shell.exited", { id: "sh_background", exit: 0, status: "exited" });
  await until(() => reports.length === 2, "shell completion report");
  assert.deepEqual(states(reports), [["ses_root", "working"], ["ses_root", "idle"]]);
});

test("failed and malformed snapshots preserve the previous report and recover on retry", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host();
  start(t, ui, env);
  await until(() => reports.length === 1, "working report");
  ui.state.resolve = () => Promise.reject(new Error("server disconnected"));
  ui.emit("server.connected", {});
  await until(() => ui.calls.length >= 3, "RPC retry");
  assert.deepEqual(states(reports), [["ses_root", "working"]]);
  ui.state.resolve = () => Promise.resolve({ state: "idle", rootID: "ses_root" });
  await delay(75);
  assert.deepEqual(states(reports), [["ses_root", "working"]]);
  ui.state.resolve = undefined;
  ui.state.response = snapshot({ state: "blocked", reason: "question", questionIDs: ["q1"] });
  await until(() => reports.length === 2, "recovery report");
  assert.deepEqual(states(reports), [["ses_root", "working"], ["ses_root", "blocked"]]);
});

test("selection polling and periodic snapshots recover navigation and lost lifecycle events", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host({ options: { refreshIntervalMs: 60 } });
  start(t, ui, env);
  await until(() => reports.length === 1, "initial report");
  ui.state.response = snapshot({ state: "blocked", reason: "permission" });
  await until(() => reports.length === 2, "lost event recovered by polling");
  ui.state.route = { type: "session", sessionID: "ses_next" };
  ui.state.response = snapshot({ rootID: "ses_next" });
  await until(() => reports.length === 3, "selection recovered by polling");
  assert.deepEqual(states(reports), [["ses_root", "working"], ["ses_root", "blocked"], ["ses_next", "working"]]);
});

test("native lifecycle bursts coalesce and invalidate an in-flight pre-event snapshot", async (t) => {
  const pending = deferred();
  const { reports, env } = await socketFixture(t);
  let first = true;
  const ui = host({ resolve: () => {
    if (first) { first = false; return pending.promise; }
    return Promise.resolve(snapshot());
  } });
  start(t, ui, env);
  await until(() => ui.calls.length === 1, "initial snapshot started");
  for (const type of ["session.created", "session.execution.started", "session.inbox.enqueued", "permission.asked", "form.cancelled", "server.connected"]) {
    ui.emit(type);
  }
  pending.resolve(snapshot({ state: "idle", complete: true, activeCount: 0, reason: "done" }));
  await until(() => reports.length === 1, "fresh snapshot report");
  assert.equal(ui.calls.length, 2);
  assert.deepEqual(states(reports), [["ses_root", "working"]]);
});

test("tool progress in a busy server cannot starve an authoritative snapshot", async (t) => {
  const pending = deferred();
  const { reports, env } = await socketFixture(t);
  const ui = host({ resolve: () => pending.promise });
  start(t, ui, env);
  await until(() => ui.calls.length === 1, "initial lookup");
  for (const type of ["session.step.started", "session.step.ended", "session.tool.called", "session.tool.success", "session.tool.failed"]) {
    ui.emit(type, { sessionID: "ses_unrelated" });
  }
  pending.resolve(snapshot());
  await until(() => reports.length === 1, "the first authoritative snapshot is usable");
  assert.equal(ui.calls.length, 1);
});

test("token and tool-input deltas do not drive RPC refreshes", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host();
  start(t, ui, env);
  await until(() => reports.length === 1, "initial report");
  for (let index = 0; index < 100; index++) {
    for (const type of ["session.text.delta", "session.reasoning.delta", "session.tool.input.delta", "session.compaction.delta", "session.tool.progress"]) ui.emit(type);
  }
  await delay(70);
  assert.equal(ui.calls.length, 1);
  assert.equal(reports.length, 1);
});

test("a stale A response cannot overwrite B even when the RPC ignores cancellation", async (t) => {
  const pending = deferred();
  const { reports, env } = await socketFixture(t);
  const ui = host({ resolve: ({ sessionID }) => sessionID === "ses_root" ? pending.promise : Promise.resolve(snapshot({ rootID: sessionID })) });
  start(t, ui, env);
  await until(() => ui.calls.length === 1, "A lookup");
  ui.state.route = { type: "session", sessionID: "ses_b" };
  ui.emit("session.execution.started");
  await until(() => reports.length === 1, "B report without waiting for A");
  assert.equal(ui.calls[0].options.signal.aborted, true);
  pending.resolve(snapshot({ state: "idle", complete: true, reason: "done" }));
  await delay(60);
  assert.deepEqual(states(reports), [["ses_b", "working"]]);
});

test("A to B to A rejects the first generation's delayed completion", async (t) => {
  const firstA = deferred();
  const b = deferred();
  let count = 0;
  const { reports, env } = await socketFixture(t);
  const ui = host({ resolve: () => [firstA.promise, b.promise, Promise.resolve(snapshot())][count++] });
  start(t, ui, env);
  await until(() => ui.calls.length === 1, "first A lookup");
  ui.state.route = { type: "session", sessionID: "ses_b" };
  ui.emit("session.execution.started");
  await until(() => ui.calls.length === 2, "B lookup");
  ui.state.route = { type: "session", sessionID: "ses_root" };
  ui.emit("session.execution.started");
  await until(() => reports.length === 1, "new A report");
  firstA.resolve(snapshot({ state: "idle", complete: true, reason: "done" }));
  b.resolve(snapshot({ rootID: "ses_b", state: "blocked", reason: "question" }));
  await delay(60);
  assert.deepEqual(states(reports), [["ses_root", "working"]]);
  assert.ok(ui.calls[0].options.signal.aborted && ui.calls[1].options.signal.aborted);
});

test("rejected Herdr sends retry the latest snapshot until acknowledged", async (t) => {
  const { reports, env } = await socketFixture(t, (request, attempt) => attempt === 1
    ? { id: request.id, error: { code: "busy", message: "try again" } }
    : { id: request.id, result: { type: "ok" } });
  const ui = host();
  start(t, ui, env);
  await until(() => reports.length === 2, "transport retry");
  assert.deepEqual(states(reports), [["ses_root", "working"], ["ses_root", "working"]]);
  assert.ok(reports[1].params.seq > reports[0].params.seq);
  await delay(70);
  assert.equal(reports.length, 2);
});

test("separate TUI panes use their own captured environment with a shared RPC server", async (t) => {
  const { reports, env } = await socketFixture(t);
  const one = host();
  const two = host({ route: { type: "session", sessionID: "ses_two_child" }, response: snapshot({ rootID: "ses_two_root", state: "blocked", reason: "question" }) });
  start(t, one, { ...env, HERDR_PANE_ID: "pane-one" });
  start(t, two, { ...env, HERDR_PANE_ID: "pane-two" });
  await until(() => reports.length === 2, "both panes");
  assert.deepEqual(reports.map(({ params }) => [params.pane_id, params.agent_session_id, params.state]).sort(), [
    ["pane-one", "ses_root", "working"], ["pane-two", "ses_two_root", "blocked"],
  ]);
});

test("home navigation and cleanup do not fabricate idle or release ongoing roots", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host({ options: { refreshIntervalMs: 60 } });
  const stop = start(t, ui, env);
  await until(() => reports.length === 1, "working report");
  ui.state.route = { type: "home" };
  ui.emit("session.execution.succeeded", { sessionID: "ses_root" });
  await delay(80);
  stop();
  const count = ui.calls.length;
  ui.emit("server.connected", {});
  await delay(80);
  assert.equal(ui.calls.length, count);
  assert.equal(ui.listeners.size, 0);
  assert.deepEqual(states(reports), [["ses_root", "working"]]);
  assert.ok(reports.every((report) => report.method === "pane.report_agent"));
});

test("cleanup aborts the RPC and suppresses late results and all future timer effects", async (t) => {
  const pending = deferred();
  const { reports, env } = await socketFixture(t);
  const ui = host({ resolve: () => pending.promise, options: { refreshIntervalMs: 60 } });
  const stop = start(t, ui, env);
  await until(() => ui.calls.length === 1, "in-flight snapshot");
  stop();
  assert.equal(ui.calls[0].options.signal.aborted, true);
  pending.resolve(snapshot({ state: "idle", complete: true, reason: "done" }));
  ui.emit("session.execution.succeeded");
  await delay(120);
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.listeners.size, 0);
  assert.deepEqual(reports, []);
});

test("an unresponsive RPC is bounded and retried without accepting its late response", async (t) => {
  const pending = deferred();
  let first = true;
  const { reports, env } = await socketFixture(t);
  const ui = host({ options: { requestTimeoutMs: 50 }, resolve: () => {
    if (first) { first = false; return pending.promise; }
    return Promise.resolve(snapshot());
  } });
  start(t, ui, env);
  await until(() => reports.length === 1, "retry after timeout");
  assert.equal(ui.calls[0].options.signal.aborted, true);
  pending.resolve(snapshot({ state: "idle", complete: true, reason: "done" }));
  await delay(50);
  assert.deepEqual(states(reports), [["ses_root", "working"]]);
});

test("unhydrated sessions use the TUI location and then the default location", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host({ sessionLocation: undefined });
  start(t, ui, env);
  await until(() => reports.length === 1, "fallback location request");
  assert.deepEqual(ui.calls[0].options.location, { directory: "/tui-project" });
  ui.api.location = undefined;
  ui.emit("session.moved");
  await until(() => ui.calls.length === 2, "default location request");
  assert.deepEqual(ui.calls[1].options.location, { directory: "/default-project" });
});

test("outside Herdr the plugin starts no subscriptions, timers or RPCs", async (t) => {
  const { reports, env } = await socketFixture(t);
  const ui = host();
  start(t, ui, { ...env, HERDR_ENV: "0" });
  ui.emit("session.execution.started");
  await delay(50);
  assert.equal(ui.calls.length, 0);
  assert.equal(ui.listeners.size, 0);
  assert.deepEqual(reports, []);
});
import { tmpdir } from 'node:os';

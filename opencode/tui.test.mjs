import assert from "node:assert/strict";
import { mock, test } from "node:test";
import plugin from "./tui.js";
import { quiet, socketFixture } from "./fixtures.mjs";

const SOURCE = "custom:opencode-background-work";
const LEGACY_SOURCE = "custom:opencode-v2-lifecycle";

// A fake OpenCode world. `store` is what api.data shows; `live` is what api.client answers.
function world(overrides = {}) {
  const w = {
    route: { type: "session", sessionID: "root" },
    sessions: { root: { dir: "/a" }, child: { parent: "root", dir: "/b" } },
    status: { root: "idle", child: "idle" },
    shells: {}, // directory -> shells, as shown by the store
    liveActive: undefined, // undefined = derive from status
    liveShells: undefined, // undefined = same as the store
    permissions: {}, forms: {},
    liveFails: false,
    storeThrows: false,
    ...overrides,
  };
  const rootOf = (id) => (w.sessions[id].parent ? rootOf(w.sessions[id].parent) : id);
  const guard = () => { if (w.storeThrows) throw new Error("store not hydrated"); };
  w.api = {
    ui: { router: { current: () => w.route } },
    data: {
      session: {
        root: (id) => { guard(); return rootOf(id); },
        family: (id) => Object.keys(w.sessions).filter((s) => rootOf(s) === rootOf(id)),
        status: (id) => w.status[id],
        get: (id) => ({ location: { directory: w.sessions[id].dir } }),
        permission: { list: (id) => w.permissions[id] },
        form: { list: (id) => w.forms[id] },
      },
      shell: { list: (location) => w.shells[location.directory] ?? [] },
    },
    client: {
      session: {
        active: async () => {
          if (w.liveFails) throw new Error("service unavailable");
          return w.liveActive ?? Object.fromEntries(Object.entries(w.status).filter(([, s]) => s === "running").map(([id]) => [id, { type: "running" }]));
        },
      },
      shell: {
        list: async ({ location }) => {
          if (w.liveFails) throw new Error("service unavailable");
          return { location, data: (w.liveShells ?? w.shells)[location.directory] ?? [] };
        },
      },
    },
  };
  return w;
}

const shell = (sessionID, status = "running") => ({ id: `sh_${sessionID}`, status, metadata: { sessionID } });

// setup() captures the pane environment once, so only these fake values are ever visible to it.
async function start(t, env, w, requests, startupOut = []) {
  const saved = Object.fromEntries(["HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH"].map((key) => [key, process.env[key]]));
  Object.assign(process.env, env);
  try { mock.timers.enable({ apis: ["setInterval"] }); } catch { /* already enabled by an earlier start() in this test */ }
  let cleanup;
  const first = requests.length;
  try { cleanup = plugin.setup(w.api); } finally {
    for (const [key, value] of Object.entries(saved)) value === undefined ? delete process.env[key] : (process.env[key] = value);
  }
  t.after(() => mock.timers.reset());
  await quiet(); // let the immediate first evaluation finish before the test changes the world
  // A new instance frees a pane that release 1.0.3 or an earlier instance left held; the tests below start after that.
  if (cleanup) {
    const startup = requests.splice(first);
    startupOut.push(...startup);
    assert.ok(startup.some((request) => request.params.source === LEGACY_SOURCE), "setup clears the legacy source");
    assert.ok(startup.some((request) => request.params.source === SOURCE), "setup settles its own source");
  }
  return cleanup;
}

const tick = async (n = 1) => { for (let i = 0; i < n; i++) { mock.timers.tick(1_000); await quiet(); } };
const methods = (requests) => requests.map((r) => `${r.method}${r.params.state ? ":" + r.params.state : ""}`);

test("holds working while a descendant session is running, then hands the pane back", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  await quiet();
  assert.deepEqual(requests, [], "idle family: nothing is sent");
  w.status.child = "running";
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working"]);
  assert.equal(requests[0].params.source, SOURCE);
  assert.equal(requests[0].params.agent, "opencode");
  await tick(3);
  assert.equal(requests.length, 1, "an unchanged state is not re-sent");
  w.status.child = "idle";
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
  assert.equal(requests[1].params.source, SOURCE);
});

test("holds working while a family session owns a running shell in another location", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.shells["/b"] = [shell("child")]; // the root lives in /a, the owning child in /b
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working"]);
  w.shells["/b"] = [];
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
});

test("does not hold for an exited shell, a shell owned outside the family, or the root's own running turn", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.status.root = "running";
  w.shells["/a"] = [shell("child", "exited"), shell("someone-else"), { id: "x", status: "running", metadata: {} }];
  await tick(2);
  assert.deepEqual(requests, [], "native detection owns an ordinary turn");
});

test("a shell owned by the root itself counts, even when the root is idle", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.shells["/a"] = [shell("root")];
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working"]);
});

test("viewing a subagent session still evaluates the whole family of its root", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world({ route: { type: "session", sessionID: "child" } });
  await start(t, env, w, requests);
  w.shells["/a"] = [shell("root")];
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working"]);
});

test("reports blocked while holding with a pending permission or question, then working once answered", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  w.permissions.root = [{ id: "per_1" }];
  await tick();
  w.permissions.root = [];
  await tick();
  w.forms.child = [{ id: "frm_1" }];
  await tick();
  w.forms.child = [];
  await tick();
  assert.deepEqual(methods(requests), [
    "pane.report_agent:working", "pane.report_agent:blocked", "pane.report_agent:working",
    "pane.report_agent:blocked", "pane.report_agent:working",
  ]);
});

test("a pending question without background work is left to native detection", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.status.root = "running";
  w.forms.root = [{ id: "frm_1" }];
  w.permissions.child = [{ id: "per_1" }];
  await tick(2);
  assert.deepEqual(requests, []);
});

test("releases when the selected route leaves a session", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  w.route = { type: "home" };
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
});

test("a stale store cannot hold the pane: a positive must be confirmed by live reads", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world({ liveActive: {}, liveShells: {} });
  await start(t, env, w, requests);
  w.status.child = "running"; // the store never saw the end event; the service says nothing runs
  w.shells["/a"] = [shell("root")];
  await tick(2);
  assert.deepEqual(requests, []);
  // The same stale evidence releases an existing hold once the live read disagrees.
  w.liveActive = undefined;
  w.liveShells = undefined;
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working"]);
  w.liveActive = {};
  w.liveShells = {};
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
});

test("a failed live read releases instead of reporting anything else", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  w.liveFails = true;
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
  w.liveFails = false;
  await tick();
  assert.equal(methods(requests).at(-1), "pane.report_agent:working", "recovers once reads work again");
});

test("an unhydrated store releases instead of reporting unknown", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  w.storeThrows = true;
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
});

test("retries on the next tick when Herdr did not acknowledge", async (t) => {
  let holds = 0;
  const { requests, env } = await socketFixture(t, (request, socket) => {
    if (request.method === "pane.report_agent" && ++holds === 1) socket.end(`${JSON.stringify({ id: request.id, error: "busy" })}\n`);
    else socket.end(`${JSON.stringify({ id: request.id, result: { type: "ok" } })}\n`);
  });
  const w = world();
  await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  assert.equal(requests.length, 1);
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.report_agent:working"]);
  await tick(2);
  assert.equal(requests.length, 2, "acknowledged, so no further sends");
});

test("a hold whose acknowledgement was lost is still released when the work ends", async (t) => {
  let holds = 0;
  const { requests, env } = await socketFixture(t, (request, socket) => {
    // Herdr applied the first report, but the reply the plugin sees is an error.
    if (request.method === "pane.report_agent" && ++holds === 1) socket.end(`${JSON.stringify({ id: request.id, error: "busy" })}\n`);
    else socket.end(`${JSON.stringify({ id: request.id, result: { type: "ok" } })}\n`);
  });
  const w = world();
  await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  w.status.child = "idle"; // the work ends before the retry
  await tick();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
  await tick(2);
  assert.equal(requests.length, 2, "released and acknowledged, so nothing more is sent");
});

test("a new instance frees a pane an earlier instance held, even when the work ended across the reload", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  const oldCleanup = await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  w.status.child = "idle"; // finished before the old instance ticked again
  const startup = [];
  await start(t, env, w, requests, startup); // hot reload: the new instance sets up before the old one is disposed
  assert.ok(startup.some((request) => request.method === "pane.clear_agent_authority" && request.params.source === SOURCE),
    "the new instance clears its own source");
  assert.ok(startup.every((request) => request.method !== "pane.report_agent"));
  await tick(2);
  const before = requests.length;
  await oldCleanup();
  assert.equal(requests.length, before, "the superseded instance sent nothing");
});

test("disposal releases the pane and stops polling", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  const cleanup = await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  await cleanup();
  assert.deepEqual(methods(requests), ["pane.report_agent:working", "pane.clear_agent_authority"]);
  await tick(3);
  assert.equal(requests.length, 2);
});

test("a superseded instance does not release on disposal; its successor keeps the pane", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  const oldCleanup = await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  await start(t, env, w, requests); // hot reload: the new instance takes over the pane
  await tick();
  const before = requests.length;
  await oldCleanup();
  assert.equal(requests.length, before, "the old generation sent nothing");
  assert.equal(methods(requests).at(-1), "pane.report_agent:working");
});

test("never sends idle or unknown, and seq strictly increases, across a full scenario", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  await start(t, env, w, requests);
  w.status.child = "running"; await tick();
  w.forms.root = [{ id: "f" }]; await tick();
  w.forms.root = []; await tick();
  w.liveFails = true; await tick();
  w.liveFails = false; await tick();
  w.status.child = "idle"; w.shells["/a"] = [shell("root")]; await tick();
  w.shells["/a"] = []; await tick();
  assert.ok(requests.length >= 6);
  for (const request of requests) {
    if (request.method === "pane.report_agent") assert.ok(["working", "blocked"].includes(request.params.state));
    else assert.equal(request.method, "pane.clear_agent_authority");
  }
  const seqs = requests.map((request) => request.params.seq);
  for (let i = 1; i < seqs.length; i++) assert.ok(seqs[i] > seqs[i - 1]);
});

test("on a Herdr without clear_agent_authority the pane is still handed back, via release_agent", async (t) => {
  const { requests, env } = await socketFixture(t, (request, socket) => socket.end(`${JSON.stringify(
    request.method === "pane.clear_agent_authority"
      ? { id: request.id, error: { code: "invalid_request", message: "unknown variant" } }
      : { id: request.id, result: { type: "ok" } })}\n`));
  const w = world();
  const cleanup = await start(t, env, w, requests);
  w.status.child = "running";
  await tick();
  w.status.child = "idle";
  await tick();
  w.status.child = "running";
  await tick();
  w.status.child = "idle";
  await tick();
  // setup's legacy clear already met the rejection, so no release retries clear_agent_authority.
  assert.deepEqual(methods(requests), [
    "pane.report_agent:working", "pane.release_agent", "pane.report_agent:working", "pane.release_agent",
  ]);
  for (const request of requests.filter((r) => r.method === "pane.release_agent")) {
    assert.equal(request.params.source, SOURCE);
    assert.equal(request.params.agent, "opencode");
  }
  await cleanup();
  assert.equal(requests.at(-1).method, "pane.release_agent", "disposal also hands the pane back");
});

test("outside a Herdr pane setup does nothing and sends nothing", async (t) => {
  const { requests, env } = await socketFixture(t);
  const w = world();
  const cleanup = await start(t, { ...env, HERDR_ENV: "0" }, w, requests);
  assert.equal(cleanup, undefined);
  w.status.child = "running";
  await tick(2);
  assert.deepEqual(requests, []);
});

import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { promisify } from "node:util";
import { createNotifier } from "./notify.mjs";

const payload = { key: "private-session:completed:42", title: "Private title", message: "Private message" };
const topic = "private-fixture-topic";

async function fixture(t, respond = (_request, response) => response.end("ok")) {
  const directory = await mkdtemp(join(tmpdir(), "notify-test-"));
  const clock = { now: Date.now() };
  const requests = [];
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    requests.push({ url: request.url, title: request.headers.title, body });
    respond(request, response, requests.length);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  const options = {
    stateDirectory: join(directory, "state"),
    topicFile: join(directory, "topic.sh"),
    endpoint: `http://127.0.0.1:${server.address().port}`,
    timeoutMs: 500,
    now: () => clock.now,
    random: () => 0,
  };
  await writeFile(options.topicFile, `#!/bin/sh\nTOPIC="${topic}"\n`);
  return { directory, options, requests, clock };
}

async function logs(options) {
  return (await readFile(join(options.stateDirectory, "receipts.jsonl"), "utf8"))
    .trim().split("\n").map(JSON.parse);
}

test("concurrent instances and a new process publish a successful key only once", async (t) => {
  const f = await fixture(t);
  const results = await Promise.all(Array.from({ length: 12 }, () => createNotifier(f.options).publish(payload)));
  assert.equal(results.filter((r) => r.status === "sent").length, 1);
  assert.equal(results.filter((r) => r.status === "duplicate").length, 11);
  assert.equal((await createNotifier(f.options).publish(payload)).status, "duplicate");
  const { stdout } = await promisify(execFile)(process.execPath, ["--input-type=module", "-e", `
    import { createNotifier } from ${JSON.stringify(new URL("./notify.mjs", import.meta.url).href)};
    console.log(JSON.stringify(await createNotifier(${JSON.stringify(f.options)}).publish(${JSON.stringify(payload)})));
  `]);
  assert.equal(JSON.parse(stdout).status, "duplicate");
  assert.deepEqual(f.requests, [{ url: `/${topic}`, title: payload.title, body: payload.message }]);
});

test("permanent HTTP failure is logged, not retried or persisted as sent", async (t) => {
  const f = await fixture(t, (_request, response, count) => {
    response.statusCode = count === 1 ? 401 : 200;
    response.end("private upstream body");
  });
  const notifier = createNotifier(f.options);
  const failed = await notifier.publish(payload);
  assert.equal(failed.status, "failed");
  assert.equal(failed.httpStatus, 401);
  assert.equal(failed.attempts, 1);
  assert.ok((await logs(f.options)).some((r) => r.status === "failed" && r.httpStatus === 401));
  assert.equal((await createNotifier(f.options).publish(payload)).terminal, true);
  const next = { ...payload, key: "new-notice-after-auth-repair" };
  assert.equal((await notifier.publish(next)).status, "sent");
  assert.equal((await notifier.publish(next)).status, "duplicate");
  assert.equal(f.requests.length, 2);
});

test("transient HTTP failure recovers on the next scheduled attempt", async (t) => {
  const f = await fixture(t, (_request, response, count) => {
    response.statusCode = count === 1 ? 503 : 200;
    response.end("ok");
  });
  const notifier = createNotifier(f.options);
  const failed = await notifier.publish(payload);
  assert.equal(failed.status, "failed");
  assert.equal(f.requests.length, 1);
  f.clock.now = failed.retryAt;
  const result = await notifier.publish(payload);
  assert.equal(result.status, "sent");
  assert.equal(result.attempts, 2);
  assert.equal(f.requests.length, 2);
  assert.ok((await logs(f.options)).some((r) => r.status === "failed" && r.httpStatus === 503));
});

test("failed retries release their claims while preserving the attempt count", async (t) => {
  const f = await fixture(t, (_request, response, count) => {
    response.statusCode = count <= 2 ? 503 : 200;
    response.end("ok");
  });
  const notifier = createNotifier(f.options);
  const failed = await notifier.publish(payload);
  assert.equal(failed.status, "failed");
  assert.equal(failed.attempts, 1);
  assert.equal(f.requests.length, 1);
  f.clock.now = failed.retryAt;
  const second = await notifier.publish(payload);
  assert.equal(second.status, "failed");
  assert.equal(second.attempts, 2);
  assert.equal(f.requests.length, 2);
  f.clock.now = second.retryAt;
  assert.equal((await notifier.publish(payload)).status, "sent");
  assert.equal(f.requests.length, 3);
});

test("a network disconnect is transient", async (t) => {
  const f = await fixture(t, (request, response, count) => {
    if (count === 1) request.socket.destroy();
    else response.end("ok");
  });
  const notifier = createNotifier(f.options);
  const failed = await notifier.publish(payload);
  assert.equal(failed.reason, "network");
  f.clock.now = failed.retryAt;
  const result = await notifier.publish(payload);
  assert.equal(result.status, "sent");
  assert.equal(result.attempts, 2);
  assert.equal(f.requests.length, 2);
});

test("a stalled HTTP response times out and releases the claim", async (t) => {
  const f = await fixture(t, (_request, response, count) => {
    if (count > 1) response.end("ok");
  });
  const notifier = createNotifier({ ...f.options, timeoutMs: 50 });
  const started = Date.now();
  const failed = await notifier.publish(payload);
  assert.equal(failed.status, "failed");
  assert.equal(failed.reason, "timeout");
  assert.ok(Date.now() - started < 1500);
  assert.equal((await notifier.publish(payload)).status, "deferred");
  f.clock.now = failed.retryAt;
  assert.equal((await notifier.publish(payload)).status, "sent");
  assert.equal(f.requests.length, 2);
});

test("missing or nonliteral topic configuration is disabled without executing it", async (t) => {
  const f = await fixture(t);
  await rm(f.options.topicFile);
  const notifier = createNotifier(f.options);
  assert.equal((await notifier.publish(payload)).status, "disabled");
  const marker = join(f.directory, "must-not-exist");
  await writeFile(f.options.topicFile, `TOPIC="$(touch '${marker}')"\n`);
  assert.equal((await notifier.publish(payload)).status, "disabled");
  await assert.rejects(readFile(marker), { code: "ENOENT" });
  assert.equal(f.requests.length, 0);
  await writeFile(f.options.topicFile, `TOPIC='${topic}'\n`);
  assert.equal((await notifier.publish(payload)).status, "sent");
});

test("receipts persist only sanitized metadata, including failures and retries", async (t) => {
  const f = await fixture(t, (_request, response, count) => {
    response.statusCode = count <= 2 ? 503 : 200;
    response.end(`${topic} ${payload.title} ${payload.message}`);
  });
  const notifier = createNotifier(f.options);
  for (let i = 0; i < 2; i++) {
    const result = await notifier.publish(payload);
    assert.equal(result.status, "failed");
    f.clock.now = result.retryAt;
  }
  assert.equal((await notifier.publish(payload)).status, "sent");
  assert.equal((await notifier.publish(payload)).status, "duplicate");
  for (const receipt of await logs(f.options)) {
    assert.match(receipt.keyHash, /^[a-f0-9]{64}$/);
    assert.ok(Number.isFinite(Date.parse(receipt.time)));
    assert.ok(["publish", "retry"].includes(receipt.event));
    for (const key of Object.keys(receipt)) {
      assert.ok(["keyHash", "time", "event", "status", "reason", "httpStatus", "attempts", "retryAt", "terminal"].includes(key), key);
    }
  }
  async function inspect(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await inspect(path);
      else if (entry.isFile()) {
        const text = await readFile(path, "utf8");
        for (const secret of [topic, payload.key, payload.title, payload.message, f.options.endpoint]) {
          assert.equal(text.includes(secret), false, "private value leaked to state");
        }
      }
    }
  }
  await inspect(f.options.stateDirectory);
});

test("a local transport exception releases the claim without leaking its error text", async (t) => {
  const f = await fixture(t);
  const broken = createNotifier({
    ...f.options,
    fetchImpl: () => { throw new Error(`${topic} ${payload.message}`); },
  });
  const failed = await broken.publish(payload);
  assert.equal(failed.status, "failed");
  f.clock.now = failed.retryAt;
  assert.equal((await createNotifier(f.options).publish(payload)).status, "sent");
  assert.equal(f.requests.length, 1);
  assert.equal(JSON.stringify(await logs(f.options)).includes(topic), false);
});

test("a dead publisher's claim is recoverable by concurrent replacement instances", async (t) => {
  const f = await fixture(t);
  const child = spawn(process.execPath, ["--input-type=module", "-e", `
    import { createNotifier } from ${JSON.stringify(new URL("./notify.mjs", import.meta.url).href)};
    const notifier = createNotifier({ ...${JSON.stringify(f.options)}, timeoutMs: 10000,
      fetchImpl: async () => { console.log('claimed'); await new Promise(() => {}); }
    });
    await notifier.publish(${JSON.stringify(payload)});
  `], { stdio: ["ignore", "pipe", "pipe"] });
  t.after(() => child.kill("SIGKILL"));
  const closed = once(child, "close");
  const claimed = await Promise.race([
    once(child.stdout, "data").then(([data]) => data.toString()),
    closed.then(() => "exited without claiming"),
  ]);
  assert.equal(claimed.trim(), "claimed");
  child.kill("SIGKILL");
  await closed;
  const results = await Promise.all(Array.from({ length: 8 }, () => createNotifier(f.options).publish(payload)));
  assert.equal(results.filter((r) => r.status === "sent").length, 1);
  assert.equal(f.requests.length, 1);
});

test("an abandoned claim expires even when its owner process remains alive", async (t) => {
  const f = await fixture(t);
  const options = { ...f.options, timeoutMs: 50, leaseMs: 100 };
  const child = spawn(process.execPath, ["--input-type=module", "-e", `
    import { createNotifier } from ${JSON.stringify(new URL("./notify.mjs", import.meta.url).href)};
    await createNotifier({ ...${JSON.stringify(options)}, fetchImpl: async () => {
      console.log('claimed'); await new Promise(() => {});
    }}).publish(${JSON.stringify(payload)});
  `], { stdio: ["ignore", "pipe", "pipe"] });
  const closed = once(child, "close");
  t.after(async () => { child.kill("SIGKILL"); await closed; });
  const claimed = await Promise.race([
    once(child.stdout, "data").then(([data]) => data.toString()),
    closed.then(() => "exited without claiming"),
  ]);
  assert.equal(claimed.trim(), "claimed");
  child.kill("SIGSTOP");
  await delay(150);
  assert.equal((await createNotifier(options).publish(payload)).status, "sent");
  assert.equal(f.requests.length, 1);
});

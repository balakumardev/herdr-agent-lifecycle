import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ok = (request, socket) => socket.end(`${JSON.stringify({ id: request.id, result: { type: "ok" } })}\n`);

// A fake Herdr socket: one JSON-line request per connection, answered by `reply`.
export async function socketFixture(t, reply = ok) {
  const directory = await mkdtemp(join(tmpdir(), "opencode-overlay-"));
  const path = join(directory, "herdr.sock");
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
      reply(request, socket, requests.length);
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
  return { requests, env: { HERDR_ENV: "1", HERDR_PANE_ID: "pane-one", HERDR_SOCKET_PATH: path } };
}

export async function waitFor(condition, timeoutMs = 2_000) {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("Timed out waiting for condition");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

export const quiet = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

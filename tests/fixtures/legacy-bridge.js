// Minimal legacy bridge contract fixture. The installer must preserve this
// event/session/message flow while transferring notification ownership.
import { spawn } from "node:child_process";

async function v1({ client, directory }) {
  async function attention(event, sessionID) {
    const info = (await client.session.get({ path: { id: sessionID } })).data;
    if (!info || info.parentID || event !== "Stop") return;
    const messages = (await client.session.messages({ path: { id: sessionID } })).data;
    const last = messages.findLast((item) => item.info?.role === "assistant");
    if (!last?.info?.time?.completed) return;
    const text = last.parts.filter((part) => part.type === "text").map((part) => part.text).join("\n");
    if (!text.trim()) return;
    const child = spawn("legacy-notifier", [directory, text], { detached: true, stdio: "ignore" });
    child.unref();
  }
  return { event: async ({ event }) => {
    if (event.type === "session.idle") await attention("Stop", event.properties.sessionID);
  } };
}

export default {
  id: "claude-code-hooks",
  server: v1,
  async setup(ctx) {
    const directory = ctx.location.directory;
    const v1Client = { session: {
      get: async ({ path }) => ({ data: await ctx.session.get({ sessionID: path.id }) }),
      messages: async ({ path }) => ({ data: (await ctx.session.context({ sessionID: path.id })).map((item) => ({
        info: { id: item.id, role: item.type, time: item.time }, parts: item.content ?? [],
      })) }),
    } };
    const bridge = await v1({ client: v1Client, directory });
    const controller = new AbortController();
    void (async () => {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        const type = event.type === "session.execution.succeeded" ? "session.idle" : event.type;
        await bridge.event({ event: { type, properties: { ...(event.data ?? {}) } } });
      }
    })();
    return () => controller.abort();
  },
};

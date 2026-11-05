import { createHerdr } from "./herdr.mjs";

const POLL_MS = 1_000;
const READ_MS = 2_000;

// Reports `working`/`blocked` only while the selected root's family has background work; undefined means
// "let Herdr's screen detection decide". Never reports idle or unknown.
async function desiredState(api) {
  const route = api.ui.router.current();
  if (route?.type !== "session") return;
  const { session, shell } = api.data;
  const root = session.root(route.sessionID);
  const family = session.family(root);
  const owned = new Set(family);
  const locations = [...new Map(family.map((id) => session.get(id)?.location).filter(Boolean)
    .map((location) => [location.directory, location])).values()];
  const ownedRunning = (shells) => shells.some((item) => item.status === "running" && owned.has(item.metadata?.sessionID));
  // The synchronous store is the free trigger; only a positive is confirmed by live reads below.
  if (!family.some((id) => id !== root && session.status(id) === "running") &&
      !locations.some((location) => ownedRunning(shell.list(location)))) return;
  // A missed event must never leave the pane held, so a hold needs a live read to agree.
  const request = { signal: AbortSignal.timeout(READ_MS) };
  const [active, ...lists] = await Promise.all([api.client.session.active(request),
    ...locations.map((location) => api.client.shell.list({ location }, request))]);
  if (!Object.keys(active).some((id) => id !== root && owned.has(id)) && !lists.some((list) => ownedRunning(list.data))) return;
  // Herdr 0.9.3 still re-detects a visible prompt while held; reporting it here keeps `blocked` right on any version.
  return family.some((id) => session.permission.list(id)?.length || session.form.list(id)?.length) ? "blocked" : "working";
}

// Herdr may have applied a report whose acknowledgement was lost, so after one the state is unknown, not "released".
const UNSURE = Symbol("unsure");
const MESSAGES = { working: "background work running", blocked: "waiting for input" };

export default {
  id: "local.task-lifecycle.tui",
  setup(api) {
    const herdr = createHerdr(process.env);
    if (!herdr) return;
    void herdr.clearLegacy(); // an upgraded pane may still be held `unknown` by release 1.0.3
    let sent = UNSURE; // last acknowledged state; undefined = released. Unsure at start frees a pane an earlier instance held.
    let busy = false;
    let disposed = false;

    async function tick() {
      if (busy || disposed) return;
      busy = true;
      try {
        // Any read error means release: native detection is the fallback, never `unknown`.
        const want = await desiredState(api).catch(() => undefined);
        if (want !== sent && !disposed) {
          const acknowledged = await (want ? herdr.hold(want, MESSAGES[want]) : herdr.release());
          if (acknowledged) sent = want;
          else if (want) sent = UNSURE;
        }
      } finally {
        busy = false;
      }
    }

    const timer = setInterval(tick, POLL_MS);
    timer.unref?.();
    void tick();
    return () => {
      disposed = true;
      clearInterval(timer);
      return herdr.release();
    };
  },
};

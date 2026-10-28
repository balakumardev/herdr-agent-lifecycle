import { LifecycleRPC } from "./contract.mjs";
import { createHerdrReporter } from "./herdr.mjs";

const EVENTS = new Set([
  "session.created", "session.deleted", "session.forked", "session.moved", "session.renamed",
  "session.execution.started", "session.execution.succeeded", "session.execution.interrupted", "session.execution.failed",
  "session.inbox.enqueued", "session.inbox.delivered", "session.inbox.cancelled", "session.inbox.delivery.changed",
  "session.shell.started", "session.shell.ended",
  "shell.created", "shell.exited", "shell.deleted",
  "permission.asked", "permission.replied", "form.created", "form.replied", "form.cancelled",
  "server.connected", "location.shutdown",
]);

function duration(value, fallback) {
  return Number.isFinite(value) ? Math.max(20, Math.min(60_000, value)) : fallback;
}

function validSnapshot(value) {
  return value && typeof value.rootID === "string" && value.rootID.length > 0 &&
    ["working", "blocked", "idle"].includes(value.state) && typeof value.complete === "boolean" &&
    typeof value.reason === "string" && typeof value.title === "string" &&
    Number.isInteger(value.activeCount) && value.activeCount >= 0 &&
    Number.isInteger(value.pendingCount) && value.pendingCount >= 0 &&
    Array.isArray(value.questionIDs) && value.questionIDs.every((id) => typeof id === "string") &&
    typeof value.location?.directory === "string" && value.location.directory.length > 0;
}

function requestSnapshot(api, selection, timeoutMs) {
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      selection.controller.signal.removeEventListener("abort", abort);
      if (error) reject(error);
      else resolve(value);
    };
    const abort = () => {
      controller.abort();
      finish(new Error("Lifecycle snapshot cancelled or timed out"));
    };
    const timer = setTimeout(abort, timeoutMs);
    timer.unref?.();
    selection.controller.signal.addEventListener("abort", abort, { once: true });
    if (selection.controller.signal.aborted) return abort();
    try {
      Promise.resolve(api.client.rpc(LifecycleRPC).snapshot({ sessionID: selection.sessionID }, {
        location: selection.location,
        signal: controller.signal,
      })).then((value) => finish(undefined, value), (error) => finish(error));
    } catch (error) {
      finish(error);
    }
  });
}

export default {
  id: "local.task-lifecycle.tui",
  setup(api) {
    const reporter = createHerdrReporter();
    if (!reporter.enabled) return;
    const options = api.options ?? {};
    const pollMs = duration(options.pollIntervalMs, 250);
    const refreshMs = duration(options.refreshIntervalMs, 3_000);
    const retryMs = duration(options.retryIntervalMs, 500);
    const requestMs = duration(options.requestTimeoutMs, 5_000);
    let disposed = false;
    let selected;

    function readSelection() {
      try {
        const route = api.ui.router.current();
        if (route?.type !== "session" || typeof route.sessionID !== "string" || !route.sessionID) return;
        const location = api.data.session.get(route.sessionID)?.location ?? api.location ?? api.data.location.default();
        if (typeof location?.directory !== "string" || !location.directory) return;
        return {
          sessionID: route.sessionID,
          location: { ...location },
          key: JSON.stringify([route.sessionID, location.directory, location.workspaceID]),
        };
      } catch {
        // Unhydrated UI data is unknown, never evidence of idle.
        return;
      }
    }

    function current(selection) {
      return !disposed && selected === selection && !selection.controller.signal.aborted &&
        readSelection()?.key === selection.key;
    }

    function retire(selection) {
      if (!selection) return;
      clearTimeout(selection.timer);
      selection.controller.abort();
    }

    function schedule(selection, delay = 20) {
      if (disposed || selected !== selection || selection.timer || selection.busy) return;
      selection.timer = setTimeout(() => { void refresh(selection); }, delay);
      selection.timer.unref?.();
    }

    function syncSelection() {
      if (disposed) return;
      const next = readSelection();
      if (next?.key !== selected?.key) {
        retire(selected);
        // Object identity is the route generation: A -> B -> A cannot revive
        // asynchronous work captured by the first A selection.
        selected = next && { ...next, controller: new AbortController(), revision: 0, due: 0, dirty: false, busy: false };
      }
      if (selected && Date.now() >= selected.due) schedule(selected, 0);
    }

    async function refresh(selection) {
      selection.timer = undefined;
      if (!current(selection)) return syncSelection();
      selection.busy = true;
      selection.dirty = false;
      const revision = selection.revision;
      const fresh = () => current(selection) && selection.revision === revision;
      let acknowledged = false;
      try {
        const snapshot = await requestSnapshot(api, selection, requestMs);
        // Events arriving during a read can invalidate that read. Coalesce one
        // follow-up rather than publishing an obsolete completion or question.
        if (fresh()) {
          if (!validSnapshot(snapshot)) throw new Error("Invalid lifecycle snapshot");
          selection.rootID = snapshot.rootID;
          // A send can reach Herdr even if its acknowledgement is lost or raced
          // by new work. The older state no longer proves what the pane shows.
          selection.confirmed = undefined;
          acknowledged = await reporter.report(snapshot, {
            signal: selection.controller.signal,
            isCurrent: fresh,
          });
          if (acknowledged && fresh()) selection.confirmed = snapshot;
        }
      } catch {
        // A missing first report leaves Herdr's screen detector in charge; a
        // stale idle report also hides new background work. Neither is evidence
        // of completion. Preserve known working/blocked states, otherwise claim
        // unknown until the next authoritative read, without inventing ancestry.
        if (current(selection) && (!selection.confirmed || selection.confirmed.state === "idle")) {
          await reporter.report({ rootID: selection.rootID, state: "unknown", reason: "snapshot-unavailable" }, {
            signal: selection.controller.signal,
            // New work invalidates a successful snapshot, but cannot turn a
            // failed read into evidence for retaining Idle. Navigation still fences it.
            isCurrent: () => current(selection),
          }).catch(() => false);
        }
      } finally {
        selection.busy = false;
        selection.due = Date.now() + (acknowledged ? refreshMs : retryMs);
        syncSelection();
        if (selected === selection && selection.dirty) schedule(selection);
      }
    }

    const unsubscribe = api.data.listen(({ details }) => {
      if (disposed || !EVENTS.has(details?.type)) return;
      syncSelection();
      if (!selected) return;
      selected.revision += 1;
      selected.dirty = true;
      schedule(selected);
    });
    const poll = setInterval(syncSelection, pollMs);
    poll.unref?.();
    syncSelection();
    return () => {
      if (disposed) return;
      disposed = true;
      clearInterval(poll);
      retire(selected);
      unsubscribe();
      reporter.dispose();
    };
  },
};

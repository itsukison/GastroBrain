import type { AgentState, MeetingGate } from "./meeting-mode";
import type { RecallAvailability } from "./recall-display";
export type RecallControlOptions = { enabled: boolean; status: "idle" | "connecting" | "live" | "ended" | "error";
  gate: { current: MeetingGate | null }; start: () => Promise<void>; stop: () => void;
  onAvailability?: (state: RecallAvailability) => void };

/** One controller survives voice rotations and discards expired/revoked grants. */
export function startRecallControl(current: () => RecallControlOptions, request = fetch, now = Date.now) {
    let disposed = false;
    let terminal = false;
    let timer: ReturnType<typeof setTimeout>;
    let observed: AgentState | null = null;
    let lastSuccess = now();
    let lastStart = 0;
    let starts: number[] = [];
    let ack: string[] = [];
    let hasStarted = false;
    const report = (state: RecallAvailability) => current().onAvailability?.(state);
    const controller = new AbortController();
    async function poll() {
      if (disposed || terminal) return;
      try {
        const o = current();
        const response = await request("/api/recall/bot/control", { method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8_000)]),
          body: JSON.stringify({ local_state: o.gate.current?.state ?? observed ?? "asleep",
            observed_state: observed, healthy: o.status === "live", acknowledgements: ack }),
        });
        if (disposed) return;
        if ([401, 403, 409].includes(response.status)) {
          terminal = true;
          o.stop();
          report(response.status === 401 ? "ended" : "failed");
          return;
        }
        if (!response.ok) throw new Error("control unavailable");
        const data = await response.json() as { active: boolean; agent_state: AgentState; commands: { id: string; text: string }[] };
        lastSuccess = now();
        ack = [];
        if (!data.active) {
          if (o.status === "live" || o.status === "connecting") o.stop();
          report("connecting");
        } else if (["idle", "ended", "error"].includes(o.status) && now() - lastStart >= 15_000) {
          starts = starts.filter(t => now() - t < 180_000);
          if (starts.length >= 3) { terminal = true; o.stop(); report("failed"); return; }
          starts.push(now());
          lastStart = now();
          report(hasStarted ? "reconnecting" : "connecting");
          hasStarted = true;
          await o.start();
          if (disposed) return;
          // Restore the authoritative gate state after each 55-minute rotation.
          current().gate.current?.set(data.agent_state, "recall-sync");
        }
        if (data.active && current().status === "live") report("ready");
        else if (data.active && hasStarted) report("reconnecting");
        if (data.agent_state !== observed) current().gate.current?.set(data.agent_state, "recall-sync");
        observed = data.agent_state;
        for (const command of data.commands) {
          current().gate.current?.command(command.text);
          ack.push(command.id);
        }
      } catch {
        if (!disposed && now() - lastSuccess > 15_000) {
          current().stop();
          report("reconnecting");
        }
      } finally {
        if (!disposed && !terminal) timer = setTimeout(() => void poll(), 2_000);
      }
    }
    void poll();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
}

"use client";

import { useEffect } from "react";

/** Only one poll at a time; hidden/unmounted pages release their request. */
export function useVisiblePolling(
  refresh: (signal: AbortSignal) => Promise<void>,
  enabled: boolean,
  intervalMs: number,
) {
  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;

    function schedule() {
      if (!disposed && document.visibilityState === "visible") {
        timer = setTimeout(tick, intervalMs);
      }
    }

    async function tick() {
      if (disposed || controller || document.visibilityState !== "visible") return;
      controller = new AbortController();
      try {
        await refresh(controller.signal);
      } catch {
        // Keep the last successful data and retry on the next visible tick.
      } finally {
        controller = undefined;
        schedule();
      }
    }

    function onVisibilityChange() {
      clearTimeout(timer);
      if (document.visibilityState !== "visible") controller?.abort();
      else void tick();
    }

    schedule();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [enabled, intervalMs, refresh]);
}

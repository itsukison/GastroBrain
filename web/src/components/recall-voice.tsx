"use client";
import { useEffect, useRef, useState } from "react";
import { VoiceSession } from "./voice-session";
import { RecallDisplay } from "./recall-display";

export type RecallBinding = { meetingId: string; conversationId: string };
export function RecallVoice() {
  const [binding, setBinding] = useState<RecallBinding | null>(null);
  const [failed, setFailed] = useState(false);
  const launch = useRef<Promise<RecallBinding> | null>(null);
  useEffect(() => {
    // A single exchange even in React StrictMode. A reload uses the HttpOnly cookie.
    launch.current ??= (async () => {
      const token = new URLSearchParams(location.hash.slice(1)).get("launch");
      history.replaceState(null, "", location.pathname);
      if (token) {
        const r = await fetch("/api/recall/bot/exchange", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }), signal: AbortSignal.timeout(20_000) });
        if (!r.ok) throw new Error("launch failed");
      }
      const r = await fetch("/api/recall/bot/context", { cache: "no-store", signal: AbortSignal.timeout(20_000) });
      if (!r.ok) throw new Error("session refused");
      const data = await r.json();
      return { meetingId: data.meeting_id, conversationId: data.conversation_id };
    })();
    let disposed = false;
    void launch.current.then(b => { if (!disposed) setBinding(b); }).catch(() => { if (!disposed) setFailed(true); });
    return () => { disposed = true; };
  }, []);
  if (binding) return <VoiceSession recall={binding} />;
  return <RecallDisplay availability={failed ? "failed" : "connecting"} />;
}

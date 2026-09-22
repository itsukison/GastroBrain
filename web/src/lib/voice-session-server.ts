import { transcriptionHint, voiceInstructions } from "@/lib/voice-prompt";

/** Shared by human sessions and the separately authorized Recall session route. */
export async function mintVoiceSession(terms: string[]) {
  const key = process.env.OPENAI_API_KEY;
  if (!key) return Response.json({ error: "Voice service unavailable" }, { status: 503 });
  const model = process.env.OPENAI_VOICE_MODEL ?? "gpt-realtime-2.1-mini";
  const upstream = await fetch("https://api.openai.com/v1/realtime/client_secrets", {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({ expires_after: { anchor: "created_at", seconds: 600 }, session: { type: "realtime", model } }),
    cache: "no-store",
    signal: AbortSignal.timeout(20_000),
  });
  if (!upstream.ok) return Response.json({ error: "Voice initialization failed" }, { status: 502 });
  const secret = await upstream.json() as { value: string; expires_at?: number };
  return Response.json({ clientSecret: secret.value, expiresAt: secret.expires_at ?? null, model,
    instructions: voiceInstructions(terms), transcriptionHint: transcriptionHint(terms) },
    { headers: { "Cache-Control": "no-store" } });
}

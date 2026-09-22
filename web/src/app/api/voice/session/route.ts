import { backend } from "@/lib/api";
import { requireUser } from "@/lib/auth-guard";
import { mintVoiceSession } from "@/lib/voice-session-server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";



/**
 * Mint an ephemeral Realtime client secret for the signed-in user.
 *
 * OPENAI_API_KEY never leaves the server; the browser only ever holds the
 * short-lived secret, which can do nothing but open one Realtime session.
 *
 * The session's *config* (instructions, turn detection, voice) is applied
 * browser-side by the Agents SDK on connect, so this payload stays minimal and
 * we return the instructions alongside the secret rather than duplicating them.
 * Nothing here is a security boundary: the ACL is enforced by /v1/voice/ask.
 */
export async function POST() {
  await requireUser("/voice");

  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    return Response.json({ error: "OPENAI_API_KEY is not set" }, { status: 500 });
  }

  // Domain vocabulary, scoped to what this user may see. Best-effort: a voice
  // session must still start if the backend is slow or the catalog is down.
  let terms: string[] = [];
  try {
    const { base, token } = await backend();
    const resp = await fetch(`${base}/v1/voice/vocab`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (resp.ok) {
      terms = ((await resp.json()) as { terms: string[] }).terms ?? [];
    }
  } catch {
    // fall through with an empty vocabulary
  }

  return mintVoiceSession(terms);
}

import { cookies } from "next/headers";
import { mintVoiceSession } from "@/lib/voice-session-server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const maxDuration = 120;
const COOKIE = "__Secure-recall-session";
type Context = { params: Promise<{ action: string }> };

async function handle(request: Request, context: Context) {
  const { action } = await context.params;
  const allowed = request.method === "GET" ? ["context"] : ["exchange", "session", "ask", "control", "spoken"];
  if (!allowed.includes(action)) return new Response(null, { status: 404 });
  if (request.method === "POST" && request.headers.get("origin") !== new URL(request.url).origin) {
    return new Response(null, { status: 403 });
  }
  const base = process.env.GASTROBRAIN_API_URL?.replace(/\/$/, "");
  if (!base) return new Response(null, { status: 503 });
  const jar = await cookies();
  const token = jar.get(COOKIE)?.value;
  if (action !== "exchange" && !token) return new Response(null, { status: 401 });
  const response = await fetch(`${base}/v1/recall/bot/${action === "session" ? "context" : action}`, {
    method: action === "session" ? "GET" : request.method,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: request.method === "POST" && action !== "session" ? await request.text() : undefined,
    cache: "no-store", signal: AbortSignal.timeout(action === "ask" ? 110_000 : 20_000),
  });
  if (!response.ok) return Response.json({ error: "Bot request refused" }, { status: response.status });
  const data = await response.json();
  if (action === "exchange") {
    jar.set(COOKIE, data.token, { httpOnly: true, secure: true, sameSite: "strict", path: "/api/recall/bot",
      expires: new Date(data.expires_at) });
    delete data.token;
  }
  if (action === "session") {
    if (!data.active) return new Response(null, { status: 409 });
    return mintVoiceSession(data.terms ?? []);
  }
  return Response.json(data, { headers: { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" } });
}
export const GET = handle;
export const POST = handle;

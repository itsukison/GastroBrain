/**
 * Helpers for the Next.js → Cloud Run proxy. The browser never talks to Cloud
 * Run directly; the route handlers in app/api/ read the Supabase session from
 * cookies, mint a Bearer header, and forward.
 */
import { supabaseServer } from "@/lib/supabase/server";

export class ApiAuthError extends Error {
  constructor() {
    super("unauthenticated");
  }
}

export async function backend() {
  const base = process.env.GASTROBRAIN_API_URL;
  if (!base) throw new Error("GASTROBRAIN_API_URL is not set");
  const supabase = await supabaseServer();

  // getUser() forces session hydration + refresh + cookie chunk re-assembly in
  // the SSR client. Without it, getSession() can return null on the first
  // authenticated request right after a Slack-OIDC login because the chunked
  // auth cookie was just rotated by the middleware on the same request.
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) throw new ApiAuthError();

  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new ApiAuthError();
  return { base: base.replace(/\/$/, ""), token: session.access_token };
}

export async function forward(
  request: Request,
  path: string,
  init?: { method?: string; passBody?: boolean; stream?: boolean },
): Promise<Response> {
  let ctx;
  try {
    ctx = await backend();
  } catch (e) {
    if (e instanceof ApiAuthError) {
      return new Response("unauthenticated", { status: 401 });
    }
    throw e;
  }
  const method = init?.method ?? request.method;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${ctx.token}`,
  };
  let body: BodyInit | undefined;
  if (init?.passBody !== false && method !== "GET" && method !== "DELETE") {
    headers["Content-Type"] = request.headers.get("content-type") ?? "application/json";
    body = await request.text();
  }
  const upstream = await fetch(`${ctx.base}${path}`, {
    method,
    headers,
    body,
    // Important: SSE responses must not be buffered.
    // @ts-expect-error — Node fetch accepts this; types lag.
    duplex: "half",
  });
  if (init?.stream) {
    const out = new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
    return out;
  }
  // Fetch spec: 101/103/204/205/304 are "null body status" — passing even an
  // empty string to the Response constructor throws TypeError. Upstream DELETE
  // returns 204, so handle these explicitly before re-reading the body.
  if ([101, 103, 204, 205, 304].includes(upstream.status)) {
    return new Response(null, { status: upstream.status });
  }
  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}

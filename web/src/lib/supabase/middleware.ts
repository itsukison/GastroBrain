import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

export async function updateSession(request: NextRequest) {
  // These handlers enforce a separate, expiring, single-run grant. Everything
  // else (including the human Recall start/stop routes) still requires Supabase.
  if (request.nextUrl.pathname === "/voice/recall" ||
      /^\/api\/recall\/bot\/(exchange|context|session|ask|control|spoken)$/.test(request.nextUrl.pathname)) {
    const botResponse = NextResponse.next({ request });
    botResponse.headers.set("Cache-Control", "no-store");
    botResponse.headers.set("Referrer-Policy", "no-referrer");
    return botResponse;
  }
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet: { name: string; value: string; options: CookieOptions }[]) {
          cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    },
  );

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const path = request.nextUrl.pathname;
  const isAuthRoute = path === "/login" || path.startsWith("/auth/");
  if (!user && !isAuthRoute) {
    // An API caller needs a status it can act on, not a page. `fetch` follows
    // the redirect, so a redirected /api/* call reports 200 with an HTML body:
    // `resp.ok` is true and `.json()` throws, which callers swallow as
    // transient — leaving the meeting poller spinning forever on an expired
    // session with no sign that the session is what died. 401 matches what the
    // route handlers themselves return via `forward()`.
    if (path.startsWith("/api/")) {
      return NextResponse.json({ detail: "unauthenticated" }, { status: 401 });
    }
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", path);
    return NextResponse.redirect(url);
  }
  if (user && isAuthRoute && path === "/login") {
    const url = request.nextUrl.clone();
    url.pathname = "/";
    return NextResponse.redirect(url);
  }
  return response;
}

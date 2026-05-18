import { redirect } from "next/navigation";
import type { User } from "@supabase/supabase-js";
import { supabaseServer } from "@/lib/supabase/server";

/**
 * Server-side auth guard. Use in Server Components / route handlers to
 * guarantee an authenticated user before any backend call. Middleware does
 * the same job at the edge, but env-var hiccups or runtime errors can let a
 * request slip through — this is the load-bearing check.
 */
export async function requireUser(nextPath?: string): Promise<User> {
  const supabase = await supabaseServer();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    const qs = nextPath ? `?next=${encodeURIComponent(nextPath)}` : "";
    redirect(`/login${qs}`);
  }
  return user;
}

import type { SupabaseClient } from "@supabase/supabase-js";

/** Verify before reading the token, including after middleware rotates cookies. */
export async function readVerifiedSession(supabase: Pick<SupabaseClient, "auth">) {
  const { data: { user }, error: userError } = await supabase.auth.getUser();
  if (userError || !user) return null;

  const { data: { session }, error: sessionError } = await supabase.auth.getSession();
  if (sessionError || !session || session.user.id !== user.id) return null;

  return { user, token: session.access_token };
}

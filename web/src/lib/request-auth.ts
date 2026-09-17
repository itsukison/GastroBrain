import { cache } from "react";
import { supabaseServer } from "@/lib/supabase/server";
import { readVerifiedSession } from "./verified-session";

// React shares this promise only within a Server Component render request.
// Middleware remains independent; route handlers call it once via backend().
// Never replace this with a module-level session/token cache.
export const requestAuth = cache(async () => readVerifiedSession(await supabaseServer()));

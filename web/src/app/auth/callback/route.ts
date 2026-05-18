import { NextResponse, type NextRequest } from "next/server";
import { supabaseServer } from "@/lib/supabase/server";

export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get("code");
  const next = searchParams.get("next") ?? "/";

  if (code) {
    const supabase = await supabaseServer();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (error) {
      return NextResponse.redirect(`${origin}/login?err=${encodeURIComponent(error.message)}`);
    }

    const {
      data: { user },
    } = await supabase.auth.getUser();
    const email = user?.email ?? "";
    if (!email.endsWith("@gastroduce-japan.co.jp")) {
      await supabase.auth.signOut();
      return NextResponse.redirect(
        `${origin}/login?err=${encodeURIComponent("@gastroduce-japan.co.jp アカウントでサインインしてください")}`,
      );
    }
  }

  return NextResponse.redirect(`${origin}${next}`);
}

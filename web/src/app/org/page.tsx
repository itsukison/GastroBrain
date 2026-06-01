import { redirect } from "next/navigation";
import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import { OrgView } from "@/components/org-view";

export const dynamic = "force-dynamic";

// Admin-only. Non-admins are bounced to the chat. The backend re-checks admin
// on every /v1/org/* call, so this guard is UX, not the security boundary.
export default async function OrgPage() {
  await requireUser("/org");

  let me: { email: string | null; level: number; is_admin: boolean };
  try {
    me = await backendGet<typeof me>("/v1/org/me");
  } catch {
    redirect("/");
  }
  if (!me.is_admin) redirect("/");

  return <OrgView adminEmail={me.email ?? ""} />;
}

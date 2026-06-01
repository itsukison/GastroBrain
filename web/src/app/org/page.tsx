import { redirect } from "next/navigation";
import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import { OrgView } from "@/components/org-view";
import type { Role, Member, Folder, FolderRule } from "@/components/org-view";

export const dynamic = "force-dynamic";

// Admin-only. Non-admins are bounced to the chat. The backend re-checks admin
// on every /v1/org/* call, so this guard is UX, not the security boundary.
//
// We prefetch members/folders/roles here (server-side, in parallel) and hand
// them to OrgView as initial data — so the page arrives populated instead of
// firing a client-side fetch waterfall after hydration.
export default async function OrgPage() {
  await requireUser("/org");

  let me: { email: string | null; level: number; is_admin: boolean };
  try {
    me = await backendGet<typeof me>("/v1/org/me");
  } catch {
    redirect("/");
  }
  if (!me.is_admin) redirect("/");

  let roles: Role[] = [];
  let members: Member[] | null = null;
  let folders: Folder[] | null = null;
  let rules: FolderRule[] = [];
  try {
    const [r, m, f] = await Promise.all([
      backendGet<{ roles: Role[] }>("/v1/org/roles"),
      backendGet<{ members: Member[] }>("/v1/org/members"),
      backendGet<{ folders: Folder[]; rules: FolderRule[] }>("/v1/org/folders"),
    ]);
    roles = r.roles;
    members = m.members;
    folders = f.folders;
    rules = f.rules;
  } catch {
    // Leave initial data null/empty — OrgView falls back to client fetch.
  }

  return (
    <OrgView
      adminEmail={me.email ?? ""}
      initialRoles={roles}
      initialMembers={members}
      initialFolders={folders}
      initialRules={rules}
    />
  );
}

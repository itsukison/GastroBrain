import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import { AccessView } from "@/components/org-view";
import type { MyAccess } from "@/components/org-view";

export const dynamic = "force-dynamic";

// Self-service, available to every logged-in user: shows the NotePM notebooks
// they can access (derived from their NotePM permissions) with doc counts.
// Read-only — NotePM is the source of truth, there is nothing to edit. We
// prefetch the access list server-side so the page arrives populated.
export default async function OrgPage() {
  const user = await requireUser("/org");

  let access: MyAccess | null = null;
  try {
    access = await backendGet<MyAccess>("/v1/org/me/access");
  } catch {
    // Leave null — AccessView falls back to a client fetch.
  }

  return <AccessView email={user.email ?? ""} initial={access} />;
}

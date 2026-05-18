import { redirect } from "next/navigation";
import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import type { ThreadSummary } from "@/types";

export const dynamic = "force-dynamic";

export default async function RootPage() {
  await requireUser("/");

  let latest: ThreadSummary | undefined;
  try {
    const data = await backendGet<{ threads: ThreadSummary[] }>("/v1/threads?limit=1");
    latest = data.threads?.[0];
  } catch {
    // Backend transient failure — fall through to /new so the user still lands
    // somewhere usable instead of a blank error.
  }
  if (latest) redirect(`/c/${latest.id}`);
  redirect("/new");
}

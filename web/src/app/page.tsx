import { redirect } from "next/navigation";
import { backendGet } from "@/lib/server-api";
import type { ThreadSummary } from "@/types";

export const dynamic = "force-dynamic";

export default async function RootPage() {
  let latest: ThreadSummary | undefined;
  try {
    const data = await backendGet<{ threads: ThreadSummary[] }>("/v1/threads?limit=1");
    latest = data.threads?.[0];
  } catch {}
  if (latest) redirect(`/c/${latest.id}`);
  redirect("/new");
}

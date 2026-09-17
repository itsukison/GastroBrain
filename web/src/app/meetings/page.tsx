import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import { MeetingsView } from "@/components/meetings-view";
import type { MeetingRow } from "@/types";

export const dynamic = "force-dynamic";

// The meetings the signed-in user was invited to (docs/MEETINGS_WEB.md §5 —
// attendees only, never company-wide). Prefetched server-side so the page
// arrives populated; the client refreshes it while something is live.
export default async function MeetingsPage() {
  await requireUser("/meetings");

  const data = await backendGet<{ meetings: MeetingRow[] }>("/v1/meetings?limit=100");
  return <MeetingsView initial={data.meetings ?? []} />;
}

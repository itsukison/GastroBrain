import { notFound } from "next/navigation";

import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import { MeetingDetailView } from "@/components/meeting-detail";
import type { MeetingDetailResponse } from "@/types";

export const dynamic = "force-dynamic";

export default async function MeetingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  await requireUser(`/meetings/${id}`);

  let data: MeetingDetailResponse;
  try {
    data = await backendGet<MeetingDetailResponse>(`/v1/meetings/${id}`);
  } catch {
    // The backend answers 404 both for "no such meeting" and "not yours", so
    // that a URL cannot be used to probe which meetings exist. Same here.
    notFound();
  }

  return <MeetingDetailView initial={data} />;
}

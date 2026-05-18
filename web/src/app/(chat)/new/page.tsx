import { redirect } from "next/navigation";
import { backend } from "@/lib/api";
import { requireUser } from "@/lib/auth-guard";

export const dynamic = "force-dynamic";

/**
 * Server-side "new chat": guard auth, mint a thread, redirect into it. Doing
 * this on the server means unauthenticated users are bounced to /login by
 * requireUser() before any UI renders, so the old client-side 401 toast can't
 * happen anymore.
 */
export default async function NewChatPage() {
  await requireUser("/new");
  const { base, token } = await backend();
  const resp = await fetch(`${base}/v1/threads`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: "{}",
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`failed to create thread: HTTP ${resp.status}`);
  }
  const thread = (await resp.json()) as { id: string };
  redirect(`/c/${thread.id}`);
}

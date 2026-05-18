import { ChatShell } from "@/components/chat-shell";
import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import type { ThreadSummary } from "@/types";

export const dynamic = "force-dynamic";

export default async function ChatLayout({ children }: { children: React.ReactNode }) {
  const user = await requireUser();

  let threads: ThreadSummary[] = [];
  try {
    const data = await backendGet<{ threads: ThreadSummary[] }>("/v1/threads?limit=50");
    threads = data.threads ?? [];
  } catch {
    // Layout renders even if the backend is briefly unreachable; sidebar shows empty.
  }

  return (
    <ChatShell initialThreads={threads} userEmail={user.email ?? ""}>
      {children}
    </ChatShell>
  );
}

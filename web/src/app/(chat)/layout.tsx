import { ChatShell } from "@/components/chat-shell";
import { backendGet } from "@/lib/server-api";
import { supabaseServer } from "@/lib/supabase/server";
import type { ThreadSummary } from "@/types";

export const dynamic = "force-dynamic";

export default async function ChatLayout({ children }: { children: React.ReactNode }) {
  const supabase = await supabaseServer();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  let threads: ThreadSummary[] = [];
  try {
    const data = await backendGet<{ threads: ThreadSummary[] }>("/v1/threads?limit=50");
    threads = data.threads ?? [];
  } catch {
    // Layout renders even if the backend is briefly unreachable; sidebar shows empty.
  }

  return (
    <ChatShell initialThreads={threads} userEmail={user?.email ?? ""}>
      {children}
    </ChatShell>
  );
}

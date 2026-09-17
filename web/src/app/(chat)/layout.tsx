import { ChatShell } from "@/components/chat-shell";
import { Suspense } from "react";
import { ThreadSidebar } from "@/components/thread-sidebar";
import { SidebarSkeleton } from "@/components/route-skeleton";
import { requireUser } from "@/lib/auth-guard";
import { backendGet } from "@/lib/server-api";
import type { ThreadSummary } from "@/types";

export const dynamic = "force-dynamic";

async function Sidebar({ email }: { email: string }) {
  let threads: ThreadSummary[] = [];
  try {
    const data = await backendGet<{ threads: ThreadSummary[] }>("/v1/threads?limit=50");
    threads = data.threads ?? [];
  } catch {
    // Layout renders even if the backend is briefly unreachable; sidebar shows empty.
  }

  return <ThreadSidebar initial={threads} userEmail={email} />;
}

export default async function ChatLayout({ children }: { children: React.ReactNode }) {
  const user = await requireUser();
  return (
    <ChatShell sidebar={<Suspense fallback={<SidebarSkeleton />}><Sidebar email={user.email ?? ""} /></Suspense>}>
      {children}
    </ChatShell>
  );
}

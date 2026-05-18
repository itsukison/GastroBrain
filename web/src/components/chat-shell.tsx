"use client";

import { useEffect, useState } from "react";
import { PanelLeft } from "lucide-react";
import { ThreadSidebar } from "./thread-sidebar";
import { cn } from "@/lib/cn";
import type { ThreadSummary } from "@/types";

export function ChatShell({
  initialThreads,
  userEmail,
  children,
}: {
  initialThreads: ThreadSummary[];
  userEmail: string;
  children: React.ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const saved = typeof window !== "undefined" ? localStorage.getItem("gb:sidebar-collapsed") : null;
    if (saved === "1") setCollapsed(true);
    setHydrated(true);
  }, []);

  function toggle() {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("gb:sidebar-collapsed", next ? "1" : "0");
      } catch {}
      return next;
    });
  }

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <div
        className={cn(
          "shrink-0 overflow-hidden ease-out",
          hydrated ? "transition-[width] duration-200" : "duration-0",
          collapsed ? "w-0" : "w-72",
        )}
        aria-hidden={collapsed}
      >
        <ThreadSidebar initial={initialThreads} userEmail={userEmail} />
      </div>
      <main className="flex-1 min-w-0 flex flex-col">
        <div className="h-12 flex items-center px-2 shrink-0">
          <button
            onClick={toggle}
            className="h-9 w-9 grid place-items-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground transition"
            aria-label={collapsed ? "サイドバーを開く" : "サイドバーを閉じる"}
            aria-expanded={!collapsed}
          >
            <PanelLeft className="w-[18px] h-[18px]" />
          </button>
        </div>
        <div className="flex-1 min-h-0 flex flex-col">{children}</div>
      </main>
    </div>
  );
}

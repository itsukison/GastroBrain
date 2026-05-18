"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter, usePathname } from "next/navigation";
import { Plus, Trash2, LogOut, Settings } from "lucide-react";
import type { ThreadSummary } from "@/types";
import { cn } from "@/lib/cn";
import { SettingsModal } from "./settings-modal";

// ── Minimal delete-confirmation modal ────────────────────────────────────────
function DeleteModal({
  open,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ animation: "fadeIn 120ms ease" }}
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-[2px]"
        onClick={onCancel}
      />

      {/* Panel */}
      <div
        className="relative z-10 w-[340px] rounded-2xl border border-sidebar-border p-6 shadow-xl"
        style={{
          backgroundColor: "var(--background)",
          animation: "slideUp 160ms cubic-bezier(0.16,1,0.3,1)",
        }}
      >
        <h2 className="text-[15px] font-semibold text-foreground leading-snug">
          スレッドを削除しますか？
        </h2>
        <p className="mt-1.5 text-[13px] text-muted-foreground leading-relaxed">
          この操作は取り消せません。スレッドとすべてのメッセージが完全に削除されます。
        </p>

        <div className="mt-5 flex gap-2.5 justify-end">
          <button
            onClick={onCancel}
            className="h-9 px-4 rounded-lg text-sm font-medium border border-sidebar-border bg-transparent text-foreground hover:bg-sidebar-accent transition"
          >
            キャンセル
          </button>
          <button
            onClick={onConfirm}
            className="h-9 px-4 rounded-lg text-sm font-medium bg-red-500 hover:bg-red-600 text-white transition"
          >
            削除する
          </button>
        </div>
      </div>

      <style>{`
        @keyframes fadeIn  { from { opacity: 0 } to { opacity: 1 } }
        @keyframes slideUp { from { opacity: 0; transform: translateY(6px) scale(0.98) } to { opacity: 1; transform: none } }
      `}</style>
    </div>
  );
}

// ── Main sidebar ─────────────────────────────────────────────────────────────
export function ThreadSidebar({ initial, userEmail }: { initial: ThreadSummary[]; userEmail: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const [threads, setThreads] = useState<ThreadSummary[]>(initial);
  const [pending, startTransition] = useTransition();
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    // Light periodic refresh (every 30s while focused) so newly created threads
    // appear without manual reload. router.refresh() re-runs the server layout.
    let active = true;
    const interval = setInterval(async () => {
      if (!active || document.visibilityState !== "visible") return;
      try {
        const r = await fetch("/api/threads");
        if (!r.ok) return;
        const json = await r.json();
        setThreads(json.threads ?? []);
      } catch {}
    }, 30000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

  async function newChat() {
    startTransition(async () => {
      const resp = await fetch("/api/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!resp.ok) return;
      const t = (await resp.json()) as ThreadSummary;
      setThreads((prev) => [t, ...prev]);
      router.push(`/c/${t.id}`);
    });
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    const id = pendingDelete;
    setPendingDelete(null);
    const before = threads;
    setThreads((prev) => prev.filter((t) => t.id !== id));
    const resp = await fetch(`/api/threads/${id}`, { method: "DELETE" });
    if (!resp.ok) {
      setThreads(before);
      return;
    }
    if (pathname === `/c/${id}`) router.push("/");
  }

  return (
    <>
      <DeleteModal
        open={pendingDelete !== null}
        onCancel={() => setPendingDelete(null)}
        onConfirm={confirmDelete}
      />

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <aside className="w-72 h-full bg-sidebar border-r border-sidebar-border flex flex-col">
        <div className="h-12 px-3 flex items-center gap-[0.3rem] shrink-0">
          <svg fill="none" height="48" viewBox="0 0 42 48" width="42" xmlns="http://www.w3.org/2000/svg" className="h-5 w-auto">
            <path clipRule="evenodd" d="m15.2286 4.99951c-3.2154 0-6.18655 1.71539-7.79425 4.5l-5.7735 9.99999c-1.6076941 2.7846-1.607697 6.2154 0 9l5.7735 10c1.6077 2.7846 4.57885 4.5 7.79425 4.5h11.547c3.2154 0 6.1865-1.7154 7.7942-4.5l5.7735-10c1.6077-2.7846 1.6077-6.2154 0-9l-5.7735-9.99999c-1.6077-2.78461-4.5788-4.5-7.7942-4.5zm11.547 5.99999h-7.2169c-1.1547 0-1.8762 1.2499-1.298 2.2494 1.784 3.0838 3.5722 6.1653 5.3536 9.2506.5359.9282.5359 2.0718 0 3-1.7814 3.0854-3.5696 6.1668-5.3536 9.2506-.5782.9995.1433 2.2494 1.298 2.2494h7.2169c1.0718 0 2.0622-.5718 2.5981-1.5l5.7735-10c.5359-.9282.5359-2.0718 0-3l-5.7735-10c-.5359-.9282-1.5263-1.5-2.5981-1.5z" fill="#0a0a0a" fillRule="evenodd"/>
          </svg>
          <span className="font-semibold text-base tracking-tight">GastroBrain</span>
        </div>
        <div className="px-3 pt-1 pb-4">
          <button
            onClick={newChat}
            disabled={pending}
            className="w-full h-9 rounded-lg border border-sidebar-border bg-transparent hover:bg-sidebar-accent text-foreground flex items-center gap-2 px-3 text-sm transition disabled:opacity-50"
          >
            <Plus className="w-4 h-4" />
            <span>新しいチャット</span>
          </button>
        </div>
        <div className="flex-1 overflow-y-auto scrollbar-thin px-2 pb-2">
          {threads.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs text-muted-foreground">
              まだ会話はありません
            </div>
          ) : (
            <ul className="space-y-0.5">
              {threads.map((t) => {
                const active = pathname === `/c/${t.id}`;
                return (
                  <li key={t.id}>
                    <div
                      className={cn(
                        "group flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm cursor-pointer transition",
                        active
                          ? "bg-sidebar-accent text-foreground"
                          : "text-muted-foreground hover:bg-sidebar-accent hover:text-foreground",
                      )}
                      onClick={() => router.push(`/c/${t.id}`)}
                    >
                      <span className="flex-1 truncate">{t.title}</span>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setPendingDelete(t.id);
                        }}
                        className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive transition"
                        aria-label="削除"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <div className="border-t border-sidebar-border p-2">
          <div className="flex items-center gap-1 rounded-lg px-2.5 py-2 text-xs text-muted-foreground">
            <span className="flex-1 truncate" title={userEmail}>
              {userEmail}
            </span>
            <button
              type="button"
              onClick={() => setSettingsOpen(true)}
              className="h-6 w-6 grid place-items-center rounded-md hover:bg-sidebar-accent hover:text-foreground transition"
              title="設定"
              aria-label="設定"
            >
              <Settings className="w-3.5 h-3.5" aria-hidden />
            </button>
            <form action="/auth/signout" method="post">
              <button
                type="submit"
                className="h-6 w-6 grid place-items-center rounded-md hover:bg-sidebar-accent hover:text-foreground transition"
                title="ログアウト"
                aria-label="ログアウト"
              >
                <LogOut className="w-3.5 h-3.5" aria-hidden />
              </button>
            </form>
          </div>
        </div>
      </aside>
    </>
  );
}

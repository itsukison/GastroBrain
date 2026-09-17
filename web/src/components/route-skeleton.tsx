import type { ReactNode } from "react";
import { ArrowLeft, Video } from "lucide-react";
import { NavigationLink } from "./navigation-link";
import { BackToChatLink } from "./navigation-provider";

function Block({ className = "" }: { className?: string }) {
  return <div aria-hidden className={`rounded-md bg-sidebar-accent motion-safe:animate-pulse ${className}`} />;
}

function Status({ children }: { children: ReactNode }) {
  return <span role="status" className="sr-only">{children}</span>;
}

export function PageSkeleton({ detail = false, title = "読み込み中…" }: { detail?: boolean; title?: string }) {
  return (
    <div className="h-screen overflow-y-auto bg-background">
      <div className={`mx-auto px-6 py-8 ${detail ? "max-w-3xl" : "max-w-4xl"}`}>
        <Status>{title}</Status>
        <div className="flex items-center gap-3 mb-6">
          {detail ? (
            <NavigationLink href="/meetings" aria-label="会議一覧に戻る" className="h-8 w-8 grid place-items-center rounded-lg text-muted-foreground hover:bg-sidebar-accent">
              <ArrowLeft className="h-4 w-4" />
            </NavigationLink>
          ) : (
            <BackToChatLink aria-label="チャットに戻る" className="h-8 w-8 grid place-items-center rounded-lg text-muted-foreground hover:bg-sidebar-accent">
              <ArrowLeft className="h-4 w-4" />
            </BackToChatLink>
          )}
          {detail ? <Block className="h-6 w-56" /> : (
            <h1 className="flex items-center gap-2 text-[18px] font-semibold">
              {title === "会議" && <Video className="h-[18px] w-[18px]" aria-hidden />}{title}
            </h1>
          )}
        </div>
        <div className="ml-11 space-y-5" aria-busy="true">
          <Block className="h-3 w-2/3" />
          {detail ? (
            <>
              <div className="flex gap-4 border-b border-sidebar-border pb-3">
                {[0, 1, 2].map((i) => <Block key={i} className="h-5 w-20" />)}
              </div>
              <Block className="h-5 w-32" />
              {[0, 1, 2, 3, 4].map((i) => <Block key={i} className={`h-4 ${i === 4 ? "w-2/3" : "w-full"}`} />)}
            </>
          ) : (
            <div className="rounded-xl border border-sidebar-border divide-y divide-sidebar-border">
              {[0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="px-4 py-4 space-y-3">
                  <Block className="h-4 w-1/2" /><Block className="h-3 w-1/3" />
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function SidebarSkeleton() {
  return (
    <aside className="w-72 h-full border-r border-sidebar-border bg-sidebar p-3 space-y-4" aria-busy="true">
      <Status>会話一覧を読み込み中…</Status>
      <div className="h-9 font-semibold">GastroBrain</div>
      <Block className="h-9 w-full" />
      {[0, 1, 2, 3, 4].map((i) => <Block key={i} className="h-8 w-full" />)}
    </aside>
  );
}

export function ChatSkeleton() {
  return (
    <div className="flex-1 w-full max-w-3xl mx-auto p-6 space-y-6" aria-busy="true">
      <Status>会話を読み込み中…</Status>
      <Block className="h-14 w-2/3 ml-auto" />
      <div className="space-y-3">
        <Block className="h-4 w-full" /><Block className="h-4 w-full" /><Block className="h-4 w-2/3" />
      </div>
    </div>
  );
}

export function VoiceSkeleton() {
  return (
    <div className="flex flex-col h-dvh bg-background" aria-busy="true">
      <Status>音声画面を読み込み中…</Status>
      <header className="flex items-center gap-2 px-5 h-16 shrink-0">
        <BackToChatLink aria-label="チャットに戻る" className="h-8 w-8 grid place-items-center rounded-full text-muted-foreground hover:bg-secondary">
          <ArrowLeft className="h-4 w-4" />
        </BackToChatLink>
        <h1 className="text-[13px] font-medium">音声で質問</h1>
      </header>
      <div className="flex-1 flex flex-col items-center justify-center gap-6 px-6">
        <Block className="h-24 w-24 rounded-full" />
        <Block className="h-4 w-48" />
        <Block className="h-3 w-64 max-w-full" />
      </div>
    </div>
  );
}

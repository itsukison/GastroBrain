"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";
import { NavigationLink } from "./navigation-link";

export function RouteError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  return (
    <div className="flex flex-1 min-h-64 items-center justify-center p-8">
      <div className="max-w-sm text-center space-y-4" role="alert">
        <h2 className="text-base font-semibold">読み込めませんでした</h2>
        <p className="text-sm text-muted-foreground">通信状況を確認して、もう一度お試しください。</p>
        <button
          type="button"
          disabled={pending}
          onClick={() => startTransition(() => { router.refresh(); reset(); })}
          className="rounded-lg border border-sidebar-border px-4 py-2 text-sm hover:bg-sidebar-accent disabled:opacity-50"
        >
          {pending ? "読み込み中…" : "再読み込み"}
        </button>
        <div><NavigationLink href="/meetings" className="text-sm text-muted-foreground underline">会議一覧へ</NavigationLink></div>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

/**
 * Always create a fresh thread server-side, then redirect into it. This keeps
 * "new chat" semantics clean: every time you click + a new conversation_id
 * is minted before the first user message is sent.
 */
export default function NewChatPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const resp = await fetch("/api/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (cancelled) return;
      if (!resp.ok) {
        // Do NOT auto-redirect to /login on 401 — middleware would bounce a
        // still-authenticated user straight back here and we'd loop. Show the
        // error body so backend exceptions are visible without checking logs.
        const body = await resp.text().catch(() => "");
        const prefix =
          resp.status === 401
            ? `バックエンド認証に失敗しました (HTTP 401)`
            : `スレッドの作成に失敗しました (HTTP ${resp.status})`;
        setError(body ? `${prefix}\n${body}` : prefix);
        return;
      }
      const t = await resp.json();
      if (!cancelled) router.replace(`/c/${t.id}`);
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);
  return (
    <div className="flex-1 grid place-items-center p-6 text-sm text-muted-foreground">
      {error ? (
        <pre className="whitespace-pre-wrap break-words max-w-2xl text-destructive">
          {error}
        </pre>
      ) : (
        "新しいスレッドを作成中..."
      )}
    </div>
  );
}

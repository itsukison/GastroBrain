"use client";

/**
 * assistant-ui runtime bound to our Supabase-backed messages and the FastAPI
 * SSE stream. We own the messages array (not assistant-ui) because the same
 * data lives in Supabase and needs to round-trip through page reloads.
 *
 * Streaming invariant: the assistant placeholder message keeps the SAME id
 * across every state update during a stream. assistant-ui tracks branches by
 * observing the messages array — if we replaced the placeholder with a new
 * object that has a different id, every token would spawn a spurious branch.
 */
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { useCallback, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { parseSSE } from "@/lib/sse";
import type { Citation, MessageRow } from "@/types";

type UIMessage = ThreadMessageLike & {
  id: string;
  metadata?: { custom?: { citations?: Citation[]; query_id?: string; feedback?: number } };
};

export function RuntimeProvider({
  conversationId,
  initialMessages,
  children,
}: {
  conversationId: string;
  initialMessages: MessageRow[];
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [messages, setMessages] = useState<UIMessage[]>(() => initialMessages.map(rowToUI));
  const [isRunning, setIsRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const onCancel = useCallback(async () => {
    abortRef.current?.abort();
    setIsRunning(false);
  }, []);

  const onNew = useCallback(
    async (msg: AppendMessage) => {
      const question = textOf(msg);
      if (!question) return;

      const userMsg: UIMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content: [{ type: "text", text: question }],
      };
      const placeholderId = crypto.randomUUID();
      const placeholder: UIMessage = {
        id: placeholderId,
        role: "assistant",
        content: [{ type: "text", text: "" }],
        metadata: { custom: {} },
      };
      setMessages((prev) => [...prev, userMsg, placeholder]);

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setIsRunning(true);

      let isFirstAssistantTurnDone = false;
      let citations: Citation[] = [];
      let buffered = "";
      let lastStage = "(no events received yet)";
      const setStatus = (label: string) => {
        lastStage = label;
        if (!buffered) {
          updatePlaceholder(setMessages, placeholderId, (m) => ({
            ...m,
            content: [{ type: "text", text: `⏳ ${label}` }],
          }));
        }
      };

      try {
        const resp = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversation_id: conversationId, question }),
          signal: ctrl.signal,
        });
        if (!resp.ok || !resp.body) {
          const body = await resp.text().catch(() => "");
          throw new Error(`chat request failed: ${resp.status}${body ? ` — ${body}` : ""}`);
        }

        for await (const ev of parseSSE(resp.body, ctrl.signal)) {
          if (ev.event === "pipeline_started") {
            setStatus("接続済み — 処理を開始しています");
          } else if (ev.event === "query_rewritten") {
            setStatus("クエリを書き換え中");
          } else if (ev.event === "retrieval_started") {
            setStatus("関連ドキュメントを検索中");
          } else if (ev.event === "retrieval_done") {
            const payload = safeJSON(ev.data);
            setStatus(`候補 ${payload?.n_candidates ?? "?"} 件 — 再ランキング中`);
          } else if (ev.event === "rerank_done") {
            const payload = safeJSON(ev.data);
            citations = payload?.citations ?? [];
            updatePlaceholder(setMessages, placeholderId, (m) => ({
              ...m,
              metadata: { ...m.metadata, custom: { ...m.metadata?.custom, citations } },
            }));
          } else if (ev.event === "token") {
            const payload = safeJSON(ev.data);
            if (payload?.text) {
              buffered += payload.text;
              updatePlaceholder(setMessages, placeholderId, (m) => ({
                ...m,
                content: [{ type: "text", text: buffered }],
              }));
            }
          } else if (ev.event === "done") {
            const payload = safeJSON(ev.data);
            isFirstAssistantTurnDone = true;
            updatePlaceholder(setMessages, placeholderId, (m) => ({
              ...m,
              metadata: {
                ...m.metadata,
                custom: { ...m.metadata?.custom, citations, query_id: payload?.query_id ?? undefined },
              },
            }));
            if (payload?.message_id) {
              // Server-side row is now authoritative; swap our local id so feedback
              // calls target the right message. assistant-ui keys on the id, so
              // do this only after the stream is fully done.
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === placeholderId ? { ...m, id: payload.message_id as string } : m,
                ),
              );
            }
          } else if (ev.event === "error") {
            const payload = safeJSON(ev.data);
            updatePlaceholder(setMessages, placeholderId, (m) => ({
              ...m,
              content: [{ type: "text", text: (buffered || "") + `\n\n⚠️ エラー: ${payload?.message ?? "unknown"}` }],
            }));
          }
        }
      } catch (e) {
        if ((e as Error).name !== "AbortError") {
          const msg = (e as Error).message || String(e);
          updatePlaceholder(setMessages, placeholderId, (m) => ({
            ...m,
            content: [
              {
                type: "text",
                text:
                  (buffered || "") +
                  `\n\n⚠️ 通信エラーが発生しました。\n直近ステージ: ${lastStage}\n${msg}`,
              },
            ],
          }));
        }
      } finally {
        setIsRunning(false);
        abortRef.current = null;
      }

      // After the first assistant turn, trigger title generation in the background.
      // Compare on initial count rather than current (which now includes the new pair).
      if (isFirstAssistantTurnDone && initialMessages.length === 0) {
        fetch(`/api/threads/${conversationId}/title`, { method: "POST" })
          .then(() => router.refresh())
          .catch(() => {});
      } else if (isFirstAssistantTurnDone) {
        router.refresh();
      }
    },
    [conversationId, initialMessages.length, router],
  );

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning,
    onNew,
    onCancel,
    convertMessage: (m: UIMessage) => m,
  });

  const value = useMemo(() => runtime, [runtime]);
  return <AssistantRuntimeProvider runtime={value}>{children}</AssistantRuntimeProvider>;
}

function rowToUI(row: MessageRow): UIMessage {
  return {
    id: row.id,
    role: row.role,
    content: [{ type: "text", text: row.content }],
    metadata: {
      custom: {
        citations: row.citations ?? undefined,
        query_id: row.query_id ?? undefined,
        feedback: row.feedback ?? undefined,
      },
    },
  };
}

function textOf(msg: AppendMessage): string {
  const parts = msg.content ?? [];
  return parts
    .map((p) => ("text" in p ? p.text : ""))
    .join("")
    .trim();
}

function updatePlaceholder(
  setMessages: React.Dispatch<React.SetStateAction<UIMessage[]>>,
  id: string,
  fn: (m: UIMessage) => UIMessage,
) {
  setMessages((prev) => prev.map((m) => (m.id === id ? fn(m) : m)));
}

function safeJSON(s: string): any {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

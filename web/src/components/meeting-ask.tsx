"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { CornerDownLeft, Loader2, X } from "lucide-react";

import { Markdown } from "./markdown";
import { CitationChip } from "./citation-chip";
import { parseSSE } from "@/lib/sse";
import type { Citation } from "@/types";

/**
 * "Ask a question about this meeting" — the chat bar at the bottom of the detail
 * page, plus the answer it opens (Flownote's ChatBar + ChatAnswerModal).
 *
 * This is the ordinary chat stack pointed at a thread that belongs to the
 * meeting (§4): one thread per person per meeting, so follow-ups keep their
 * context and the Q&A tab has something to list. The backend puts the
 * transcript in front of the model for any thread with a meeting_id, so the
 * answer draws on both the meeting and the knowledge base.
 */
export function MeetingAsk({
  meetingId,
  meetingTitle,
  existingThreadId,
}: {
  meetingId: string;
  meetingTitle: string;
  existingThreadId: string | null;
}) {
  const router = useRouter();
  const threadId = useRef<string | null>(existingThreadId);

  const [question, setQuestion] = useState("");
  const [open, setOpen] = useState(false);
  const [asked, setAsked] = useState("");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function ensureThread(): Promise<string> {
    if (threadId.current) return threadId.current;
    const resp = await fetch("/api/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: `${meetingTitle} への質問`, meeting_id: meetingId }),
    });
    if (!resp.ok) throw new Error("スレッドを作成できませんでした");
    const data = (await resp.json()) as { id: string };
    threadId.current = data.id;
    return data.id;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;

    setAsked(q);
    setQuestion("");
    setAnswer("");
    setCitations([]);
    setError(null);
    setOpen(true);
    setBusy(true);

    try {
      const conversationId = await ensureThread();
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: conversationId, question: q }),
      });
      if (!resp.ok || !resp.body) throw new Error(`回答を取得できませんでした (${resp.status})`);

      for await (const ev of parseSSE(resp.body)) {
        if (ev.event === "rerank_done") {
          const payload = JSON.parse(ev.data) as { citations: Citation[] };
          setCitations(payload.citations ?? []);
        } else if (ev.event === "token") {
          const payload = JSON.parse(ev.data) as { text: string };
          setAnswer((a) => a + payload.text);
        } else if (ev.event === "error") {
          const payload = JSON.parse(ev.data) as { message: string };
          setError(payload.message);
        }
      }
      // The Q&A tab is server-rendered from the thread list; refresh so a first
      // question shows up there without a manual reload.
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form
        onSubmit={submit}
        className="sticky bottom-0 bg-background/95 backdrop-blur pt-3 pb-4 border-t border-sidebar-border"
      >
        <div className="flex items-center gap-2">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="この会議について質問する"
            className="flex-1 h-10 px-3.5 rounded-xl border border-sidebar-border bg-transparent text-[13px] text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition"
          />
          <button
            type="submit"
            disabled={!question.trim() || busy}
            className="h-10 px-3.5 rounded-xl bg-primary text-primary-foreground text-[13px] font-medium disabled:opacity-40 transition"
          >
            {busy ? (
              <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
            ) : (
              <CornerDownLeft className="w-4 h-4" aria-hidden />
            )}
            <span className="sr-only">送信</span>
          </button>
        </div>
      </form>

      {open && (
        <div
          className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
          role="dialog"
          aria-modal="true"
          onClick={() => !busy && setOpen(false)}
        >
          <div
            className="w-full max-w-2xl max-h-[80vh] overflow-y-auto scrollbar-thin rounded-2xl border border-sidebar-border bg-background p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start gap-3 mb-4">
              <p className="flex-1 text-[14px] font-medium text-foreground">{asked}</p>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="h-7 w-7 grid place-items-center rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition"
                aria-label="閉じる"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {error && <p className="text-[13px] text-destructive mb-3">{error}</p>}

            {!answer && !error && (
              <p className="text-[13px] text-muted-foreground flex items-center gap-2">
                <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />
                回答を作成しています…
              </p>
            )}

            {answer && (
              <div className="text-[13px] text-foreground">
                <Markdown text={answer} citations={citations} />
              </div>
            )}

            {citations.length > 0 && (
              <div className="mt-5 pt-4 border-t border-sidebar-border">
                <p className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
                  出典
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {citations.map((c) => (
                    <CitationChip key={c.n} citation={c} />
                  ))}
                </div>
              </div>
            )}

            {threadId.current && !busy && (
              <Link
                href={`/c/${threadId.current}`}
                className="inline-block mt-5 text-[12px] text-muted-foreground hover:text-foreground transition"
              >
                チャットで続ける →
              </Link>
            )}
          </div>
        </div>
      )}
    </>
  );
}

"use client";

import { useState } from "react";
import { CornerDownLeft, Loader2 } from "lucide-react";

/**
 * "Ask a question about this meeting" — the bar pinned to the bottom of the
 * detail page.
 *
 * It used to own the whole exchange, and opened the answer in a modal. That
 * modal reset itself on every question (`setAnswer("")`), so a three-question
 * conversation read as three isolated one-shots even though the backend threads
 * them; a stray click on the backdrop destroyed the answer mid-read; and the
 * backdrop hid the transcript the answer was about.
 *
 * Now this is only the input. `MeetingDetailView` owns the conversation and
 * renders it in the Q&A tab, which is where a question you asked should still
 * be tomorrow.
 */
export function MeetingAsk({
  busy,
  onSubmit,
}: {
  busy: boolean;
  onSubmit: (question: string) => void;
}) {
  const [question, setQuestion] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setQuestion("");
    onSubmit(q);
  }

  return (
    <form
      onSubmit={submit}
      className="sticky bottom-0 bg-background/95 backdrop-blur pt-3 pb-4 border-t border-sidebar-border"
    >
      <div className="flex items-center gap-2">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="この会議について質問する"
          aria-label="この会議について質問する"
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
  );
}

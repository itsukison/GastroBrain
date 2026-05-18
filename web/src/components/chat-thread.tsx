"use client";

import {
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useMessage,
} from "@assistant-ui/react";
import { ArrowUp, Square, ThumbsDown, ThumbsUp } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Markdown } from "./markdown";
import type { Citation } from "@/types";
import { cn } from "@/lib/cn";

export function ChatThread() {
  const viewportRef = useRef<HTMLDivElement>(null);
  const spacerRef = useRef<HTMLDivElement>(null);
  // null = pre-init (first observation does initial scroll-to-bottom for history loads)
  const userCountRef = useRef<number | null>(null);

  useEffect(() => {
    const vp = viewportRef.current;
    const spacer = spacerRef.current;
    if (!vp || !spacer) return;

    const lastUser = () =>
      vp.querySelector<HTMLElement>('[data-role="user"]:last-of-type');

    const recompute = () => {
      const last = lastUser();
      if (!last) {
        spacer.style.height = "0px";
        return;
      }
      // Spacer height = viewport - lastUserHeight - (everything below lastUser excluding spacer itself),
      // so the latest user message can be scrolled to the top of the viewport even when the response is short.
      const currentSpacerH = spacer.offsetHeight;
      const lastBottom = last.offsetTop + last.offsetHeight;
      const belowExclSpacer = vp.scrollHeight - lastBottom - currentSpacerH;
      const needed = Math.max(0, vp.clientHeight - last.offsetHeight - belowExclSpacer);
      spacer.style.height = `${needed}px`;
    };

    const onChange = () => {
      const count = vp.querySelectorAll('[data-role="user"]').length;
      const prev = userCountRef.current;
      userCountRef.current = count;
      recompute();
      if (prev === null) {
        // history load: land at the bottom of the conversation
        vp.scrollTop = vp.scrollHeight;
        return;
      }
      if (count > prev) {
        const last = lastUser();
        if (last) {
          requestAnimationFrame(() => {
            last.scrollIntoView({ block: "start", behavior: "smooth" });
          });
        }
      }
    };

    onChange();

    const ro = new ResizeObserver(recompute);
    ro.observe(vp);

    const mo = new MutationObserver(onChange);
    mo.observe(vp, { childList: true, subtree: true, characterData: true });

    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, []);

  return (
    <ThreadPrimitive.Root className="flex h-full flex-col">
      <ThreadPrimitive.Viewport
        autoScroll={false}
        ref={viewportRef}
        className="flex-1 overflow-y-auto scrollbar-thin"
      >
        <div className="mx-auto w-full max-w-3xl px-4 pt-6 pb-3 space-y-6">
          <ThreadPrimitive.Empty>
            <EmptyState />
          </ThreadPrimitive.Empty>
          <ThreadPrimitive.Messages
            components={{
              UserMessage,
              AssistantMessage,
            }}
          />
          <div ref={spacerRef} aria-hidden />
        </div>
      </ThreadPrimitive.Viewport>
      <Composer />
    </ThreadPrimitive.Root>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center text-center pt-32 select-none">
      <div className="text-3xl font-semibold mb-2">何について調べますか？</div>
      <p className="text-sm text-muted-foreground max-w-md">
        NotePMの社内ドキュメントから回答します。続けて質問もできます。
      </p>
    </div>
  );
}

function Composer() {
  return (
    <div className="relative bg-background">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 -top-6 h-6 bg-gradient-to-t from-background to-transparent"
      />
      <div className="mx-auto w-full max-w-3xl px-4 pt-2 pb-4">
        <ComposerPrimitive.Root className="flex items-end gap-2 rounded-2xl border border-border bg-card px-3 py-2 shadow-sm focus-within:ring-2 focus-within:ring-ring transition">
          <ComposerPrimitive.Input
            placeholder="質問を入力..."
            rows={1}
            className="flex-1 resize-none bg-transparent outline-none py-2 text-[15px] leading-6 max-h-48"
          />
          <ThreadPrimitive.If running>
            <ComposerPrimitive.Cancel asChild>
              <button
                aria-label="停止"
                className="h-9 w-9 rounded-full bg-secondary text-secondary-foreground hover:bg-accent grid place-items-center transition"
              >
                <Square className="w-4 h-4 fill-current" />
              </button>
            </ComposerPrimitive.Cancel>
          </ThreadPrimitive.If>
          <ThreadPrimitive.If running={false}>
            <ComposerPrimitive.Send asChild>
              <button
                aria-label="送信"
                className="h-9 w-9 rounded-full bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-40 grid place-items-center transition"
              >
                <ArrowUp className="w-4 h-4" />
              </button>
            </ComposerPrimitive.Send>
          </ThreadPrimitive.If>
        </ComposerPrimitive.Root>
        <p className="text-[11px] text-muted-foreground text-center mt-2">
          回答は社内文書に基づきますが、内容を必ずご確認ください。
        </p>
      </div>
    </div>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root data-role="user" className="flex justify-end scroll-mt-2">
      <div className="rounded-2xl bg-secondary text-secondary-foreground px-4 py-2.5 max-w-[80%] whitespace-pre-wrap leading-relaxed">
        <MessagePrimitive.Content />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="flex">
      <div className="w-full">
        <AssistantBody />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantBody() {
  const message = useMessage();
  const text = extractText(message);
  const meta = (message.metadata as { custom?: { citations?: Citation[]; query_id?: string; feedback?: number } } | undefined)?.custom ?? {};
  const citations = meta.citations ?? [];
  const queryId = meta.query_id;

  if (!text) {
    return <ThinkingIndicator />;
  }

  return (
    <div className="space-y-3">
      <div className="text-[15px]">
        <Markdown text={text} citations={citations} />
      </div>
      {citations.length > 0 && <SourceList citations={citations} />}
      {queryId && <FeedbackBar messageId={message.id} initial={meta.feedback ?? null} />}
    </div>
  );
}

function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-1 py-2 text-muted-foreground">
      <span className="w-2 h-2 rounded-full bg-current animate-pulse" />
      <span className="w-2 h-2 rounded-full bg-current animate-pulse [animation-delay:120ms]" />
      <span className="w-2 h-2 rounded-full bg-current animate-pulse [animation-delay:240ms]" />
    </div>
  );
}

function SourceList({ citations }: { citations: Citation[] }) {
  return (
    <div className="mt-4 rounded-xl border border-border bg-card/60 p-3">
      <div className="text-xs font-medium text-muted-foreground mb-2">出典</div>
      <ul className="space-y-1.5">
        {citations.map((c) => (
          <li key={c.n} className="text-xs flex items-start gap-2">
            <span className="inline-flex items-center justify-center min-w-[20px] h-[18px] px-1 rounded bg-secondary text-secondary-foreground text-[10px] font-medium">
              {c.n}
            </span>
            {c.doc_url ? (
              <a href={c.doc_url} target="_blank" rel="noreferrer" className="text-foreground hover:underline truncate">
                {c.doc_title}
              </a>
            ) : (
              <span className="truncate">{c.doc_title}</span>
            )}
            {c.heading_path.length > 0 && (
              <span className="text-muted-foreground truncate">{c.heading_path.join(" / ")}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function FeedbackBar({ messageId, initial }: { messageId: string; initial: number | null }) {
  const [rating, setRating] = useState<number | null>(initial);
  const [pending, setPending] = useState(false);

  async function submit(value: 1 | -1) {
    if (pending) return;
    setPending(true);
    const previous = rating;
    setRating(value);
    try {
      const resp = await fetch(`/api/messages/${messageId}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rating: value }),
      });
      if (!resp.ok) setRating(previous);
    } catch {
      setRating(previous);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex items-center gap-1 pt-1">
      <button
        onClick={() => submit(1)}
        className={cn(
          "h-7 w-7 grid place-items-center rounded-md text-muted-foreground hover:bg-accent transition",
          rating === 1 && "text-foreground bg-accent",
        )}
        aria-label="役立った"
      >
        <ThumbsUp className="w-3.5 h-3.5" />
      </button>
      <button
        onClick={() => submit(-1)}
        className={cn(
          "h-7 w-7 grid place-items-center rounded-md text-muted-foreground hover:bg-accent transition",
          rating === -1 && "text-foreground bg-accent",
        )}
        aria-label="改善が必要"
      >
        <ThumbsDown className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

function extractText(message: { content?: unknown }): string {
  const parts = (message.content as Array<{ type?: string; text?: string }> | undefined) ?? [];
  return parts
    .filter((p) => p?.type === "text" && typeof p.text === "string")
    .map((p) => p.text as string)
    .join("");
}

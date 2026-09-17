"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NavigationLink as Link } from "./navigation-link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  ExternalLink,
  Loader2,
  Radio,
  RefreshCw,
  Trash2,
  Users,
} from "lucide-react";

import { AgentStateToggle } from "./agent-state-toggle";
import { CitationChip } from "./citation-chip";
import { MeetingAsk } from "./meeting-ask";
import { MeetingShare } from "./meeting-share";
import { Markdown } from "./markdown";
import { parseSSE } from "@/lib/sse";
import { STATUS_LABELS, formatClock, formatDuration, formatElapsed } from "@/lib/meetings";
import { cn } from "@/lib/cn";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import type {
  AgentState,
  Citation,
  MeetingDetailResponse,
  MeetingParticipant,
  MeetingSegment,
  MessageRow,
  NextAction,
} from "@/types";

type Tab = "summary" | "transcript" | "qa";

const TABS: { key: Tab; label: string }[] = [
  { key: "summary", label: "サマリー" },
  { key: "transcript", label: "文字起こし" },
  { key: "qa", label: "Q&A" },
];

/** Live meetings gain captions continuously, and a summary is written a moment
 *  after the meeting ends — both are worth following without a manual reload. */
const POLL_MS = 10_000;

export function MeetingDetailView({ initial }: { initial: MeetingDetailResponse }) {
  const router = useRouter();
  const [data, setData] = useState(initial);
  const [tab, setTab] = useState<Tab>("summary");

  const { meeting, participants, segments, threads } = data;
  const live = meeting.status === "live" || meeting.status === "joining";
  const summaryPending = meeting.summary_status === "pending" && meeting.status === "ended";

  // The Q&A conversation. There is one thread per person per meeting (§4), and
  // `threads` is already filtered to the caller by the backend, so `threads[0]`
  // is this user's own. Holding the messages here rather than inside the ask bar
  // is what lets the Q&A tab show what you asked an hour ago.
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [qaLoaded, setQaLoaded] = useState(false);
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const threadId = useRef<string | null>(threads[0]?.id ?? null);
  const qaEnd = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const timeout = AbortSignal.timeout(15_000);
      const resp = await fetch(`/api/meetings/${meeting.id}`, { signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
      if (!resp.ok) return;
      const next = (await resp.json()) as MeetingDetailResponse;
      if (!signal?.aborted) setData(next);
    } catch {
      // Transient — the next tick tries again.
    }
  }, [meeting.id]);

  useVisiblePolling(refresh, live || summaryPending, POLL_MS);

  // Load the existing conversation once, on mount. The meeting payload carries
  // thread summaries but not their messages, so this is a second round trip —
  // paid once, and only when a thread already exists.
  useEffect(() => {
    const id = threadId.current;
    if (!id) {
      setQaLoaded(true);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const resp = await fetch(`/api/threads/${id}`);
        if (resp.ok) {
          const body = (await resp.json()) as { messages: MessageRow[] };
          if (!cancelled) setMessages(body.messages ?? []);
        }
      } catch {
        // Leave the tab empty; asking still works and will populate it.
      } finally {
        if (!cancelled) setQaLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const patchMessage = (id: string, fn: (m: MessageRow) => MessageRow) =>
    setMessages((all) => all.map((m) => (m.id === id ? fn(m) : m)));

  async function ensureThread(): Promise<string> {
    if (threadId.current) return threadId.current;
    const resp = await fetch("/api/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: `${meeting.title} への質問`, meeting_id: meeting.id }),
    });
    if (!resp.ok) throw new Error("スレッドを作成できませんでした");
    const body = (await resp.json()) as { id: string };
    threadId.current = body.id;
    return body.id;
  }

  async function ask(question: string) {
    // Switching tabs is what replaces the old modal: the answer needs somewhere
    // visible to stream into, without covering the meeting it is about.
    setTab("qa");
    setAskError(null);
    setAsking(true);

    const now = new Date().toISOString();
    const answerId = `pending-${Date.now()}`;
    setMessages((all) => [
      ...all,
      {
        id: `asked-${Date.now()}`,
        role: "user",
        content: question,
        created_at: now,
        citations: null,
        query_id: null,
        feedback: null,
      },
      {
        id: answerId,
        role: "assistant",
        content: "",
        created_at: now,
        citations: null,
        query_id: null,
        feedback: null,
      },
    ]);
    requestAnimationFrame(() =>
      qaEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" }),
    );

    try {
      const conversationId = await ensureThread();
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: conversationId, question }),
      });
      if (!resp.ok || !resp.body) throw new Error(`回答を取得できませんでした (${resp.status})`);

      for await (const ev of parseSSE(resp.body)) {
        if (ev.event === "rerank_done") {
          const payload = JSON.parse(ev.data) as { citations: Citation[] };
          patchMessage(answerId, (m) => ({ ...m, citations: payload.citations ?? null }));
        } else if (ev.event === "token") {
          const payload = JSON.parse(ev.data) as { text: string };
          patchMessage(answerId, (m) => ({ ...m, content: m.content + payload.text }));
        } else if (ev.event === "error") {
          const payload = JSON.parse(ev.data) as { message: string };
          setAskError(payload.message);
        }
      }
    } catch (err) {
      setAskError(err instanceof Error ? err.message : String(err));
    } finally {
      setAsking(false);
    }
  }

  const setAgentState = (next: AgentState) =>
    setData((d) => ({ ...d, meeting: { ...d.meeting, agent_state: next } }));

  async function rename(title: string) {
    const trimmed = title.trim();
    if (!trimmed || trimmed === meeting.title) return;
    setData((d) => ({ ...d, meeting: { ...d.meeting, title: trimmed } }));
    await fetch(`/api/meetings/${meeting.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: trimmed }),
    });
  }

  async function remove() {
    if (!confirm("この会議の記録を削除しますか？")) return;
    await fetch(`/api/meetings/${meeting.id}`, { method: "DELETE" });
    router.push("/meetings");
  }

  const duration = formatDuration(meeting.started_at, meeting.ended_at);

  return (
    <div className="h-screen overflow-y-auto scrollbar-thin bg-background">
      <div className="mx-auto max-w-3xl px-6 py-8 flex flex-col min-h-screen">
        <div className="flex items-start gap-3 mb-1">
          <Link
            href="/meetings"
            className="h-8 w-8 grid place-items-center rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition shrink-0"
            aria-label="会議一覧に戻る"
          >
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <input
            defaultValue={meeting.title}
            key={meeting.title}
            onBlur={(e) => void rename(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
            }}
            aria-label="会議名"
            className="flex-1 min-w-0 bg-transparent text-[18px] font-semibold text-foreground rounded-lg px-2 -mx-2 py-0.5 hover:bg-sidebar-accent/50 focus:bg-sidebar-accent/50 focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition"
          />
          <button
            type="button"
            onClick={() => void remove()}
            className="h-8 w-8 grid place-items-center rounded-lg text-muted-foreground hover:text-destructive transition shrink-0"
            aria-label="削除"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>

        <div className="ml-11 flex items-start gap-3 mb-5">
          <div className="flex-1 min-w-0 flex flex-wrap items-center gap-x-3 gap-y-2 pt-1 text-[12px] text-muted-foreground">
            <span>{formatClock(meeting.started_at ?? meeting.scheduled_at)}</span>
            {duration && <span>{duration}</span>}
            <span className="inline-flex items-center gap-1">
              <Users className="w-3 h-3" aria-hidden />
              {participants.length}
            </span>
            {live ? (
              <span className="inline-flex items-center gap-1.5 text-destructive font-medium">
                <Radio className="w-3.5 h-3.5 animate-pulse" aria-hidden />
                進行中
              </span>
            ) : (
              <span>{STATUS_LABELS[meeting.status]}</span>
            )}
            {meeting.meet_url && (
              <a
                href={meeting.meet_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 hover:text-foreground transition"
              >
                Meet <ExternalLink className="w-3 h-3" aria-hidden />
              </a>
            )}
            {live && (
              <AgentStateToggle
                meetingId={meeting.id}
                state={meeting.agent_state}
                onChange={setAgentState}
              />
            )}
          </div>

          {/* Right-hand action column, directly under the delete button. Keeping
              it out of the participant row below is what stops the page shifting
              when it opens, and stops the button wandering as members are added. */}
          <MeetingShare meetingId={meeting.id} onShared={() => void refresh()} />
        </div>

        <div className="ml-11 mb-5">
          <ParticipantList participants={participants} />
        </div>

        <div className="ml-11 flex items-center gap-1 border-b border-sidebar-border mb-5">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={cn(
                "px-3 py-2 text-[13px] -mb-px border-b-2 transition",
                tab === t.key
                  ? "border-foreground text-foreground font-medium"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="ml-11 flex-1">
          {tab === "summary" && (
            <SummaryTab
              meetingId={meeting.id}
              status={meeting.summary_status}
              meetingEnded={meeting.status === "ended"}
              summary={meeting.summary}
              nextActions={meeting.next_actions}
              onRegenerate={() => void refresh()}
            />
          )}
          {tab === "transcript" && <TranscriptTab segments={segments} />}
          {tab === "qa" && (
            <QaTab
              messages={messages}
              loaded={qaLoaded}
              error={askError}
              threadId={threadId.current}
              endRef={qaEnd}
            />
          )}
        </div>

        <div className="ml-11">
          <MeetingAsk busy={asking} onSubmit={(q) => void ask(q)} />
        </div>
      </div>
    </div>
  );
}

/** Who can see this meeting. Read-only — adding someone lives in `MeetingShare`,
 *  anchored above, so this row never changes shape on its own. */
function ParticipantList({ participants }: { participants: MeetingParticipant[] }) {
  if (participants.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {participants.map((p) => (
        <span
          key={p.email}
          title={p.is_organizer ? "主催者" : p.shared ? "共有されたメンバー" : "招待されたメンバー"}
          className={cn(
            "text-[11px] px-2 py-0.5 rounded-md",
            p.is_organizer
              ? "bg-secondary text-secondary-foreground"
              : "bg-sidebar-accent text-muted-foreground",
          )}
        >
          {p.email.replace("@gastroduce-japan.co.jp", "")}
          {p.shared && " ・共有"}
        </span>
      ))}
    </div>
  );
}

function SummaryTab({
  meetingId,
  status,
  meetingEnded,
  summary,
  nextActions,
  onRegenerate,
}: {
  meetingId: string;
  status: MeetingDetailResponse["meeting"]["summary_status"];
  meetingEnded: boolean;
  summary: string | null;
  nextActions: NextAction[] | null;
  onRegenerate: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function regenerate() {
    setBusy(true);
    try {
      await fetch(`/api/meetings/${meetingId}/summary`, { method: "POST" });
      onRegenerate();
    } finally {
      setBusy(false);
    }
  }

  if (!meetingEnded) {
    return (
      <p className="text-[13px] text-muted-foreground py-8">
        サマリーは会議の終了後に作成されます。
      </p>
    );
  }

  if (status === "pending" && !summary) {
    return (
      <p className="text-[13px] text-muted-foreground py-8 flex items-center gap-2">
        <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />
        サマリーを作成しています…
      </p>
    );
  }

  return (
    <div className="pb-8">
      {status === "failed" && (
        <p className="text-[13px] text-destructive mb-3">サマリーの作成に失敗しました。</p>
      )}

      {summary ? (
        <div className="text-[13px] text-foreground">
          <Markdown text={summary} />
        </div>
      ) : (
        <p className="text-[13px] text-muted-foreground">サマリーがありません。</p>
      )}

      {nextActions && nextActions.length > 0 && (
        <div className="mt-6">
          <h3 className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
            ネクストアクション
          </h3>
          <ul className="rounded-xl border border-sidebar-border overflow-hidden">
            {nextActions.map((a, i) => (
              <li
                key={i}
                className="flex items-start gap-3 px-4 py-2.5 border-t border-sidebar-border first:border-t-0 text-[13px]"
              >
                <span className="flex-1 text-foreground">{a.text}</span>
                {a.owner && <span className="text-[12px] text-muted-foreground">{a.owner}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <button
        type="button"
        onClick={() => void regenerate()}
        disabled={busy}
        className="mt-6 inline-flex items-center gap-1.5 text-[12px] text-muted-foreground hover:text-foreground transition disabled:opacity-50"
      >
        <RefreshCw className={cn("w-3.5 h-3.5", busy && "animate-spin")} aria-hidden />
        再生成
      </button>
    </div>
  );
}

function TranscriptTab({ segments }: { segments: MeetingSegment[] }) {
  // Consecutive lines from one speaker read as one turn, the way Flownote shows
  // them — caption lines break mid-sentence and a name per line is unreadable.
  const turns = useMemo(() => {
    const out: { speaker: string; spokenAt: string; lines: string[] }[] = [];
    for (const s of segments) {
      const last = out[out.length - 1];
      if (last && last.speaker === s.speaker) last.lines.push(s.text);
      else out.push({ speaker: s.speaker, spokenAt: s.spoken_at, lines: [s.text] });
    }
    return out;
  }, [segments]);

  if (segments.length === 0) {
    return <p className="text-[13px] text-muted-foreground py-8">文字起こしがありません。</p>;
  }

  const first = segments[0].spoken_at;

  return (
    <div className="space-y-4 pb-8">
      {turns.map((t, i) => (
        <div key={i} className="flex gap-3">
          <span className="w-10 shrink-0 text-[11px] text-muted-foreground tabular-nums pt-0.5">
            {formatElapsed(t.spokenAt, first)}
          </span>
          <div className="min-w-0">
            <p className="text-[12px] text-muted-foreground mb-0.5">{t.speaker}</p>
            <p className="text-[13px] text-foreground leading-relaxed">{t.lines.join(" ")}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * The questions asked about this meeting, and their answers.
 *
 * This used to list *threads* — and because there is exactly one thread per
 * person per meeting, that meant one row, forever, whose only action was to
 * navigate away. Ask fifty questions and the tab still showed one row, which is
 * why there was nowhere to see what you had already asked. It now renders the
 * conversation itself.
 */
function QaTab({
  messages,
  loaded,
  error,
  threadId,
  endRef,
}: {
  messages: MessageRow[];
  loaded: boolean;
  error: string | null;
  threadId: string | null;
  endRef: React.RefObject<HTMLDivElement | null>;
}) {
  if (!loaded) {
    return (
      <p className="text-[13px] text-muted-foreground py-8 flex items-center gap-2">
        <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />
        読み込んでいます…
      </p>
    );
  }

  if (messages.length === 0) {
    return (
      <p className="text-[13px] text-muted-foreground py-8">
        この会議についての質問はまだありません。下の入力欄から質問できます。
      </p>
    );
  }

  return (
    <div className="space-y-5 pb-8">
      {messages.map((m) =>
        m.role === "user" ? (
          <div key={m.id}>
            <p className="text-[11px] text-muted-foreground mb-1">あなた</p>
            <p className="text-[13px] font-medium text-foreground whitespace-pre-wrap">
              {m.content}
            </p>
          </div>
        ) : (
          <div key={m.id}>
            <p className="text-[11px] text-muted-foreground mb-1">商談AI</p>
            {m.content ? (
              <div className="text-[13px] text-foreground">
                <Markdown text={m.content} citations={m.citations ?? []} />
              </div>
            ) : (
              <p className="text-[13px] text-muted-foreground flex items-center gap-2">
                <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />
                回答を作成しています…
              </p>
            )}
            {m.citations && m.citations.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {m.citations.map((c) => (
                  <CitationChip key={c.n} citation={c} />
                ))}
              </div>
            )}
          </div>
        ),
      )}

      {error && <p className="text-[13px] text-destructive">{error}</p>}

      {threadId && (
        <Link
          href={`/c/${threadId}`}
          className="inline-block text-[12px] text-muted-foreground hover:text-foreground transition"
        >
          チャットで続ける →
        </Link>
      )}

      <div ref={endRef} />
    </div>
  );
}

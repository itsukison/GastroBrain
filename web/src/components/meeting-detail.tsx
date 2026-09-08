"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  ExternalLink,
  Loader2,
  Radio,
  RefreshCw,
  Trash2,
  UserPlus,
  Users,
} from "lucide-react";

import { AgentStateToggle } from "./agent-state-toggle";
import { MeetingAsk } from "./meeting-ask";
import { Markdown } from "./markdown";
import { STATUS_LABELS, formatClock, formatDuration, formatElapsed } from "@/lib/meetings";
import { cn } from "@/lib/cn";
import type {
  AgentState,
  MeetingDetailResponse,
  MeetingParticipant,
  MeetingSegment,
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

  const refresh = useCallback(async () => {
    try {
      const resp = await fetch(`/api/meetings/${meeting.id}`);
      if (!resp.ok) return;
      setData((await resp.json()) as MeetingDetailResponse);
    } catch {
      // Transient — the next tick tries again.
    }
  }, [meeting.id]);

  useEffect(() => {
    if (!live && !summaryPending) return;
    const id = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(id);
  }, [live, summaryPending, refresh]);

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

        <div className="ml-11 flex flex-wrap items-center gap-x-3 gap-y-2 text-[12px] text-muted-foreground mb-5">
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

        <div className="ml-11 mb-5">
          <ParticipantList
            meetingId={meeting.id}
            participants={participants}
            onShared={() => void refresh()}
          />
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
          {tab === "qa" && <QaTab threads={threads} />}
        </div>

        <div className="ml-11">
          <MeetingAsk
            meetingId={meeting.id}
            meetingTitle={meeting.title}
            existingThreadId={threads[0]?.id ?? null}
          />
        </div>
      </div>
    </div>
  );
}

function ParticipantList({
  meetingId,
  participants,
  onShared,
}: {
  meetingId: string;
  participants: MeetingParticipant[];
  onShared: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function share(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const resp = await fetch(`/api/meetings/${meetingId}/share`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email.trim() }),
    });
    if (!resp.ok) {
      const body = (await resp.json().catch(() => null)) as { detail?: string } | null;
      setError(body?.detail ?? "共有できませんでした");
      return;
    }
    setEmail("");
    setAdding(false);
    onShared();
  }

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

      {adding ? (
        <form onSubmit={share} className="flex items-center gap-1.5">
          <input
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@gastroduce-japan.co.jp"
            className="h-7 px-2 rounded-md border border-sidebar-border bg-transparent text-[12px] text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-sidebar-accent"
          />
          <button type="submit" className="text-[12px] text-foreground hover:underline">
            追加
          </button>
          <button
            type="button"
            onClick={() => {
              setAdding(false);
              setError(null);
            }}
            className="text-[12px] text-muted-foreground hover:text-foreground"
          >
            取消
          </button>
        </form>
      ) : (
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-sidebar-accent transition"
        >
          <UserPlus className="w-3 h-3" aria-hidden />
          共有
        </button>
      )}
      {error && <span className="text-[11px] text-destructive">{error}</span>}
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

function QaTab({ threads }: { threads: MeetingDetailResponse["threads"] }) {
  if (threads.length === 0) {
    return (
      <p className="text-[13px] text-muted-foreground py-8">
        この会議についての質問はまだありません。下の入力欄から質問できます。
      </p>
    );
  }
  return (
    <ul className="rounded-xl border border-sidebar-border overflow-hidden mb-8">
      {threads.map((t) => (
        <li key={t.id} className="border-t border-sidebar-border first:border-t-0">
          <Link
            href={`/c/${t.id}`}
            className="flex items-center gap-3 px-4 py-3 hover:bg-sidebar-accent/40 transition"
          >
            <span className="flex-1 truncate text-[13px] text-foreground">{t.title}</span>
            <span className="text-[12px] text-muted-foreground shrink-0">
              {new Date(t.updated_at).toLocaleDateString("ja-JP")}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

"use client";

import { useCallback, useEffect, useState } from "react";
import { NavigationLink as Link } from "./navigation-link";
import { BackToChatLink } from "./navigation-provider";
import { ArrowLeft, Radio, Users, Video } from "lucide-react";

import { AgentStateToggle } from "./agent-state-toggle";
import { RecallStart } from "./recall-start";
import { STATUS_LABELS, formatClock, formatDuration, groupMeetings } from "@/lib/meetings";
import { cn } from "@/lib/cn";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import type { AgentState, MeetingRow } from "@/types";

/** While something is live the page is a control surface, not a history list —
 *  poll so the toggle and the 進行中 badge do not go stale behind the user. */
const LIVE_POLL_MS = 15_000;

export function MeetingsView({ initial }: { initial: MeetingRow[] }) {
  const [meetings, setMeetings] = useState(initial);
  const hasLive = meetings.some((m) => m.status === "live" || m.status === "joining");

  useEffect(() => setMeetings(initial), [initial]);

  const refresh = useCallback(async (signal: AbortSignal) => {
    try {
      const resp = await fetch("/api/meetings?limit=100", { signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]) });
      if (!resp.ok) return;
      const data = (await resp.json()) as { meetings: MeetingRow[] };
      if (!signal.aborted) setMeetings(data.meetings ?? []);
    } catch {
      // Transient — the next tick tries again.
    }
  }, []);

  useVisiblePolling(refresh, hasLive, LIVE_POLL_MS);

  const setAgentState = (id: string, next: AgentState) =>
    setMeetings((ms) => ms.map((m) => (m.id === id ? { ...m, agent_state: next } : m)));

  const groups = groupMeetings(meetings);

  return (
    <div className="h-screen overflow-y-auto scrollbar-thin bg-background">
      <div className="mx-auto max-w-4xl px-6 py-8">
        <div className="flex items-center gap-3 mb-1">
          <BackToChatLink
            className="h-8 w-8 grid place-items-center rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition"
            aria-label="チャットに戻る"
          >
            <ArrowLeft className="w-4 h-4" />
          </BackToChatLink>
          <h1 className="text-[18px] font-semibold text-foreground flex items-center gap-2">
            <Video className="w-[18px] h-[18px]" aria-hidden />
            会議
          </h1>
        </div>
        <p className="ml-11 text-[12px] text-muted-foreground mb-6">
          商談AIが参加した会議の記録です。表示されるのは自分が招待された会議のみです。
        </p>

        <RecallStart onChange={() => void refresh(new AbortController().signal)} />
        <div className="ml-11 space-y-6">
          {groups.length === 0 && (
            <p className="text-[13px] text-muted-foreground py-12 text-center">
              会議がまだありません。Google カレンダーの招待に 商談AI
              を追加すると、ここに表示されます。
            </p>
          )}

          {groups.map((g) => (
            <section key={g.label}>
              <h2 className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
                {g.label}
              </h2>
              <ul className="rounded-xl border border-sidebar-border overflow-hidden">
                {g.items.map((m) => (
                  <MeetingListRow
                    key={m.id}
                    meeting={m}
                    onAgentState={(next) => setAgentState(m.id, next)}
                  />
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

function MeetingListRow({
  meeting,
  onAgentState,
}: {
  meeting: MeetingRow;
  onAgentState: (next: AgentState) => void;
}) {
  const live = meeting.status === "live";
  const duration = formatDuration(meeting.started_at, meeting.ended_at);

  return (
    <li className="border-t border-sidebar-border first:border-t-0">
      <Link
        href={`/meetings/${meeting.id}`}
        className="flex items-center gap-3 px-4 py-3 hover:bg-sidebar-accent/40 transition"
      >
        <span className="flex-1 min-w-0">
          <span className="block truncate text-[13px] text-foreground">{meeting.title}</span>
          <span className="block text-[12px] text-muted-foreground mt-0.5">
            {formatClock(meeting.started_at ?? meeting.scheduled_at)}
            {duration && ` ・ ${duration}`}
            <span className="inline-flex items-center gap-1 ml-2 align-middle">
              <Users className="w-3 h-3" aria-hidden />
              {meeting.participant_count}
            </span>
          </span>
        </span>

        {live ? (
          <span className="flex items-center gap-2 shrink-0">
            <span className="inline-flex items-center gap-1.5 text-[12px] text-destructive font-medium">
              <Radio className="w-3.5 h-3.5 animate-pulse" aria-hidden />
              進行中
            </span>
            <AgentStateToggle
              meetingId={meeting.id}
              state={meeting.agent_state}
              onChange={onAgentState}
            />
          </span>
        ) : (
          <span
            className={cn(
              "shrink-0 text-[11px] px-2 py-0.5 rounded-md",
              meeting.status === "failed"
                ? "bg-destructive/10 text-destructive"
                : "bg-sidebar-accent text-muted-foreground",
            )}
          >
            {STATUS_LABELS[meeting.status]}
          </span>
        )}
      </Link>
    </li>
  );
}

/**
 * Formatting and grouping for the meetings list (docs/MEETINGS_WEB.md §7.1).
 *
 * Ported from Flownote's `history/utils.ts` — the shape is agreed, so this is a
 * translation, not a redesign. Dependency-free so it can be unit-tested with
 * `npm run test`.
 */

import type { MeetingRow } from "@/types";

/** "1:05:03" / "5:02", or null while the meeting has no measured span yet. */
export function formatDuration(startedAt: string | null, endedAt: string | null): string | null {
  if (!startedAt || !endedAt) return null;
  const ms = new Date(endedAt).getTime() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
}

/** Elapsed time from the first caption, for the transcript's left gutter. */
export function formatElapsed(spokenAt: string, firstSpokenAt: string): string {
  const secs = Math.max(
    0,
    Math.floor((new Date(spokenAt).getTime() - new Date(firstSpokenAt).getTime()) / 1000),
  );
  return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
}

export const STATUS_LABELS: Record<MeetingRow["status"], string> = {
  scheduled: "予定",
  joining: "参加中",
  live: "進行中",
  ended: "終了",
  failed: "失敗",
};

/** The timestamp a meeting is filed under: when it actually ran if it ran. */
function occurredAt(m: MeetingRow): string {
  return m.started_at ?? m.scheduled_at;
}

function startOfDay(t: number): number {
  const d = new Date(t);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

export type MeetingGroup = { label: string; items: MeetingRow[] };

/**
 * Newest first, grouped by the day the meeting happened, with everything still
 * to come collected at the top under 予定 (soonest first) — that group is the
 * "what is the AI booked into next" half of §7.1.
 */
export function groupMeetings(meetings: MeetingRow[], now = new Date()): MeetingGroup[] {
  const today = startOfDay(now.getTime());
  const yesterday = today - 86_400_000;

  const upcoming: MeetingRow[] = [];
  const past: MeetingRow[] = [];
  for (const m of meetings) {
    const isFuture = new Date(m.scheduled_at).getTime() > now.getTime();
    if (isFuture && (m.status === "scheduled" || m.status === "joining")) upcoming.push(m);
    else past.push(m);
  }

  upcoming.sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at));
  past.sort((a, b) => occurredAt(b).localeCompare(occurredAt(a)));

  const groups: MeetingGroup[] = [];
  if (upcoming.length) groups.push({ label: "予定", items: upcoming });

  for (const m of past) {
    const t = new Date(occurredAt(m)).getTime();
    let label: string;
    if (t >= today) label = "今日";
    else if (t >= yesterday) label = "昨日";
    else
      label = new Date(t).toLocaleDateString("ja-JP", {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(m);
    else groups.push({ label, items: [m] });
  }
  return groups;
}

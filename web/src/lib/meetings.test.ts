import { strict as assert } from "node:assert";
import { test } from "node:test";

import { formatDuration, groupMeetings } from "./meetings.ts";
import type { MeetingRow } from "../types.ts";

const NOW = new Date("2026-09-07T12:00:00+09:00");

function meeting(over: Partial<MeetingRow> & { id: string }): MeetingRow {
  return {
    title: "会議",
    meet_url: null,
    scheduled_at: "2026-09-07T10:00:00+09:00",
    started_at: null,
    ended_at: null,
    status: "ended",
    agent_state: "asleep",
    summary_status: "ready",
    participant_count: 2,
    ...over,
  };
}

test("formatDuration renders m:ss and h:mm:ss", () => {
  assert.equal(
    formatDuration("2026-09-07T10:00:00Z", "2026-09-07T10:05:02Z"),
    "5:02",
  );
  assert.equal(
    formatDuration("2026-09-07T10:00:00Z", "2026-09-07T11:05:03Z"),
    "1:05:03",
  );
});

test("formatDuration is null until the meeting has both ends", () => {
  assert.equal(formatDuration(null, "2026-09-07T10:05:00Z"), null);
  assert.equal(formatDuration("2026-09-07T10:00:00Z", null), null);
});

test("upcoming meetings are grouped first, soonest first", () => {
  const groups = groupMeetings(
    [
      meeting({ id: "later", scheduled_at: "2026-09-09T10:00:00+09:00", status: "scheduled" }),
      meeting({ id: "sooner", scheduled_at: "2026-09-07T15:00:00+09:00", status: "scheduled" }),
    ],
    NOW,
  );
  assert.equal(groups[0].label, "予定");
  assert.deepEqual(groups[0].items.map((m) => m.id), ["sooner", "later"]);
});

test("past meetings are filed under the day they ran, newest first", () => {
  const groups = groupMeetings(
    [
      meeting({ id: "old", started_at: "2026-09-01T10:00:00+09:00" }),
      meeting({ id: "today", started_at: "2026-09-07T09:00:00+09:00" }),
      meeting({ id: "yday", started_at: "2026-09-06T09:00:00+09:00" }),
    ],
    NOW,
  );
  // The third label is an absolute date rendered in the viewer's own timezone —
  // matching the rest of the app, which never pins JST — so assert its shape,
  // not a fixed string that would only pass on a JST machine.
  assert.deepEqual(groups.slice(0, 2).map((g) => g.label), ["今日", "昨日"]);
  assert.equal(groups.length, 3);
  assert.match(groups[2].label, /年.+月.+日/);
  assert.deepEqual(groups[2].items.map((m) => m.id), ["old"]);
});

test("a meeting scheduled for the future but already live is not 予定", () => {
  // The AI joins a minute early, so scheduled_at can still be ahead of now
  // while the meeting is under way. It belongs in today's list, not 予定.
  const groups = groupMeetings(
    [meeting({ id: "live", scheduled_at: "2026-09-07T12:30:00+09:00", started_at: "2026-09-07T11:59:00+09:00", status: "live" })],
    NOW,
  );
  assert.equal(groups[0].label, "今日");
});

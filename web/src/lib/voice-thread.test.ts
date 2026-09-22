import { test } from "node:test";
import assert from "node:assert/strict";
import { createVoiceThread } from "./voice-thread.ts";
const meetingId = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
test("meeting voice creates a meeting-scoped thread", async () => {
  const calls: unknown[] = [];
  const result = await createVoiceThread(meetingId, async (_url, init) => {
    calls.push(JSON.parse(init!.body as string));
    return Response.json({ id: "thread" });
  });
  assert.deepEqual(calls, [{ meeting_id: meetingId }]);
  assert.equal(result.meetingId, meetingId);
});
test("denied and unavailable meeting threads fall back without claiming record access", async () => {
  for (const failure of [403, 404, 503, 0]) {
    const calls: unknown[] = [];
    const result = await createVoiceThread(meetingId, async (_url, init) => {
      calls.push(JSON.parse(init!.body as string));
      if (calls.length > 1) return Response.json({ id: "fallback" });
      if (!failure) throw new TypeError("offline");
      return new Response(null, { status: failure });
    });
    assert.deepEqual(calls, [{ meeting_id: meetingId }, {}]);
    assert.equal(result.meetingId, null);
    assert.equal(result.fallbackStatus, failure);
  }
});
test("ordinary voice makes one unscoped thread; total failure stays an error", async () => {
  const calls: unknown[] = [];
  await createVoiceThread(null, async (_url, init) => {
    calls.push(JSON.parse(init!.body as string));
    return Response.json({ id: "voice" });
  });
  assert.deepEqual(calls, [{}]);
  await assert.rejects(createVoiceThread(meetingId, async () => new Response(null, { status: 401 })), /401/);
});

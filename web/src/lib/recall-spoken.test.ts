import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createSpokenRecorder, type SpokenTurn } from "./recall-spoken.ts";

function fixture() {
  const saved: SpokenTurn[] = [];
  let clock = 1000;
  const record = createSpokenRecorder((turn) => saved.push(turn), () => clock);
  const audio = (type: string, id = "r1") => record({ type: `output_audio_buffer.${type}`, response_id: id });
  const done = (id = "r1", status = "completed") => record({ type: "response.done", response: {
    id, status, output: [{ id: "item", type: "message", role: "assistant", content: [{ type: "audio", transcript: "確認しました。" }] }],
  } });
  return { saved, record, audio, done, time: (next: number) => { clock = next; } };
}

test("saves each played response once with its own start/end interval", () => {
  const f = fixture();
  f.audio("started"); f.done();
  assert.equal(f.saved.length, 0);
  f.time(5000); f.audio("stopped"); f.audio("stopped"); f.done();
  f.time(6000); f.audio("started", "r2"); f.done("r2");
  f.time(8000); f.audio("stopped", "r2");
  assert.deepEqual(f.saved.map(({ item_id, spoken_at, ended_at }) => ({ item_id, spoken_at, ended_at })), [
    { item_id: "response:r1", spoken_at: new Date(1000).toISOString(), ended_at: new Date(5000).toISOString() },
    { item_id: "response:r2", spoken_at: new Date(6000).toISOString(), ended_at: new Date(8000).toISOString() },
  ]);
});

test("late generation completion keeps the original playback interval", () => {
  const f = fixture();
  f.audio("started"); f.time(3000); f.audio("stopped");
  f.time(10000); f.done();
  assert.equal(f.saved[0].ended_at, new Date(3000).toISOString());
});

test("does not sweep unrelated generated but unplayed responses", () => {
  const f = fixture(); f.done("unplayed");
  f.audio("started"); f.done(); f.time(3000); f.audio("stopped");
  assert.deepEqual(f.saved.map((s) => s.item_id), ["response:r1"]);
});

test("cleared and cancelled playback never persists the full unplayed transcript", () => {
  for (const withId of [true, false]) {
    const f = fixture(); f.audio("started"); f.done();
    f.record({ type: "output_audio_buffer.cleared", ...(withId ? { response_id: "r1" } : {}) });
    f.audio("stopped"); f.done();
    assert.equal(f.saved.length, 0);
  }
  const f = fixture(); f.audio("started"); f.done("r1", "cancelled"); f.audio("stopped");
  assert.equal(f.saved.length, 0);
});

test("missing response identity/start or text-only output is not invented as speech", () => {
  const f = fixture();
  f.record({ type: "output_audio_buffer.started" }); f.done(); f.audio("stopped");
  assert.equal(f.saved.length, 0);
  const g = fixture(); g.audio("started");
  g.record({ type: "response.done", response: { id: "r1", status: "completed", output: [
    { type: "message", role: "assistant", content: [{ type: "output_text", text: "not spoken" }] },
  ] } });
  g.audio("stopped"); assert.equal(g.saved.length, 0);
});

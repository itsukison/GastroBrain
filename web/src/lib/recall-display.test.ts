import { test } from "node:test";
import assert from "node:assert/strict";
import { createRecallAnswers, recallDisplayState, type RecallAnswerView } from "./recall-display.ts";
import type { VoiceAnswer } from "../types";

const result = (answer: string): VoiceAnswer => ({ answer, message_id: answer, query_id: null, latency_ms: 20,
  citations: [{ n: 1, doc_title: "運用ガイド", doc_url: "https://private.example", heading_path: [], snippet: "private excerpt" }] });

test("unavailable sessions override a stale open gate", () => {
  assert.equal(recallDisplayState("ready", "open").label, "応答受付中");
  assert.equal(recallDisplayState("ready", "asleep").label, "待機中");
  for (const availability of ["connecting", "reconnecting", "failed", "ended"] as const) {
    assert.notEqual(recallDisplayState(availability, "open").tone, "active");
  }
});

test("only accepted turns replace the card; late results cannot replace a newer question or session", () => {
  let view: RecallAnswerView = { answer: null, failed: false };
  const answers = createRecallAnswers(v => { view = v; });
  const currentView = (): RecallAnswerView => view;
  answers.begin();
  const old = answers.lookup();
  answers.finish(old, result("first"));
  assert.equal(view.answer?.answer, "first");
  assert.deepEqual(view.answer?.sources, [{ n: 1, title: "運用ガイド" }]);
  answers.begin(); // Includes a new room-only question: previous citations vanish.
  assert.equal(view.answer, null);
  answers.finish(old, result("late"));
  assert.equal(view.answer, null);
  const current = answers.lookup();
  answers.finish(current, { ...result("no sources"), citations: [] });
  assert.deepEqual(currentView().answer?.sources, []);
  answers.reset(); // Rotation/stop invalidates pending requests.
  answers.finish(current, result("late after stop"));
  assert.equal(view.answer, null);
});

test("lookup failure removes references and overlapping lookups cannot reverse completion order", () => {
  let view: RecallAnswerView = { answer: null, failed: false };
  const answers = createRecallAnswers(v => { view = v; });
  answers.begin();
  const old = answers.lookup();
  const latest = answers.lookup();
  answers.finish(latest, null);
  assert.deepEqual(view, { answer: null, failed: true });
  answers.finish(old, result("old success"));
  assert.deepEqual(view, { answer: null, failed: true });
});

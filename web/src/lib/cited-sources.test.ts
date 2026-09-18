import { strict as assert } from "node:assert";
import { test } from "node:test";
import { citedSources } from "./cited-sources.ts";

const sources = [{ n: 1, title: "別の会議" }, { n: 2, title: "承認規程" }];

test("meeting-only answers do not advertise unrelated retrieval candidates", () => {
  assert.deepEqual(citedSources("この会議の記録上の所要時間は30分です。", sources), []);
});

test("mixed answers keep cited company evidence without renumbering", () => {
  assert.deepEqual(citedSources("会議では予算を提案。社内規程では承認が必要。[2]", sources), [sources[1]]);
});

test("streaming adds a chip only after a complete citation and survives reload", () => {
  assert.deepEqual(citedSources("規程では承認が必要。[", sources), []);
  assert.deepEqual(citedSources("規程では承認が必要。[2", sources), []);
  const answer = "規程では承認が必要。[2]";
  const reloaded = JSON.parse(JSON.stringify(sources));
  assert.deepEqual(citedSources(answer, sources), citedSources(answer, reloaded));
});

test("repeated, adjacent and unknown citation markers do not invent sources", () => {
  assert.deepEqual(citedSources("規程[2][1]。再引用[2]。誤引用[99]。", sources), sources);
});

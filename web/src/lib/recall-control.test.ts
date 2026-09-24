import { test, type TestContext } from "node:test";
import assert from "node:assert/strict";
import { startRecallControl, type RecallControlOptions } from "./recall-control.ts";
import type { AgentState, MeetingGate } from "./meeting-mode";
import type { RecallAvailability } from "./recall-display";

const flush = () => new Promise<void>(resolve => setImmediate(resolve));
function fixture(t: TestContext) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let clock = 100_000;
  let starts = 0;
  let stops = 0;
  const commands: string[] = [];
  const writes: AgentState[] = [];
  const availability: RecallAvailability[] = [];
  let state: AgentState = "asleep";
  const gate = { get state() { return state; }, set(next: AgentState) { state = next; writes.push(next); },
    command(text: string) { commands.push(text); return "asked"; }, close() {}, ask() {} } as unknown as MeetingGate;
  const options: RecallControlOptions = { enabled: true, status: "idle", gate: { current: gate },
    onAvailability: state => availability.push(state),
    start: async () => { starts++; options.status = "live"; }, stop: () => { stops++; options.status = "ended"; } };
  const requests: { healthy: boolean; acknowledgements: string[]; observed_state: AgentState; local_state: AgentState }[] = [];
  let reply: () => Response = () => Response.json({ active: true, agent_state: "asleep", commands: [] });
  const request: typeof fetch = async (_url, init) => { requests.push(JSON.parse(init!.body as string)); return reply(); };
  const close = startRecallControl(() => options, request, () => clock);
  t.after(close);
  return { options, commands, writes, requests, gate, availability, get starts() { return starts; }, get stops() { return stops; },
    reply(fn: () => Response) { reply = fn; }, async tick(ms = 2000) { clock += ms; t.mock.timers.tick(ms); await flush(); } };
}

test("Recall rotates the same binding and restores the gate; remote updates only apply on change", async t => {
  const f = fixture(t);
  await flush();
  assert.equal(f.starts, 1);
  assert.equal(f.availability.at(-1), "ready");
  f.reply(() => Response.json({ active: true, agent_state: "open", commands: [] }));
  await f.tick();
  assert.equal(f.gate.state, "open");
  f.writes.length = 0;
  await f.tick();
  assert.deepEqual(f.writes, []);
  f.options.status = "ended"; // VoiceSession's existing 55-minute timer.
  await f.tick(55 * 60 * 1000);
  assert.equal(f.starts, 2);
  assert.ok(f.availability.includes("reconnecting"));
  assert.equal(f.gate.state, "open");
});

test("Recall sends a chat acknowledgement on the next poll and preserves local idle expiry", async t => {
  const f = fixture(t);
  await flush();
  f.reply(() => Response.json({ active: true, agent_state: "open", commands: [{ id: "question", text: "商談AI 質問" }] }));
  await f.tick();
  assert.deepEqual(f.commands, ["商談AI 質問"]);
  f.gate.set("asleep", "idle");
  f.reply(() => Response.json({ active: true, agent_state: "asleep", commands: [] }));
  await f.tick();
  assert.deepEqual(f.requests.at(-1)?.acknowledgements, ["question"]);
  assert.equal(f.requests.at(-1)?.observed_state, "open");
  assert.equal(f.requests.at(-1)?.local_state, "asleep");
  await f.tick();
  assert.deepEqual(f.requests.at(-1)?.acknowledgements, []);
});

test("revocation stops output immediately; network failure stops it after the short grace period", async t => {
  const f = fixture(t);
  await flush();
  f.reply(() => { throw new Error("offline"); });
  await f.tick(16_000);
  assert.equal(f.stops, 1);
  assert.equal(f.availability.at(-1), "reconnecting");
  f.reply(() => new Response(null, { status: 401 }));
  await f.tick();
  const requests = f.requests.length;
  await f.tick(60_000);
  assert.equal(f.requests.length, requests);
  assert.equal(f.starts, 1);
  assert.equal(f.availability.at(-1), "ended");
});

test("three failed starts bound recovery instead of reconnecting forever", async t => {
  const f = fixture(t);
  f.options.start = async () => { f.options.status = "error"; };
  await flush();
  for (let i = 0; i < 4; i++) await f.tick(16_000);
  assert.equal(f.stops, 1);
  assert.equal(f.availability.at(-1), "failed");
  const requests = f.requests.length;
  await f.tick(60_000);
  assert.equal(f.requests.length, requests);
});

test("waiting for admission is connecting, not a finished meeting", async t => {
  const f = fixture(t);
  f.reply(() => Response.json({ active: false, agent_state: "asleep", commands: [] }));
  await flush();
  await f.tick();
  assert.equal(f.availability.at(-1), "connecting");
});

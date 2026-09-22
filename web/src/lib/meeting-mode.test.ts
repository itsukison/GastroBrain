import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { RealtimeSession } from "@openai/agents-realtime";

import {
  attachMeetingGate,
  isQuietCommand,
  looksAddressed,
  meetingIdFromParams,
  parseChatCommand,
  MEETING_INSTRUCTIONS,
  meetingInstructions,
} from "./meeting-mode.ts";

type Handler = (event: unknown) => void;

/**
 * The three things the gate touches on a session: `on("transport_event")`,
 * `on("error")` and `transport.sendEvent`. Faking them keeps these tests free of
 * a network, a microphone and an OpenAI key.
 */
function fakeSession() {
  const handlers = new Map<string, Handler[]>();
  const sent: { type: string; [key: string]: unknown }[] = [];

  const session = {
    on(event: string, handler: Handler) {
      const list = handlers.get(event) ?? [];
      list.push(handler);
      handlers.set(event, list);
    },
    transport: {
      sendEvent(event: { type: string }) {
        sent.push(event);
      },
    },
  };

  const emit = (event: string, payload: unknown) => {
    for (const handler of handlers.get(event) ?? []) handler(payload);
  };

  return {
    session: session as unknown as RealtimeSession,
    /** Every `response.create` the gate has asked for. */
    answers: () => sent.filter((e) => e.type === "response.create").length,
    injected: () => sent.filter((e) => e.type === "conversation.item.create"),
    /** Somebody in the room finished a sentence. */
    said(transcript: string) {
      emit("transport_event", {
        type: "conversation.item.input_audio_transcription.completed",
        transcript,
      });
    },
    /** The agent finished answering. */
    finished() {
      emit("transport_event", { type: "response.done" });
    },
    fail() {
      emit("error", new Error("transport died"));
    },
  };
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("looksAddressed", () => {
  it("accepts question-marked and か-family endings", () => {
    assert.equal(looksAddressed("じゃあAmazonは？"), true);
    assert.equal(looksAddressed("楽天のSKU上限はいくつですか"), true);
    assert.equal(looksAddressed("それ、まだ決まってないかな"), true);
    assert.equal(looksAddressed("手数料を教えてください"), true);
  });

  it("ignores plain statements", () => {
    assert.equal(looksAddressed("来週の火曜に納品します"), false);
    assert.equal(looksAddressed("そうですね"), false);
  });

  it("ignores fragments too short to be a question", () => {
    assert.equal(looksAddressed("か"), false);
    assert.equal(looksAddressed("は？"), false);
  });
});

describe("isQuietCommand", () => {
  it("matches loosely, mid-sentence and across kana spellings", () => {
    assert.equal(isQuietCommand("静かに"), true);
    assert.equal(isQuietCommand("商談AI、ちょっと静かにしてください"), true);
    assert.equal(isQuietCommand("しずかにして"), true);
    assert.equal(isQuietCommand("だまってて"), true);
  });

  it("does not fire on unrelated speech", () => {
    assert.equal(isQuietCommand("楽天の手数料は？"), false);
    assert.equal(isQuietCommand(""), false);
  });
});

describe("attachMeetingGate", () => {
  it("answers a follow-up that does not repeat the name", async () => {
    // The regression this state machine exists for. The one-shot gate it
    // replaced required a wake word on *every* transcript, so the second
    // question below got silence.
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    s.said("商談AI、楽天のSKU上限は？");
    assert.equal(s.answers(), 1);
    assert.equal(gate.state, "open");

    s.finished();
    s.said("じゃあAmazonは？");
    assert.equal(s.answers(), 2, "follow-up without the name must still be answered");

    gate.close();
  });

  it("stays silent while asleep", () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    s.said("じゃあAmazonは？");
    s.said("来週の火曜に納品します");
    assert.equal(s.answers(), 0);
    assert.equal(gate.state, "asleep");

    gate.close();
  });

  it("goes quiet on command and stops answering follow-ups", () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    s.said("商談AI、楽天のSKU上限は？");
    s.finished();
    s.said("ちょっと静かにして");
    assert.equal(gate.state, "asleep");

    s.said("じゃあAmazonは？");
    assert.equal(s.answers(), 1, "no answers after being told to be quiet");

    gate.close();
  });

  it("lets 静かに win over the wake word in the same sentence", () => {
    // How people actually phrase it: the name, then the instruction.
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    s.said("商談AI、ちょっと静かにして");
    assert.equal(s.answers(), 0);
    assert.equal(gate.state, "asleep");

    gate.close();
  });

  it("falls asleep again after the idle window", async () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 20 });

    s.said("商談AI、楽天のSKU上限は？");
    s.finished();
    assert.equal(gate.state, "open");

    await sleep(60);
    assert.equal(gate.state, "asleep");

    s.said("じゃあAmazonは？");
    assert.equal(s.answers(), 1);

    gate.close();
  });

  it("keeps the window open while it is being used", async () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 40 });

    s.said("商談AI、楽天のSKU上限は？");
    s.finished();
    await sleep(25);
    s.said("じゃあAmazonは？"); // resets the clock
    s.finished();
    await sleep(25);

    assert.equal(gate.state, "open", "activity must postpone the timeout");
    gate.close();
  });

  it("does not start a second response while one is running", () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    s.said("商談AI、楽天のSKU上限は？");
    s.said("じゃあAmazonは？"); // no response.done in between
    assert.equal(s.answers(), 1);

    gate.close();
  });

  it("recovers from a failed response instead of going permanently deaf", () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    s.said("商談AI、楽天のSKU上限は？");
    assert.equal(s.answers(), 1);
    s.fail(); // no response.done will ever arrive

    s.said("じゃあAmazonは？");
    assert.equal(s.answers(), 2, "a transport error must not wedge the gate");

    gate.close();
  });

  it("answers a typed question from asleep, without a wake word", () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    gate.ask("楽天のSKU上限は？");

    const injected = s.injected();
    assert.equal(injected.length, 1);
    assert.deepEqual(injected[0].item, {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: "楽天のSKU上限は？" }],
    });
    assert.equal(s.answers(), 1);
    assert.equal(gate.state, "open", "typing is an unambiguous address; it wakes the agent");

    gate.close();
  });

  it("can be driven from outside", () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    gate.set("open", "chat command");
    s.said("じゃあAmazonは？");
    assert.equal(s.answers(), 1);

    gate.set("asleep", "chat command");
    s.finished();
    s.said("ほかには？");
    assert.equal(s.answers(), 1);

    gate.close();
  });
});

describe("parseChatCommand", () => {
  it("only acts on messages that open with the agent's name", () => {
    // Two colleagues talking *about* the agent must not trigger it.
    assert.equal(parseChatCommand("さっき商談AIが言ってたやつ、どう思う？"), null);
    assert.equal(parseChatCommand("楽天のSKU上限は？"), null);
    assert.equal(parseChatCommand(""), null);
  });

  it("reads a question addressed to the agent", () => {
    assert.deepEqual(parseChatCommand("商談AI 楽天のSKU上限は？"), {
      kind: "ask",
      question: "楽天のSKU上限は？",
    });
    assert.deepEqual(parseChatCommand("@商談AI 楽天のSKU上限は？"), {
      kind: "ask",
      question: "楽天のSKU上限は？",
    });
    // The name is spelled however the typist felt like spelling it.
    assert.deepEqual(parseChatCommand("しょうだんエーアイ、楽天は？"), {
      kind: "ask",
      question: "楽天は？",
    });
  });

  it("reads quiet and wake commands", () => {
    assert.deepEqual(parseChatCommand("商談AI 静かに"), { kind: "quiet" });
    assert.deepEqual(parseChatCommand("商談AI 黙ってて"), { kind: "quiet" });
    assert.deepEqual(parseChatCommand("商談AI 起きて"), { kind: "wake" });
    // The name alone is an invitation to listen.
    assert.deepEqual(parseChatCommand("商談AI"), { kind: "wake" });
  });
});

describe("gate.command", () => {
  it("drives the whole loop from Meet chat, with no wake word spoken", () => {
    const s = fakeSession();
    const gate = attachMeetingGate(s.session, { idleMs: 10_000 });

    assert.equal(gate.command("おつかれさまです"), "ignored");
    assert.equal(s.answers(), 0);

    assert.equal(gate.command("商談AI 楽天のSKU上限は？"), "ask");
    assert.equal(s.answers(), 1);
    assert.equal(gate.state, "open");

    s.finished();
    assert.equal(gate.command("商談AI 静かに"), "quiet");
    assert.equal(gate.state, "asleep");

    assert.equal(gate.command("商談AI 起きて"), "wake");
    assert.equal(gate.state, "open");
    assert.equal(s.answers(), 1, "waking is not itself a question");

    gate.close();
  });
});

describe("meetingIdFromParams", () => {
  const id = (query: string) => meetingIdFromParams(new URLSearchParams(query));

  it("reads the meeting the supervisor attached this tab to", () => {
    assert.equal(
      id("mode=meeting&meeting_id=7C9E6679-7425-40DE-944B-E07FC1F90AE7"),
      "7c9e6679-7425-40de-944b-e07fc1f90ae7",
      "normalised to lower case, because it is compared against API output",
    );
  });

  it("treats anything that is not a uuid as no meeting", () => {
    // It goes straight into a request body, so a malformed value has to read as
    // "started by hand" rather than as a request the backend must reject.
    assert.equal(id("mode=meeting"), null);
    assert.equal(id("mode=meeting&meeting_id="), null);
    assert.equal(id("mode=meeting&meeting_id=live"), null);
    assert.equal(id("mode=meeting&meeting_id=7c9e6679-7425-40de-944b"), null);
    assert.equal(meetingIdFromParams(null), null);
  });
});

describe("MEETING_INSTRUCTIONS", () => {
  // Prompt text cannot be unit-tested for behaviour, but the one rule that
  // fixed a real defect can be pinned so it is not lost in a future edit: the
  // base prompt orders every factual question through ask_gastrobrain, and
  // questions about the meeting in progress are the documented exception.
  it("keeps this-meeting questions away from the corpus tool", () => {
    assert.match(MEETING_INSTRUCTIONS, /ask_gastrobrain は呼ばない/);
    assert.match(MEETING_INSTRUCTIONS, /この会議/);
  });
});

describe("meeting instructions after reconnect", () => {
  it("makes tool precedence explicit and does not invent complete audio memory", () => {
    assert.match(MEETING_INSTRUCTIONS, /規則の例外/);
    assert.match(MEETING_INSTRUCTIONS, /再接続前の発言も聞いていたと主張しない/);
    assert.match(MEETING_INSTRUCTIONS, /話者の名前が確認できなければ断定しない/);
  });
  it("allows record lookup only for a successfully bound meeting thread", () => {
    assert.match(meetingInstructions(true), /このスレッドにはこの会議の記録が紐づいている/);
    assert.doesNotMatch(meetingInstructions(false), /このスレッドにはこの会議の記録が紐づいている/);
  });
});

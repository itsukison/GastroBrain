/**
 * Meeting mode — `/voice?mode=meeting`.
 *
 * The same voice agent, running as a participant in a Google Meet 商談 instead
 * of as a page someone is looking at. Two things change:
 *
 * 1. **Audio comes from the meeting, not the laptop.** Meetron exposes two
 *    loopback devices; we bind the session to those rather than the OS default,
 *    so the agent hears the room and speaks into the meeting.
 * 2. **It answers only when addressed.** Normal mode replies to every utterance,
 *    which in a meeting means talking over everyone. Automatic responses are
 *    turned off and every `response.create` is fired by hand, by the two-state
 *    gate below: the wake word (or a chat command) opens a window, and inside
 *    that window follow-up questions need no name.
 *
 * Everything here is opt-in: without `?mode=meeting` the page behaves exactly as
 * it did before.
 */

import type { RealtimeSession } from "@openai/agents-realtime";

import { canonicalize, findWakeWord } from "./wake-word.ts";

/**
 * Meetron's virtual devices, named from the agent's point of view. The meeting
 * arrives on "Meeting to AI"; the agent's voice leaves on "AI to Meeting", which
 * the Meet tab has selected as its microphone. Two separate devices is what
 * stops the agent from hearing itself.
 *
 * Matched as a substring: Chrome prefixes labels ("Default - …") and may append
 * a device index.
 */
export const MEETING_INPUT_LABEL = "Meetron: Meeting to AI";
export const MEETING_OUTPUT_LABEL = "Meetron: AI to Meeting";

/** True when the page was opened as a meeting participant. */
export function isMeetingMode(params: URLSearchParams | null): boolean {
  return params?.get("mode") === "meeting";
}

/**
 * `?mode=meeting&audio=default` — meeting behaviour, laptop microphone.
 *
 * For testing the wake-word gate without a meeting. Normal meeting mode listens
 * to a loopback device fed only by the Meet tab, so talking at the screen does
 * nothing: the agent is not ignoring you, it genuinely hears silence. This binds
 * the same gated session to the OS default input so the wake word can be tuned
 * against a real voice, alone, in seconds.
 *
 * Testing aid only — the real participant must never use it, or the agent
 * listens to the host machine's microphone instead of the meeting.
 */
export function usesDefaultAudio(params: URLSearchParams | null): boolean {
  return params?.get("audio") === "default";
}

export interface MeetingAudio {
  mediaStream: MediaStream;
  audioElement: HTMLAudioElement;
}

/** `setSinkId` is not in this TypeScript release's DOM lib, but Chrome has it. */
type SinkCapable = HTMLAudioElement & { setSinkId?: (id: string) => Promise<void> };

/**
 * Bind to Meetron's loopback devices.
 *
 * Throws with a message naming the missing device — in the dedicated Chrome
 * nobody is watching the screen, so the caller logs it and shows it in the
 * transcript rather than failing silently.
 */
export async function openMeetingAudio(): Promise<MeetingAudio> {
  if (!navigator.mediaDevices?.enumerateDevices) {
    throw new Error("このブラウザは音声デバイスの選択に対応していません。");
  }

  // Device *labels* are empty until the origin holds a microphone permission, so
  // without this throwaway grant the Meetron devices cannot be told apart. The
  // dedicated Chrome runs with --use-fake-ui-for-media-stream, so it is silent.
  const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
  for (const track of probe.getTracks()) track.stop();

  const devices = await navigator.mediaDevices.enumerateDevices();
  const input = devices.find(
    (d) => d.kind === "audioinput" && d.label.includes(MEETING_INPUT_LABEL),
  );
  const output = devices.find(
    (d) => d.kind === "audiooutput" && d.label.includes(MEETING_OUTPUT_LABEL),
  );
  if (!input) throw new Error(`入力デバイス「${MEETING_INPUT_LABEL}」が見つかりません。`);
  if (!output) throw new Error(`出力デバイス「${MEETING_OUTPUT_LABEL}」が見つかりません。`);

  const mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      deviceId: { exact: input.deviceId },
      // Meet has already cleaned this audio, and the loopback carries several
      // speakers at once. A second pass of echo cancellation and gain control
      // treats the far end as noise and punches holes in it.
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });

  const audioElement = document.createElement("audio") as SinkCapable;
  audioElement.autoplay = true;
  // In the DOM (not merely constructed) so Chrome treats it as a real playing
  // element; hidden because meeting mode has no visible player.
  audioElement.style.display = "none";
  document.body.append(audioElement);

  if (typeof audioElement.setSinkId !== "function") {
    throw new Error("このブラウザは出力デバイスの指定に対応していません。");
  }
  await audioElement.setSinkId(output.deviceId);

  // Publish what was actually bound. Picking the wrong input is the one failure
  // with no symptom — no error, no audio, an agent that simply never hears its
  // name — so it has to be readable from outside the page.
  document.documentElement.dataset.meetingInput = input.label;
  document.documentElement.dataset.meetingOutput = output.label;

  return { mediaStream, audioElement };
}

/**
 * Appended to the normal voice prompt. The base prompt is written for someone
 * sitting in front of the page — it greets, and it closes every turn by asking
 * "他にありますか？". Both are wrong in a 商談, so this cancels them explicitly
 * rather than relying on the model to infer the change of setting.
 */
export const MEETING_INSTRUCTIONS = `

# Meeting Mode（この設定が最優先）
- あなたは Google Meet の商談に参加している。会話は常に聞こえているが、
  発言を求められたときだけ話す。
- 発言を求められたら、直前に自分へ向けられた質問にだけ答える。
  会議の他の話題に口を挟まない。
- 挨拶・自己紹介をしない。「他にありますか？」等の促しもしない。
  答え終わったら黙る。会議の進行はあなたの仕事ではない。
- 1ターンは2文以内。参加者を待たせない。
- 一度呼ばれたあとの追加質問には、名前で呼ばれ直さなくてもそのまま答える。
  ただし答え終わったら黙る。
- 参加者は全員社内メンバー。社内の呼び名はそのまま使ってよい。
  ただし未確定の数値は必ず「未確定」と添える。`;

/** What the gate is doing right now, for the on-screen indicator. */
export type GateState = "listening" | "answering";

/**
 * Whether the agent will answer without being named.
 *
 * The predecessor to this was a doorbell: every utterance had to contain the
 * wake word or it was ignored. Real people say a name once and then keep
 * talking — 「商談AI、楽天のSKU上限は？」 followed by 「じゃあAmazonは？」 — and the
 * second question got silence. That is not a tuning problem, so the fix is not
 * a better matcher: being awake has to *persist*.
 *
 * - `asleep` — hears and transcribes everything, says nothing. The default, and
 *   also the muzzle: there is deliberately no third "muted" state, because a
 *   state that does not speak is already what muting means. The only thing a
 *   separate muzzle would add is deafness to the wake word, which is worth
 *   building only if false wakes turn out to be a real nuisance.
 * - `open` — answers anything addressed to it, no name required. Expires by
 *   itself after {@link OPEN_IDLE_MS}, so somebody who opens the agent and
 *   forgets does not leave it answering the rest of the meeting.
 */
export type AgentState = "asleep" | "open";

/**
 * How long `open` survives with nothing addressed to the agent.
 *
 * Long enough to think between follow-ups, short enough that a forgotten
 * session goes quiet before the topic has moved on.
 */
export const OPEN_IDLE_MS = 90_000;

/**
 * 「静かに」 and friends. Matched *loosely*, on purpose.
 *
 * Waking and quieting are not equally risky. A false wake interrupts the room;
 * a false quiet costs nothing, because the agent was going to be asked again
 * anyway. So the wake word is matched strictly (an exact alias, see
 * `wake-word.ts`) and this is matched as a bare substring — 「静かにしてください」
 * and 「ちょっと黙ってて」 both land.
 *
 * Canonicalised with the same transform as the wake word, so kana script,
 * width and long vowels do not matter here either.
 */
const QUIET_ALIASES: readonly string[] = ["静かに", "しずかに", "黙って", "だまって"];

const CANONICAL_QUIET = QUIET_ALIASES.map(canonicalize).filter((entry) => entry.length > 0);

/** True when the line is telling the agent to stop talking. */
export function isQuietCommand(text: string): boolean {
  if (!text) return false;
  const canonical = canonicalize(text);
  return CANONICAL_QUIET.some((needle) => canonical.includes(needle));
}

/**
 * Does this sound like it was aimed at the agent?
 *
 * Only consulted while `open`, where the name is not required and something has
 * to stand in for it. Japanese marks questions reliably at the end of the
 * sentence, so that is what we look at: a question mark, a か-family ending, or
 * an explicit 「〜ください」 request.
 *
 * Deliberately *not* "answer everything while open". In a room with several
 * people that would have the agent replying to side remarks between colleagues,
 * which is the behaviour the wake word existed to prevent in the first place.
 *
 * Known and accepted looseness: 「10個とか」 ends in か and will fire. The cost of
 * a wrong answer here is one interruption, recoverable with 「静かに」, whereas the
 * cost of missing a follow-up is the defect this whole state machine exists to
 * fix. Bias toward answering.
 *
 * Known gap: bare imperatives without ください (「まとめて」「調べて」) are not
 * matched, because 〜て is far too common mid-sentence to key on. Use the name,
 * or type it in the Meet chat.
 */
const QUESTION_TAIL =
  /(?:[?？]|か|かな|かね|かい|のか|だろうか|でしょうか|ください|下さい)[\s。．.!！]*$/u;

export function looksAddressed(text: string): boolean {
  const trimmed = text.trim();
  // Two characters cannot be a question, but can easily be a stray 「か」 from a
  // half-transcribed word.
  if (trimmed.length < 3) return false;
  return QUESTION_TAIL.test(trimmed);
}

/** 「起きて」 and friends, for a chat message that names the agent but asks nothing. */
const WAKE_COMMANDS: readonly string[] = ["起きて", "おきて", "起動", "wake", "start"];

const CANONICAL_WAKE_COMMANDS = WAKE_COMMANDS.map(canonicalize).filter((e) => e.length > 0);

export type ChatCommand =
  | { kind: "quiet" }
  | { kind: "wake" }
  | { kind: "ask"; question: string };

/**
 * Interpret one Meet chat message.
 *
 * Chat is a shared human channel, so unlike speech a message only counts when
 * it *opens* with the agent's name — otherwise 「さっき商談AIが言ってたやつ、どう？」
 * between two colleagues would be answered. Typed text has no transcription
 * error, so demanding an exact opening costs nothing and buys precision that
 * the voice path cannot have.
 *
 * Returns `null` for every message not addressed to the agent, which is most of
 * them.
 */
export function parseChatCommand(raw: string): ChatCommand | null {
  const text = raw.trim().replace(/^[@＠]\s*/u, "");
  const match = findWakeWord(text);
  // `findWakeWord` returns the last match; requiring position 0 means a message
  // that merely mentions the agent is ignored. Fails closed, which is right:
  // the cost of ignoring a chat message is that somebody retypes it.
  if (!match || match.start !== 0) return null;

  const rest = match.question;
  if (isQuietCommand(rest)) return { kind: "quiet" };
  if (!rest || CANONICAL_WAKE_COMMANDS.includes(canonicalize(rest))) return { kind: "wake" };
  return { kind: "ask", question: rest };
}

/**
 * What the page publishes on `window.meetingControl` in meeting mode.
 *
 * The Meet chat reader runs in the *meeting* tab, not this one, so a command
 * typed in chat reaches the agent by Meetron evaluating one of these against
 * this window over CDP. Keep it small and stringly-typed: it is a wire format
 * between two processes, not an internal API.
 */
export interface MeetingControl {
  state(): AgentState;
  set(next: AgentState, reason?: string): void;
  ask(text: string): void;
  /** Feed one raw Meet chat message; returns what it was taken to mean. */
  command(text: string): ChatCommand["kind"] | "ignored";
}

/** Handle returned by {@link attachMeetingGate}, for control from outside the page. */
export interface MeetingGate {
  /** Current wake state. */
  readonly state: AgentState;
  /**
   * Force a state. Used by the Meet chat reader and the web session monitor;
   * `reason` is logged, because when the agent misbehaves in a meeting the
   * first question is always "what put it in that state".
   */
  set(next: AgentState, reason: string): void;
  /**
   * Ask a question typed in the Meet chat.
   *
   * Answered whatever the state, and it wakes the agent: someone who types a
   * question has unambiguously addressed it, so there is nothing left to infer.
   * Typed text also has no transcription error, which is why this path exists
   * at all — it is the one way of reaching the agent that cannot be misheard.
   */
  ask(text: string): void;
  /**
   * Route one raw Meet chat message through {@link parseChatCommand} and act on
   * it. Kept here rather than in the caller so the chat rules are covered by the
   * same tests as the state machine.
   */
  command(text: string): ChatCommand["kind"] | "ignored";
  /** Stop the idle timer. The session's own listeners die with the session. */
  close(): void;
}

/**
 * Wire the two-state gate onto a live Realtime session.
 *
 * `createResponse: false` on the session means the model still segments and
 * transcribes turns but never replies on its own; every answer in a meeting is
 * a `response.create` sent from here.
 */
export function attachMeetingGate(
  session: RealtimeSession,
  options: {
    onState?: (state: AgentState) => void;
    onActivity?: (activity: GateState) => void;
    idleMs?: number;
  } = {},
): MeetingGate {
  const { onState, onActivity, idleMs = OPEN_IDLE_MS } = options;

  let state: AgentState = "asleep";
  // Set optimistically when the request is sent rather than on `response.created`,
  // so a second trigger arriving in that gap cannot start a second response.
  let answering = false;
  let idleTimer: ReturnType<typeof setTimeout> | null = null;

  const clearIdle = () => {
    if (idleTimer === null) return;
    clearTimeout(idleTimer);
    idleTimer = null;
  };

  const armIdle = () => {
    clearIdle();
    if (state !== "open") return;
    idleTimer = setTimeout(() => {
      idleTimer = null;
      setState("asleep", `idle ${Math.round(idleMs / 1000)}s`);
    }, idleMs);
  };

  function setState(next: AgentState, reason: string): void {
    if (state === next) {
      // Re-arming on a repeat is the point: every time the agent is addressed
      // while already open, the clock starts again.
      if (next === "open") armIdle();
      return;
    }
    state = next;
    console.info(`[meeting] ${next} (${reason})`);
    onState?.(next);
    if (next === "open") armIdle();
    else clearIdle();
  }

  const settle = () => {
    if (!answering) return;
    answering = false;
    onActivity?.("listening");
    // The window to ask a follow-up starts when the agent stops talking, not
    // when it started.
    armIdle();
  };

  const respond = (why: string): void => {
    if (answering) {
      // The API rejects `response.create` while a response is active, and in a
      // meeting the right reading of a mid-answer trigger is "wait your turn".
      console.info(`[meeting] busy, ignored: ${why}`);
      return;
    }
    answering = true;
    clearIdle();
    onActivity?.("answering");
    session.transport.sendEvent({ type: "response.create" });
  };

  const askText = (text: string): void => {
    const question = text.trim();
    if (!question) return;
    setState("open", "typed question");
    session.transport.sendEvent({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: question }],
      },
    });
    respond(`typed: ${question}`);
  };

  session.on("transport_event", (event) => {
    if (event.type === "response.done") {
      settle();
      return;
    }

    if (event.type !== "conversation.item.input_audio_transcription.completed") return;
    const transcript = (event as { transcript?: string }).transcript ?? "";
    if (!transcript.trim()) return;

    // Checked before everything else: 「静かに」 must win even when the sentence
    // also contains the agent's name or a question mark, which is exactly how
    // people phrase it — 「商談AI、ちょっと静かにして」.
    if (isQuietCommand(transcript)) {
      setState("asleep", "quiet command");
      return;
    }

    const match = findWakeWord(transcript);
    if (match) {
      setState("open", `wake word "${match.alias}"`);
      respond(transcript);
      return;
    }

    if (state === "open" && looksAddressed(transcript)) {
      respond(transcript);
    }
  });

  // Without this a failed response leaves `answering` stuck true and the agent
  // goes permanently deaf — the worst possible failure in a meeting.
  session.on("error", settle);

  return {
    get state() {
      return state;
    },
    set(next, reason) {
      setState(next, reason);
    },
    ask: askText,
    command(text) {
      const parsed = parseChatCommand(text);
      if (parsed === null) return "ignored";
      if (parsed.kind === "quiet") {
        setState("asleep", "chat: quiet");
        return "quiet";
      }
      if (parsed.kind === "wake") {
        setState("open", "chat: wake");
        return "wake";
      }
      askText(parsed.question);
      return "ask";
    },
    close: clearIdle,
  };
}

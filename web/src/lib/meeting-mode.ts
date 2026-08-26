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
 *    turned off and `response.create` is fired by hand on a wake word.
 *
 * Everything here is opt-in: without `?mode=meeting` the page behaves exactly as
 * it did before.
 */

import type { RealtimeSession } from "@openai/agents-realtime";

import { findWakeWord } from "./wake-word";

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
- 社外の相手が同席している可能性がある。社内の呼び名や未確定の数値を
  不用意に読み上げない。`;

/** What the gate is doing, for the on-screen indicator. */
export type GateState = "listening" | "answering";

/**
 * Answer only when addressed.
 *
 * With `createResponse: false` the model still segments turns and transcribes
 * them, it just does not reply. Each finished transcript is tested for the wake
 * word, and only a hit sends `response.create`.
 *
 * Returns nothing to detach: the listeners live and die with the session, which
 * the caller closes.
 */
export function attachWakeWordGate(
  session: RealtimeSession,
  onState?: (state: GateState) => void,
): void {
  // Set optimistically on send rather than waiting for `response.created`, so a
  // second wake word arriving in that gap cannot start a second response.
  let answering = false;

  const settle = () => {
    if (!answering) return;
    answering = false;
    onState?.("listening");
  };

  session.on("transport_event", (event) => {
    if (event.type === "conversation.item.input_audio_transcription.completed") {
      const transcript = (event as { transcript?: string }).transcript ?? "";
      const match = findWakeWord(transcript);
      if (!match) return;
      if (answering) {
        // The API rejects `response.create` while a response is active, and in a
        // meeting the right reading of a mid-answer call is "wait your turn".
        console.info("[meeting] wake word while answering, ignored:", transcript);
        return;
      }
      console.info(`[meeting] woken by "${match.alias}":`, transcript);
      answering = true;
      onState?.("answering");
      session.transport.sendEvent({ type: "response.create" });
      return;
    }

    if (event.type === "response.done") settle();
  });

  // Without this a failed response leaves the gate stuck on "answering" and the
  // agent goes permanently deaf — the worst possible failure in a meeting.
  session.on("error", settle);
}

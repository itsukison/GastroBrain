import type { AgentState } from "./meeting-mode";
import type { VoiceAnswer } from "../types";

export type RecallAvailability = "connecting" | "ready" | "reconnecting" | "ended" | "failed";
export type RecallAnswer = Pick<VoiceAnswer, "answer" | "message_id"> & {
  // Only intended camera content crosses this boundary. No snippets or URLs.
  sources: { n: number; title: string }[];
};
export type RecallAnswerView = { answer: RecallAnswer | null; failed: boolean };

export function recallDisplayState(availability: RecallAvailability, agentState: AgentState) {
  if (availability === "connecting") return { label: "接続準備中", detail: "まもなく会議に参加します", tone: "neutral" } as const;
  if (availability === "reconnecting") return { label: "再接続中", detail: "接続を確認しています", tone: "neutral" } as const;
  if (availability === "failed") return { label: "接続できません", detail: "会議の管理画面で接続を確認してください", tone: "error" } as const;
  if (availability === "ended") return { label: "終了しました", detail: "会議への参加を終了しました", tone: "neutral" } as const;
  return agentState === "open"
    ? { label: "応答受付中", detail: "名前を呼ばずに、続けて質問できます", tone: "active" } as const
    : { label: "待機中", detail: "会議を聞きながら、呼びかけを待っています", tone: "standby" } as const;
}

/** Accepted questions, rather than room transcription, own the card lifetime.
 * A ticket expires on a new question, a newer lookup, or session teardown. */
export function createRecallAnswers(publish: (view: RecallAnswerView) => void) {
  let turn = 0;
  let request = 0;
  let active = false;
  const clear = () => publish({ answer: null, failed: false });
  return {
    begin() { turn++; request++; active = true; clear(); },
    reset() { turn++; request++; active = false; clear(); },
    lookup() {
      const ticket = { turn, request: ++request };
      if (active) clear();
      return ticket;
    },
    finish(ticket: { turn: number; request: number }, result: VoiceAnswer | null) {
      if (!active || ticket.turn !== turn || ticket.request !== request) return;
      publish(result ? {
        failed: false,
        answer: { answer: result.answer, message_id: result.message_id,
          sources: (result.citations ?? []).map(c => ({ n: c.n, title: c.doc_title })) },
      } : { answer: null, failed: true });
    },
  };
}

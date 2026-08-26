"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  ArrowDown,
  ArrowLeft,
  BookOpen,
  ExternalLink,
  Loader2,
  MessageSquare,
  Mic,
  MicOff,
  PhoneOff,
  Search,
  X,
} from "lucide-react";
import {
  OpenAIRealtimeWebRTC,
  RealtimeAgent,
  RealtimeSession,
  tool,
  type RealtimeItem,
} from "@openai/agents-realtime";
import type { Citation, VoiceAnswer, VoiceSessionInit } from "@/types";
import { cn } from "@/lib/cn";
import {
  attachWakeWordGate,
  isMeetingMode,
  openMeetingAudio,
  usesDefaultAudio,
  MEETING_INSTRUCTIONS,
  type GateState,
} from "@/lib/meeting-mode";

type Status = "idle" | "connecting" | "live" | "ended" | "error";

type Line = {
  id: string;
  role: "user" | "assistant";
  text: string;
  pending: boolean;
};

// OpenAI hard-caps a Realtime session at 60 minutes. Close at 55 so the user
// gets a clean "session ended" instead of the transport dropping mid-sentence.
const MAX_SESSION_S = 55 * 60;
const WARN_AT_S = 45 * 60;

/** Flatten the SDK's history into displayable transcript lines. Audio turns
 * carry their text in `transcript`, which arrives incrementally. */
function toLines(history: RealtimeItem[]): Line[] {
  const out: Line[] = [];
  for (const item of history) {
    if (item.type !== "message") continue;
    if (item.role === "system") continue;
    const text = item.content
      .map((c) => {
        if (c.type === "input_text" || c.type === "output_text") return c.text;
        if (c.type === "input_audio" || c.type === "output_audio") return c.transcript ?? "";
        return "";
      })
      .join("")
      .trim();
    if (!text) continue;
    out.push({
      id: item.itemId,
      role: item.role,
      text,
      pending: item.status === "in_progress",
    });
  }
  return out;
}

/** Pull a human-readable cause out of whatever the SDK hands us.
 *
 * The realtime error event is `{ type: "error", error: unknown }`, and that
 * inner value can be an Error, a raw server event (`{code, param, message}`),
 * or a DOM Event from the RTCDataChannel — none of which survive a plain
 * `console.error(obj)`, which is why this used to log an empty object. */
function describeError(e: unknown, depth = 0): string {
  if (e === null || e === undefined) return "unknown error";
  if (typeof e === "string") return e;
  if (e instanceof Error) return e.message || e.name;
  if (typeof e === "object") {
    const o = e as Record<string, unknown>;
    if (depth < 3 && o.error && o.error !== e) return describeError(o.error, depth + 1);
    if (typeof o.message === "string" && o.message) {
      return [o.code, o.param, o.message].filter(Boolean).join(" — ");
    }
    try {
      const s = JSON.stringify(e);
      if (s && s !== "{}") return s.slice(0, 300);
    } catch {
      // circular — fall through
    }
    if (typeof o.type === "string") return `transport event: ${o.type}`;
  }
  return String(e);
}

/** Turn the raw cause into something a non-engineer can act on. */
function friendlyError(detail: string): string {
  const d = detail.toLowerCase();
  if (d.includes("notallowed") || d.includes("permission denied") || d.includes("permission dismissed")) {
    return "マイクへのアクセスが許可されていません。ブラウザのアドレスバーのマイクアイコンから許可してください。";
  }
  if (d.includes("notfound") || d.includes("requested device not found")) {
    return "マイクが見つかりません。入力デバイスを確認してください。";
  }
  if (d.includes("insufficient_quota") || d.includes("quota")) {
    return "OpenAI の利用枠が不足しています。請求設定を確認してください。";
  }
  if (d.includes("401") || d.includes("invalid") || d.includes("expired")) {
    return "認証に失敗しました。ページを再読み込みしてお試しください。";
  }
  return `接続に失敗しました: ${detail}`;
}

function mmss(total: number): string {
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** How close to the bottom still counts as "following the conversation".
 * Generous enough that a trackpad's inertial overshoot doesn't unpin. */
const STICK_THRESHOLD_PX = 64;

/** Apple's standard ease-out. Decelerates hard at the end, which is what makes
 * a panel feel like it settled rather than stopped. */
const EASE = "cubic-bezier(0.32,0.72,0,1)";

const PANEL_W = "w-[23rem]";

/** Sources behind the most recent answer.
 *
 * On the voice surface this panel is the *only* place citations appear — the
 * spoken answer has no `[N]` markers to hang chips off (the voice format block
 * forbids them), so the snippet is shown inline rather than behind a hover.
 *
 * `version` re-keys the list so a new set of sources replays the entry
 * animation instead of silently swapping text in place. */
function SourceList({
  citations,
  version,
  onDismiss,
}: {
  citations: Citation[];
  version: number;
  onDismiss: () => void;
}) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-4 pl-0.5">
        <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
          この回答の出典
        </span>
        <button
          type="button"
          onClick={onDismiss}
          className="ml-auto h-7 w-7 grid place-items-center rounded-full text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
          aria-label="出典を閉じる"
        >
          <X className="w-3.5 h-3.5" aria-hidden />
        </button>
      </div>

      <ul key={version} className="space-y-2.5">
        {citations.map((c, i) => (
          <li
            key={c.n}
            className="rounded-2xl bg-secondary px-3.5 py-3 animate-in fade-in slide-in-from-bottom-2 fill-mode-backwards"
            // Staggered so the set reads as arriving, not blinking into place.
            style={{ animationDuration: "420ms", animationDelay: `${i * 45}ms` }}
          >
            <div className="flex items-baseline gap-2 mb-1.5">
              <span className="text-[11px] font-medium tabular-nums text-muted-foreground">
                {c.n}
              </span>
              {c.doc_url ? (
                <a
                  href={c.doc_url}
                  target="_blank"
                  rel="noreferrer"
                  className="min-w-0 text-[13px] font-medium leading-snug break-words hover:underline"
                >
                  {c.doc_title}
                  <ExternalLink
                    className="inline-block w-3 h-3 ml-1 -translate-y-px text-muted-foreground"
                    aria-hidden
                  />
                </a>
              ) : (
                <span className="min-w-0 text-[13px] font-medium leading-snug break-words">
                  {c.doc_title}
                </span>
              )}
            </div>

            {c.heading_path.length > 0 && (
              <div className="text-[11px] text-muted-foreground/80 mb-2 break-words pl-[1.1rem]">
                {c.heading_path.join(" / ")}
              </div>
            )}

            <p className="text-xs leading-[1.7] text-muted-foreground whitespace-pre-wrap line-clamp-6 pl-[1.1rem]">
              {c.snippet}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function VoiceSession() {
  // Meeting mode: bound to Meetron's loopback devices, silent until addressed
  // by name. Opt-in via `?mode=meeting` so the page is unchanged for everyone
  // else. See lib/meeting-mode.ts.
  const params = useSearchParams();
  const meeting = isMeetingMode(params);
  // Testing aid: meeting behaviour on the laptop microphone. See lib/meeting-mode.ts.
  const defaultAudio = meeting && usesDefaultAudio(params);
  const [gate, setGate] = useState<GateState>("listening");

  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [searching, setSearching] = useState(false);
  const [muted, setMuted] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [conversationId, setConversationId] = useState<string | null>(null);
  // Turns actually written to `messages`. Gates 「チャットで続ける」 — the on-screen
  // transcript lives in React state only, so lines existing says nothing about
  // whether the thread has content to continue from.
  const [persistedTurns, setPersistedTurns] = useState(0);

  // Following the tail of the transcript. Mirrored into a ref because the
  // scroll effect runs on every token and must not re-subscribe on each change.
  const [pinned, setPinned] = useState(true);

  // Source panel. Closed until an answer actually cites something, dismissible,
  // and reopened by the next answer. `sourceVersion` re-keys the list so a new
  // set replays its entry animation.
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [sourceVersion, setSourceVersion] = useState(0);

  const sessionRef = useRef<RealtimeSession | null>(null);
  const conversationRef = useRef<string | null>(null);
  const answeredRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);

  const stop = useCallback(() => {
    sessionRef.current?.close();
    sessionRef.current = null;
    setSearching(false);
    setStatus((s) => (s === "error" ? s : "ended"));
  }, []);

  // Close the transport if the user navigates away mid-conversation —
  // otherwise the session keeps billing until OpenAI times it out.
  useEffect(() => () => sessionRef.current?.close(), []);

  useEffect(() => {
    if (status !== "live") return;
    const id = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(id);
  }, [status]);

  useEffect(() => {
    if (status === "live" && elapsed >= MAX_SESSION_S) stop();
  }, [status, elapsed, stop]);

  // A new citation set arrives as a new array, so dismissing stays dismissed
  // until the *next* answer — which then slides the panel back in.
  useEffect(() => {
    if (citations.length === 0) return;
    setSourceVersion((v) => v + 1);
    setSourcesOpen(true);
  }, [citations]);

  const jumpToLatest = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = true;
    setPinned(true);
    el.scrollTop = el.scrollHeight;
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX;
    if (atBottom === pinnedRef.current) return;
    pinnedRef.current = atBottom;
    setPinned(atBottom);
  }, []);

  // Keep the newest text on screen. Assignment rather than
  // `scrollIntoView({behavior:"smooth"})`: transcripts arrive token by token, and
  // a smooth animation restarts on every update, so the text visibly outruns the
  // viewport. Skipped while the user has scrolled up to read something.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [lines, searching, citations, error]);

  const start = useCallback(async () => {
    setStatus("connecting");
    setError(null);
    setLines([]);
    setCitations([]);
    setElapsed(0);
    setPersistedTurns(0);
    setPinned(true);
    setSourcesOpen(false);
    answeredRef.current = false;
    pinnedRef.current = true;

    try {
      // One conversation row per voice session, minted lazily so merely
      // opening this page doesn't litter the sidebar. Voice turns land in the
      // same `messages` table as chat, so the thread is readable afterwards.
      const threadResp = await fetch("/api/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!threadResp.ok) throw new Error(`スレッドの作成に失敗しました (HTTP ${threadResp.status})`);
      const thread = (await threadResp.json()) as { id: string };
      conversationRef.current = thread.id;
      setConversationId(thread.id);

      const initResp = await fetch("/api/voice/session", { method: "POST" });
      if (!initResp.ok) {
        const body = (await initResp.json().catch(() => ({}))) as { error?: string };
        throw new Error(body.error ?? `音声セッションの初期化に失敗しました (HTTP ${initResp.status})`);
      }
      const init = (await initResp.json()) as VoiceSessionInit;

      // The supervisor. Everything factual the agent says comes through here —
      // same retrieval + Sonnet pipeline, same ACL, as the web chat.
      const askGastrobrain = tool({
        name: "ask_gastrobrain",
        description:
          "社内資料（NotePM・Slack・Google Drive・Chatwork）に基づく回答を取得する。" +
          "会社・業務・クライアント・数値・過去の経緯に関する質問には必ずこれを使う。" +
          "返ってきた文章はそのまま読み上げること。",
        parameters: {
          type: "object",
          properties: {
            question: {
              type: "string",
              description:
                "文脈を補って自己完結させた日本語の質問文。" +
                "代名詞（それ・その件・さっきの）は具体的な名詞に展開する。",
            },
            utterance: {
              type: "string",
              description:
                "ユーザーが今話した内容を、要約や補完をせずそのまま書き起こした文。" +
                "後からチャット画面で会話を読み返すための記録として使う。",
            },
          },
          required: ["question", "utterance"],
          additionalProperties: false,
        },
        strict: true,
        execute: async (input) => {
          const { question, utterance } = input as { question: string; utterance?: string };
          const cid = conversationRef.current;
          if (!cid) return "内部エラーが発生しました。チャットからお試しください。";
          try {
            const resp = await fetch("/api/voice/ask", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ conversation_id: cid, question, utterance }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = (await resp.json()) as VoiceAnswer;
            setCitations(data.citations ?? []);
            setError(null);
            setPersistedTurns((n) => n + 1);
            // Name the thread once, after the first real answer, so voice
            // sessions are findable in the sidebar later.
            if (!answeredRef.current) {
              answeredRef.current = true;
              void fetch(`/api/threads/${cid}/title`, { method: "POST" }).catch(() => {});
            }
            return data.answer;
          } catch (e) {
            // Surface it on screen too, not just aloud. A spoken-only apology
            // is indistinguishable from "the corpus had nothing", which is how
            // an undeployed /v1/voice/ask went unnoticed for two days — every
            // turn failed and no turn was ever persisted.
            const detail = describeError(e);
            console.error("voice ask failed:", detail, e);
            setError(`社内資料の検索に失敗しました（${detail}）。この回答は記録されていません。`);
            // Returned text is spoken verbatim, so phrase it for the listener.
            return "資料の検索に失敗しました。もう一度お願いできますか。";
          }
        },
      });

      const agent = new RealtimeAgent({
        name: "Gastrobrain",
        instructions: meeting ? init.instructions + MEETING_INSTRUCTIONS : init.instructions,
        tools: [askGastrobrain],
      });

      // In meeting mode the audio is Meetron's loopback pair, not the OS
      // default. Resolved before connecting so a missing device fails as a
      // clear error rather than a session that silently hears the wrong room.
      const audio = meeting && !defaultAudio ? await openMeetingAudio() : null;

      const session = new RealtimeSession(agent, {
        model: init.model,
        ...(audio
          ? { transport: new OpenAIRealtimeWebRTC({ mediaStream: audio.mediaStream, audioElement: audio.audioElement }) }
          : {}),
        config: {
          audio: {
            input: {
              transcription: {
                model: "gpt-4o-mini-transcribe",
                language: "ja",
                prompt: init.transcriptionHint,
              },
              // Semantic VAD endpoints on meaning rather than a silence timer —
              // it stops cutting people off when they pause mid-thought, which
              // Japanese speakers do often.
              turnDetection: {
                type: "semantic_vad",
                eagerness: "auto",
                // In a meeting both must be off. `createResponse` is the wake
                // word gate — on, it answers every utterance in the room.
                // `interruptResponse` is separate: on, any cough or side remark
                // cuts the agent off mid-answer, so it never finishes a sentence.
                interruptResponse: !meeting,
                createResponse: !meeting,
              },
              noiseReduction: { type: "near_field" },
            },
            output: { voice: "marin", speed: 1.0 },
          },
        },
      });

      if (meeting) attachWakeWordGate(session, setGate);

      session.on("history_updated", (history) => setLines(toLines(history)));
      session.on("agent_tool_start", () => setSearching(true));
      session.on("agent_tool_end", () => setSearching(false));
      session.on("error", (e) => {
        const detail = describeError(e);
        console.error("realtime session error:", detail, e);
        setError(friendlyError(detail));
        // A transport error before we're live means the session never started.
        // Once live, surface the message but don't tear down the conversation.
        setStatus((s) => (s === "live" ? s : "error"));
      });

      // Store before connecting so a failed attempt is still closeable and
      // doesn't leak a half-open peer connection.
      sessionRef.current = session;
      await session.connect({ apiKey: init.clientSecret, model: init.model });
      setMuted(false);
      setStatus("live");
    } catch (e) {
      const detail = describeError(e);
      console.error("voice session failed:", detail, e);
      sessionRef.current?.close();
      sessionRef.current = null;
      setError(friendlyError(detail));
      setStatus("error");
    }
  }, [meeting, defaultAudio]);

  // Nobody is looking at this tab in a meeting — Meetron opens it in the
  // dedicated Chrome and there is no one to press 「会話を始める」.
  useEffect(() => {
    if (!meeting || status !== "idle") return;
    void start();
  }, [meeting, status, start]);

  // Machine-readable state for Meetron, which drives this tab over CDP and has
  // to know whether the agent came up. An attribute rather than on-screen text:
  // the wording here is Japanese and free to change, this contract is not.
  useEffect(() => {
    if (!meeting) return;
    const root = document.documentElement;
    root.dataset.meetingStatus = status === "live" ? gate : status;
    return () => {
      delete root.dataset.meetingStatus;
    };
  }, [meeting, status, gate]);

  const toggleMute = useCallback(() => {
    const session = sessionRef.current;
    if (!session) return;
    const next = !muted;
    session.mute(next);
    setMuted(next);
  }, [muted]);

  const live = status === "live";
  const showSources = sourcesOpen && citations.length > 0;

  return (
    <div className="flex flex-col h-dvh bg-background text-foreground">
      <header className="flex items-center gap-2 px-5 h-16 shrink-0">
        <Link
          href="/"
          className="h-8 w-8 -ml-1.5 grid place-items-center rounded-full text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
          aria-label="チャットに戻る"
        >
          <ArrowLeft className="w-4 h-4" aria-hidden />
        </Link>
        <h1 className="text-[13px] font-medium tracking-tight">
          {meeting ? "商談AI（会議モード）" : "音声で質問"}
          {/* Unmissable: this session is on the laptop mic, not the meeting. */}
          {defaultAudio && (
            <span className="ml-2 text-[11px] font-normal text-destructive">
              テスト用・PCのマイク
            </span>
          )}
        </h1>

        <div className="ml-auto flex items-center gap-3">
          {/* The only readout of the wake-word gate. Nobody watches this tab
              live, but it is what you check over CDP when the agent is silent. */}
          {meeting && live && (
            <span className="text-xs text-muted-foreground">
              {gate === "answering" ? "応答中" : "待機中（呼ばれるまで発言しません）"}
            </span>
          )}

          {live && (
            <span
              className={cn(
                "text-xs tabular-nums",
                elapsed >= WARN_AT_S ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {mmss(elapsed)}
              {elapsed >= WARN_AT_S && " / 55:00 で終了します"}
            </span>
          )}

          {/* Without this, dismissing the panel would strand the sources. */}
          {citations.length > 0 && !sourcesOpen && (
            <button
              type="button"
              onClick={() => setSourcesOpen(true)}
              className="hidden lg:inline-flex h-8 items-center gap-1.5 pl-2.5 pr-3 rounded-full text-xs text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
            >
              <BookOpen className="w-3.5 h-3.5" aria-hidden />
              出典 {citations.length}
            </button>
          )}
        </div>
      </header>

      <div className="flex-1 min-h-0 flex justify-center">
        {/* Transcript and panel are one centered spread rather than two
            edge-anchored columns. Capped just above reading-column + panel, so
            the space between them stays a gutter instead of growing with the
            viewport. */}
        <div className="flex-1 min-w-0 max-w-[63rem] flex">
          {/* Transcript. `relative` anchors the jump-to-latest button. */}
          <div className="relative flex-1 min-w-0 flex flex-col">
            <div ref={scrollRef} onScroll={onScroll} className="flex-1 overflow-y-auto">
              <div className="mx-auto max-w-[38rem] px-6 pt-8 pb-24">
                {status === "idle" && (
                  <div className="py-24 text-center">
                    <p className="text-[15px] text-muted-foreground leading-[1.9]">
                      マイクに向かって質問すると、社内資料をもとに音声で回答します。
                      <br />
                      回答の出典は画面の右側に表示されます。
                    </p>
                  </div>
                )}
  
                {error && (
                  // Tagged so Meetron can read the cause: it drives this tab
                  // headlessly, and matching on the destructive *style* picks
                  // up the 終了する button instead of the message.
                  <div
                    data-meeting-error
                    className="rounded-2xl bg-destructive/10 px-4 py-3 text-[13px] leading-relaxed text-destructive break-words"
                  >
                    {error}
                  </div>
                )}
  
                {/* Role is carried by placement and weight rather than boxes: the
                    answer is the page, the question is an aside on it. */}
                <ul className="space-y-9" aria-live="polite">
                  {lines.map((line) =>
                    line.role === "user" ? (
                      <li key={line.id} className="flex justify-end">
                        <div
                          className={cn(
                            "max-w-[85%] rounded-[1.375rem] bg-secondary px-4 py-2.5",
                            "text-[14px] leading-[1.65] text-secondary-foreground",
                            line.pending && "opacity-50",
                          )}
                        >
                          <span className="sr-only">あなた: </span>
                          {line.text}
                        </div>
                      </li>
                    ) : (
                      <li
                        key={line.id}
                        className={cn(
                          "text-[15.5px] leading-[1.85] tracking-[-0.003em]",
                          line.pending && "opacity-60",
                        )}
                      >
                        <span className="sr-only">Gastrobrain: </span>
                        {line.text}
                      </li>
                    ),
                  )}
                </ul>
  
                {searching && (
                  <div className="mt-6 flex items-center gap-2 text-xs text-muted-foreground">
                    <Search className="w-3.5 h-3.5 animate-pulse" aria-hidden />
                    社内資料を検索しています…
                  </div>
                )}
  
                {/* Below lg there is no room beside the transcript, so sources
                    fall back to the end of it. */}
                {citations.length > 0 && sourcesOpen && (
                  <div className="lg:hidden mt-12">
                    <SourceList
                      citations={citations}
                      version={sourceVersion}
                      onDismiss={() => setSourcesOpen(false)}
                    />
                  </div>
                )}
              </div>
            </div>
  
            {!pinned && (
              <button
                type="button"
                onClick={jumpToLatest}
                className="absolute bottom-5 left-1/2 -translate-x-1/2 h-9 pl-3 pr-3.5 rounded-full bg-secondary/90 backdrop-blur-sm text-xs font-medium inline-flex items-center gap-1.5 shadow-sm hover:bg-accent transition-colors animate-in fade-in slide-in-from-bottom-1"
              >
                <ArrowDown className="w-3.5 h-3.5" aria-hidden />
                最新へ
              </button>
            )}
          </div>
  
          {/* Sources, beside the transcript so the answer stays readable while the
              user checks where it came from.
              Width animates on the clipping wrapper while the content keeps a
              fixed width — otherwise the Japanese text re-wraps on every frame of
              the transition, which reads as a stutter rather than a slide. */}
          <aside
            className={cn(
              "hidden lg:block shrink-0 overflow-hidden",
              showSources ? PANEL_W : "w-0",
            )}
            style={{ transition: `width 480ms ${EASE}` }}
            // Clipped by `w-0`, not `display:none`, so without `inert` the links
            // and the close button stay in the tab order while invisible.
            inert={!showSources}
          >
            <div
              className={cn(
                PANEL_W,
                "h-full overflow-y-auto scrollbar-thin pr-6 pl-2 pt-8 pb-24",
                showSources ? "opacity-100 translate-x-0" : "opacity-0 translate-x-5",
              )}
              style={{ transition: `opacity 380ms ${EASE}, transform 480ms ${EASE}` }}
            >
              <SourceList
                citations={citations}
                version={sourceVersion}
                onDismiss={() => setSourcesOpen(false)}
              />
            </div>
          </aside>
        </div>
      </div>

      {/* No top rule — the transcript's pb-24 keeps text clear of the controls,
          so the separation is space rather than a line. */}
      <footer className="px-5 pb-7 pt-2 shrink-0">
        <div className="flex items-center justify-center gap-2.5">
          {!live ? (
            <button
              type="button"
              onClick={start}
              disabled={status === "connecting"}
              className="h-11 px-6 rounded-full bg-primary text-primary-foreground text-[13px] font-medium inline-flex items-center gap-2 hover:opacity-90 disabled:opacity-60 transition-opacity"
            >
              {status === "connecting" ? (
                <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
              ) : (
                <Mic className="w-4 h-4" aria-hidden />
              )}
              {status === "connecting"
                ? "接続中…"
                : status === "idle"
                  ? "会話を始める"
                  : "もう一度話す"}
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={toggleMute}
                className={cn(
                  "h-11 w-11 grid place-items-center rounded-full transition-colors",
                  muted
                    ? "bg-foreground text-background"
                    : "bg-secondary text-foreground hover:bg-accent",
                )}
                aria-label={muted ? "ミュート解除" : "ミュート"}
                title={muted ? "ミュート解除" : "ミュート"}
              >
                {muted ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
              </button>
              <button
                type="button"
                onClick={stop}
                className="h-11 px-6 rounded-full bg-destructive text-white text-[13px] font-medium inline-flex items-center gap-2 hover:opacity-90 transition-opacity"
              >
                <PhoneOff className="w-4 h-4" aria-hidden />
                終了する
              </button>
            </>
          )}

          {conversationId && persistedTurns > 0 && (
            <Link
              href={`/c/${conversationId}`}
              className="h-11 px-5 rounded-full bg-secondary text-[13px] inline-flex items-center gap-2 hover:bg-accent transition-colors"
            >
              <MessageSquare className="w-4 h-4" aria-hidden />
              チャットで続ける
            </Link>
          )}
        </div>
      </footer>
    </div>
  );
}

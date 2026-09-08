export type Citation = {
  n: number;
  doc_title: string;
  doc_url: string | null;
  heading_path: string[];
  snippet: string;
};

export type ThreadSummary = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type MessageRow = {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  citations: Citation[] | null;
  query_id: string | null;
  feedback: number | null;
};

export type Department =
  | "consulting"
  | "sales"
  | "content"
  | "dev"
  | "backoffice"
  | "other";

export type UserPreferences = {
  department: Department | null;
  extra_note: string | null;
  updated_at: string | null;
};

/** Response of POST /api/voice/session — everything the browser needs to open
 * a Realtime session. `clientSecret` is short-lived and single-use. */
export type VoiceSessionInit = {
  clientSecret: string;
  expiresAt: number | null;
  model: string;
  instructions: string;
  transcriptionHint: string;
};

/** Response of POST /api/voice/ask — one supervisor turn. `answer` is spoken
 * verbatim by the voice agent; `citations` are rendered on screen. */
export type VoiceAnswer = {
  answer: string;
  citations: Citation[];
  message_id: string;
  query_id: string | null;
  latency_ms: number;
};

export type ChatStreamEvent =
  | { event: "query_rewritten"; data: { original: string; rewritten: string } }
  | { event: "retrieval_started"; data: Record<string, never> }
  | { event: "retrieval_done"; data: { n_candidates: number } }
  | { event: "rerank_done"; data: { n_chunks: number; citations: Citation[] } }
  | { event: "token"; data: { text: string } }
  | {
      event: "done";
      data: {
        message_id: string;
        query_id: string | null;
        latency_ms: number;
        input_tokens: number;
        output_tokens: number;
        cost_jpy: number;
      };
    }
  | { event: "error"; data: { message: string } };

// ── Meetings (商談AI) ──────────────────────────────────────────────────────
// See docs/MEETINGS_WEB.md. `status` and `agent_state` mirror the CHECK
// constraints in migrations/014_meetings.sql; `agent_state` has two values on
// purpose (§6.4) and its 90 s expiry is enforced on the meeting side.

export type MeetingStatus = "scheduled" | "joining" | "live" | "ended" | "failed";
export type AgentState = "asleep" | "open";
export type SummaryStatus = "pending" | "ready" | "failed";

export type MeetingRow = {
  id: string;
  title: string;
  meet_url: string | null;
  scheduled_at: string;
  started_at: string | null;
  ended_at: string | null;
  status: MeetingStatus;
  agent_state: AgentState;
  summary_status: SummaryStatus;
  participant_count: number;
};

export type NextAction = { text: string; owner: string };

export type MeetingDetail = MeetingRow & {
  summary: string | null;
  next_actions: NextAction[] | null;
};

export type MeetingSegment = {
  seq: number;
  speaker: string;
  text: string;
  spoken_at: string;
};

export type MeetingParticipant = {
  email: string;
  is_organizer: boolean;
  /** Added by hand through "共有" rather than by the Calendar invite. */
  shared: boolean;
};

/** GET /api/meetings/[id]. `threads` is only ever the caller's own Q&A threads
 *  about this meeting — one thread per person (§4). */
export type MeetingDetailResponse = {
  meeting: MeetingDetail;
  participants: MeetingParticipant[];
  segments: MeetingSegment[];
  threads: ThreadSummary[];
};

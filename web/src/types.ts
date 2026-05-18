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

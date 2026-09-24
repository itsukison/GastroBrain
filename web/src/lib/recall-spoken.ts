export type SpokenTurn = { item_id: string; text: string; spoken_at: string; ended_at: string };

type Playback = { start?: number; end?: number; text?: string; cancelled?: boolean; saved?: boolean };
function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}

/** Raw transport events carry response_id (the SDK's history does not).
 * Generation completion and playback completion may arrive in either order.
 * Never save an interrupted response's full, partly unplayed transcript.
 */
export function createSpokenRecorder(save: (turn: SpokenTurn) => void, now = Date.now) {
  const responses = new Map<string, Playback>();
  return (value: unknown) => {
    const event = object(value);
    const response = object(event.response);
    const id = event.type === "response.done" ? response.id : event.response_id;
    if (event.type === "output_audio_buffer.cleared") {
      // Older SDK schemas omit response_id on cleared; invalidate active output.
      for (const [key, turn] of responses) {
        if (key === id || (typeof id !== "string" && turn.start !== undefined && turn.end === undefined)) turn.cancelled = true;
      }
      return;
    }
    if (typeof id !== "string" || !["response.done", "output_audio_buffer.started", "output_audio_buffer.stopped"].includes(String(event.type))) return;
    const turn = responses.get(id) ?? {};
    responses.set(id, turn);
    // Session lifetimes are bounded; also cap retained event IDs defensively.
    if (responses.size > 512) responses.delete(responses.keys().next().value!);
    if (event.type === "output_audio_buffer.started") turn.start ??= now();
    if (event.type === "output_audio_buffer.stopped") turn.end ??= now();
    if (event.type === "response.done") {
      if (response.status !== "completed") turn.cancelled = true;
      const output = Array.isArray(response.output) ? response.output : [];
      turn.text = output.flatMap((value) => {
        const item = object(value);
        if (item.type !== "message" || item.role !== "assistant") return [];
        return (Array.isArray(item.content) ? item.content : []).flatMap((part) => {
          const content = object(part);
          return ["audio", "output_audio"].includes(String(content.type)) && typeof content.transcript === "string" ? [content.transcript] : [];
        });
      }).join("\n").trim();
    }
    if (!turn.saved && !turn.cancelled && turn.text && turn.start !== undefined && turn.end !== undefined && turn.end >= turn.start) {
      turn.saved = true;
      save({ item_id: `response:${id}`, text: turn.text, spoken_at: new Date(turn.start).toISOString(), ended_at: new Date(turn.end).toISOString() });
    }
  };
}

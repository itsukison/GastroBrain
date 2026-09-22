/** Bind in-meeting Q&A to the selected record, or keep voice available with an
 * explicitly unbound thread. Only a successful bind may advertise record access.
 */
export async function createVoiceThread(meetingId: string | null, fetchImpl = fetch) {
  async function create(id: string | null) {
    try {
      const response = await fetchImpl("/api/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(id ? { meeting_id: id } : {}),
      });
      if (!response.ok) return { id: null, status: response.status };
      const body = await response.json() as { id?: string };
      return { id: typeof body.id === "string" && body.id ? body.id : null, status: response.status };
    } catch {
      return { id: null, status: 0 };
    }
  }
  const bound = await create(meetingId);
  if (bound.id) return { id: bound.id, meetingId, fallbackStatus: null };
  if (meetingId) {
    const fallback = await create(null);
    if (fallback.id) return { id: fallback.id, meetingId: null, fallbackStatus: bound.status };
  }
  throw new Error(`スレッドの作成に失敗しました (HTTP ${bound.status})`);
}

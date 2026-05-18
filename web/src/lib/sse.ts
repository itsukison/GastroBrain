/**
 * Minimal SSE parser for `fetch` Response.body streams. The browser's native
 * EventSource doesn't support POST + custom headers, so we use fetch + manual
 * parsing.
 *
 * Yields {event, data} pairs. `data` is the raw string (caller parses JSON).
 */
export async function* parseSSE(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<{ event: string; data: string }> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  // Per the EventSource spec, line terminators are LF, CR, or CRLF, and event
  // boundaries are two terminators in a row. sse_starlette emits CRLF, so we
  // must match \r\n\r\n as well as \n\n. We normalize CRLF -> LF on ingest so
  // the rest of the parser only deals with one form.
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException("aborted", "AbortError");
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      buf = buf.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      let sep: number;
      while ((sep = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        let event = "message";
        const dataLines: string[] = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
        }
        if (dataLines.length) yield { event, data: dataLines.join("\n") };
      }
    }
  } finally {
    reader.releaseLock();
  }
}

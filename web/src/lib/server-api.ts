/** Server-side helpers for Server Components — same auth path as `forward`,
 *  but returns parsed JSON instead of piping a Response. */
import { backend } from "@/lib/api";

export class BackendReadError extends Error {
  constructor(public readonly status: number) {
    super(`Backend read failed: HTTP ${status}`);
  }
}

export async function backendGet<T>(path: string): Promise<T> {
  const { base, token } = await backend();
  const resp = await fetch(`${base}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    // Bound normal page reads; streaming chat/voice use forward(), not this helper.
    signal: AbortSignal.timeout(15_000),
  });
  if (!resp.ok) {
    throw new BackendReadError(resp.status);
  }
  return (await resp.json()) as T;
}

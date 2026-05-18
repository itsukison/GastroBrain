/** Server-side helpers for Server Components — same auth path as `forward`,
 *  but returns parsed JSON instead of piping a Response. */
import { backend } from "@/lib/api";

export async function backendGet<T>(path: string): Promise<T> {
  const { base, token } = await backend();
  const resp = await fetch(`${base}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`backend GET ${path} failed: ${resp.status}`);
  }
  return (await resp.json()) as T;
}

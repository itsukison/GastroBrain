/** Retrieval candidates retain their original numbers for streaming and reload.
 * Only sources referenced by the answer belong in its visible source list.
 */
export function citedSources<T extends { n: number }>(text: string, sources: T[]): T[] {
  const used = new Set(Array.from(text.matchAll(/\[(\d+)\]/g), (m) => Number(m[1])));
  return sources.filter((source) => used.has(source.n));
}

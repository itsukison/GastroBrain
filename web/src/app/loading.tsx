export default function Loading() {
  return (
    <div className="min-h-screen grid place-items-center" role="status">
      <span className="text-sm text-muted-foreground motion-safe:animate-pulse">読み込み中…</span>
    </div>
  );
}

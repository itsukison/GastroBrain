"use client";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useVisiblePolling } from "@/lib/use-visible-polling";

type Run = { id: string; meeting_id: string; title: string; state: string; problem: string | null };
const labels: Record<string, string> = { creating: "準備中", uncertain: "参加状況を確認中", joining: "入室待ち", live: "参加中",
  stopping: "退出中", finishing: "記録を処理中", done: "完了", failed: "要確認" };
const problems: Record<string, string> = { captions_not_received: "字幕をまだ受信していません", voice_unhealthy: "音声接続が切れました",
  create_refused: "参加リクエストが拒否されました。管理者に連絡してください",
  transcript_incomplete: "記録を完了できませんでした", create_unresolved: "参加状況を確認できません。管理者に連絡してください",
  create_unconfirmed: "参加状況を確認しています", recovery_pending: "接続を再確認しています", bot_fatal: "入室・接続に失敗しました",
  recording_failed: "記録に失敗しました", duplicate_bots: "重複した参加を停止しました", deadline: "制限時間に達しました" };

export function RecallStart({ onChange }: { onChange: () => void }) {
  const [enabled, setEnabled] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch("/api/recall/runs", { signal, cache: "no-store" });
    if (!response.ok) throw new Error("参加状況を取得できませんでした");
    const data = await response.json();
    setEnabled(data.enabled); setRuns(data.runs);
  }, []);
  useEffect(() => { void refresh().catch(() => {}); }, [refresh]);
  useVisiblePolling(refresh, enabled, 5000);
  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/recall/runs", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ meet_url: String(form.get("url")).trim(), title: String(form.get("title")).trim(),
          attendees: String(form.get("attendees")).split(/[,\s]+/).filter(Boolean) }) });
      if (!response.ok) throw new Error(response.status === 409 ? "この会議には既にAIが参加しています" : "参加を開始できませんでした。URLと入力内容を確認してください");
      await refresh(); onChange();
    } catch (e) { setError(e instanceof Error ? e.message : "接続に失敗しました"); }
    finally { setBusy(false); }
  }
  async function stop(id: string) {
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/recall/runs/${id}/stop`, { method: "POST" });
      if (!response.ok) throw new Error("退出を依頼できませんでした");
      await refresh(); onChange();
    } catch (e) { setError(e instanceof Error ? e.message : "接続に失敗しました"); }
    finally { setBusy(false); }
  }
  if (!enabled) return null;
  return <section className="ml-11 mb-6 rounded-xl border border-sidebar-border p-4 text-sm">
    <h2 className="font-medium mb-3">商談AIをGoogle Meetに招待</h2>
    <form onSubmit={start} className="grid gap-3">
      <label>会議名<input name="title" required maxLength={200} defaultValue="商談AI テスト会議" className="block w-full border rounded p-2 mt-1" /></label>
      <label>Google Meet URL<input name="url" type="url" required placeholder="https://meet.google.com/abc-defg-hij" className="block w-full border rounded p-2 mt-1" /></label>
      <label>記録を共有する参加者のメール<input name="attendees" placeholder="カンマ区切り・自分は自動追加" className="block w-full border rounded p-2 mt-1" /></label>
      <p className="text-xs text-muted-foreground">会議でAIを承認してください。音声と字幕を使って回答・記録します。</p>
      <button disabled={busy} className="justify-self-start border rounded px-4 py-2 disabled:opacity-50">参加を開始</button>
    </form>
    {error && <p role="alert" className="mt-3 text-destructive">{error}</p>}
    <ul className="mt-4 space-y-3">{runs.slice(0, 5).map(run => <li key={run.id}>
      <a href={`/meetings/${run.meeting_id}`} className="underline">{run.title}</a> · {labels[run.state] ?? run.state}
      {!["done", "failed", "finishing", "stopping"].includes(run.state) && <button disabled={busy} className="ml-3 underline" onClick={() => void stop(run.id)}>退出</button>}
      {run.problem && <p className="text-xs text-destructive mt-1">{problems[run.problem] ?? "接続状況を確認してください"}</p>}
    </li>)}</ul>
  </section>;
}

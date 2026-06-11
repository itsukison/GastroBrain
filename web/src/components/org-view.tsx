"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowLeft, BookOpen, Globe, Lock, Search } from "lucide-react";

// ── types ─────────────────────────────────────────────────────────────────
export type AccessNote = { name: string; n_docs: number; is_public: boolean };
export type MyAccess = { total_notes: number; total_docs: number; notes: AccessNote[] };

// Read-only, self-service view: the NotePM notebooks the signed-in user can
// access, derived from their NotePM permissions. NotePM is the source of truth,
// so there is nothing to edit here.
export function AccessView({ email, initial }: { email: string; initial: MyAccess | null }) {
  const [data, setData] = useState<MyAccess | null>(initial);
  const [q, setQ] = useState("");

  // Client fetch fallback if the server prefetch didn't populate.
  useEffect(() => {
    if (initial !== null) return;
    fetch("/api/org/me/access")
      .then((r) => (r.ok ? r.json() : null))
      .then((d: MyAccess | null) => d && setData(d))
      .catch(() => setData({ total_notes: 0, total_docs: 0, notes: [] }));
  }, [initial]);

  const filtered = useMemo(
    () => (data?.notes ?? []).filter((n) => n.name.toLowerCase().includes(q.toLowerCase())),
    [data, q],
  );

  return (
    <div className="h-screen overflow-y-auto scrollbar-thin bg-background">
      <div className="mx-auto max-w-4xl px-6 py-8">
        <div className="flex items-center gap-3 mb-1">
          <Link
            href="/"
            className="h-8 w-8 grid place-items-center rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition"
            aria-label="チャットに戻る"
          >
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <h1 className="text-[18px] font-semibold text-foreground flex items-center gap-2">
            <BookOpen className="w-[18px] h-[18px]" aria-hidden />
            アクセスできる資料
          </h1>
        </div>
        <p className="ml-11 text-[12px] text-muted-foreground mb-6">{email}</p>

        <div className="ml-11">
          <p className="text-[12px] text-muted-foreground leading-relaxed mb-4">
            あなたが閲覧できる NotePM のノートブックの一覧です。アクセス権限は NotePM
            の設定に基づいて自動的に決まります（変更は NotePM 側で行ってください）。
            {data && (
              <>
                {" "}現在、<span className="text-foreground font-medium">{data.total_notes}</span>{" "}
                個のノートブック・
                <span className="text-foreground font-medium">{data.total_docs}</span>{" "}
                件のドキュメントを閲覧できます。
              </>
            )}
          </p>

          <div className="relative mb-3 max-w-xs">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" aria-hidden />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="ノートブック名で検索"
              className="w-full h-8 pl-8 pr-3 rounded-lg border border-sidebar-border bg-transparent text-[13px] text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition"
            />
          </div>

          <div className="rounded-xl border border-sidebar-border overflow-hidden">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="bg-sidebar-accent/40 text-muted-foreground text-[11px] uppercase tracking-wide">
                  <th className="text-left font-medium px-4 py-2.5 whitespace-nowrap">ノートブック</th>
                  <th className="text-left font-medium px-4 py-2.5 w-28 whitespace-nowrap">公開範囲</th>
                  <th className="text-left font-medium px-4 py-2.5 w-28 whitespace-nowrap">ドキュメント</th>
                </tr>
              </thead>
              <tbody>
                {data === null && (
                  <tr><td colSpan={3} className="px-4 py-8 text-center text-muted-foreground text-[12px]">読み込み中...</td></tr>
                )}
                {data !== null && filtered.length === 0 && (
                  <tr><td colSpan={3} className="px-4 py-8 text-center text-muted-foreground text-[12px]">閲覧できるノートブックがありません</td></tr>
                )}
                {filtered.map((n) => (
                  <tr key={n.name} className="border-t border-sidebar-border">
                    <td className="px-4 py-2.5 text-foreground">
                      <span className="truncate max-w-[420px] inline-block align-middle" title={n.name}>{n.name}</span>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground text-[12px] whitespace-nowrap">
                      {n.is_public ? (
                        <span className="inline-flex items-center gap-1.5"><Globe className="w-3.5 h-3.5" aria-hidden />全体公開</span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5"><Lock className="w-3.5 h-3.5 text-amber-500" aria-hidden />限定</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground text-[12px] whitespace-nowrap">{n.n_docs}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

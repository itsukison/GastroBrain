"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ArrowLeft,
  Check,
  ChevronDown,
  FolderLock,
  Lock,
  Search,
  ShieldCheck,
  Users,
} from "lucide-react";

// ── types ─────────────────────────────────────────────────────────────────
export type Role = { id: number; name: string; level: number };

export type Member = {
  email: string;
  role_id: number | null;
  role_name: string | null;
  level: number | null;
  is_admin: boolean;
  last_sign_in_at: string | null;
};

export type FolderRule = { id: string; folder_prefix: string[]; min_level: number; note: string | null };
export type Folder = { folder_path: string[]; n_docs: number; effective_min_level: number };

type Tab = "members" | "folders";

const TABS: { id: Tab; label: string; icon: typeof Users }[] = [
  { id: "members", label: "メンバー", icon: Users },
  { id: "folders", label: "フォルダアクセス", icon: FolderLock },
];

// ── generic level/role dropdown ─────────────────────────────────────────────
function Dropdown({
  value,
  options,
  onChange,
  disabled,
  placeholder,
  className,
}: {
  value: number | null;
  options: { value: number | null; label: string }[];
  onChange: (v: number | null) => void;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
}) {
  const current = options.find((o) => o.value === value);
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild disabled={disabled}>
        <button
          type="button"
          className={`h-8 px-2.5 rounded-lg border border-sidebar-border bg-transparent text-[13px] text-foreground flex items-center justify-between gap-2 hover:bg-sidebar-accent focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition disabled:opacity-50 data-[state=open]:bg-sidebar-accent ${className ?? "w-48"}`}
        >
          <span className={`flex-1 min-w-0 truncate text-left ${current ? "" : "text-muted-foreground"}`}>
            {current?.label ?? placeholder ?? "—"}
          </span>
          <ChevronDown className="w-3.5 h-3.5 text-muted-foreground shrink-0" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={4}
          className="z-[60] min-w-[var(--radix-dropdown-menu-trigger-width)] rounded-lg border border-sidebar-border shadow-lg p-1 text-[13px]"
          style={{ backgroundColor: "var(--background)", animation: "slideUp 120ms cubic-bezier(0.16,1,0.3,1)" }}
        >
          {options.map((opt) => {
            const selected = opt.value === value;
            return (
              <DropdownMenu.Item
                key={String(opt.value)}
                onSelect={() => onChange(opt.value)}
                className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-foreground hover:bg-sidebar-accent focus:bg-sidebar-accent focus:outline-none cursor-pointer transition"
              >
                <Check className={`w-3.5 h-3.5 shrink-0 ${selected ? "opacity-100" : "opacity-0"}`} aria-hidden />
                <span className="flex-1 whitespace-nowrap">{opt.label}</span>
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

// ── members tab ─────────────────────────────────────────────────────────────
function MembersTab({ roles, initial }: { roles: Role[]; initial: Member[] | null }) {
  const [members, setMembers] = useState<Member[] | null>(initial);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/org/members");
      if (!r.ok) throw new Error();
      const d = (await r.json()) as { members: Member[] };
      setMembers(d.members);
    } catch {
      setMembers([]);
    }
  }, []);
  // Only fetch client-side if the server didn't prefetch (resilience fallback).
  useEffect(() => {
    if (initial === null) void load();
  }, [initial, load]);

  const roleOptions = useMemo(
    () => [
      { value: null as number | null, label: "未割り当て" },
      ...roles.map((r) => ({ value: r.id, label: `${r.name}（Lv.${r.level}）` })),
    ],
    [roles],
  );

  async function patch(email: string, body: { role_id?: number | null; is_admin?: boolean }) {
    setBusy(email);
    setError(null);
    const before = members;
    setMembers((prev) =>
      (prev ?? []).map((m) => {
        if (m.email !== email) return m;
        const next = { ...m };
        if ("role_id" in body) {
          next.role_id = body.role_id ?? null;
          const role = roles.find((r) => r.id === body.role_id);
          next.role_name = role?.name ?? null;
          next.level = role?.level ?? null;
        }
        if ("is_admin" in body) next.is_admin = body.is_admin!;
        return next;
      }),
    );
    try {
      const r = await fetch(`/api/org/members/${encodeURIComponent(email)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const msg = r.status === 400 ? "最後の管理者は解除できません" : "更新に失敗しました";
        throw new Error(msg);
      }
    } catch (e) {
      setMembers(before);
      setError(e instanceof Error ? e.message : "更新に失敗しました");
    } finally {
      setBusy(null);
    }
  }

  const filtered = (members ?? []).filter((m) => m.email.toLowerCase().includes(q.toLowerCase()));

  return (
    <div>
      <p className="text-[12px] text-muted-foreground leading-relaxed mb-4">
        役割（クリアランスレベル）を割り当てます。上位レベルは下位レベルの閲覧範囲をすべて含みます。ここでの変更は Web・Slack・MCP のすべての経路に即時反映されます。
      </p>

      <div className="relative mb-3 max-w-xs">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" aria-hidden />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="メールアドレスで検索"
          className="w-full h-8 pl-8 pr-3 rounded-lg border border-sidebar-border bg-transparent text-[13px] text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition"
        />
      </div>

      {error && <div className="mb-3 text-[12px] text-red-500">{error}</div>}

      <div className="rounded-xl border border-sidebar-border overflow-hidden">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="bg-sidebar-accent/40 text-muted-foreground text-[11px] uppercase tracking-wide">
              <th className="text-left font-medium px-4 py-2.5 whitespace-nowrap">メンバー</th>
              <th className="text-left font-medium px-4 py-2.5 w-52 whitespace-nowrap">役割</th>
              <th className="text-left font-medium px-4 py-2.5 w-24 whitespace-nowrap">管理者</th>
              <th className="text-left font-medium px-4 py-2.5 w-32 whitespace-nowrap">最終ログイン</th>
            </tr>
          </thead>
          <tbody>
            {members === null && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-muted-foreground text-[12px]">読み込み中...</td></tr>
            )}
            {members !== null && filtered.length === 0 && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-muted-foreground text-[12px]">該当するメンバーがいません</td></tr>
            )}
            {filtered.map((m) => (
              <tr key={m.email} className="border-t border-sidebar-border">
                <td className="px-4 py-2.5 text-foreground truncate max-w-[260px]" title={m.email}>{m.email}</td>
                <td className="px-4 py-2">
                  <Dropdown
                    value={m.role_id}
                    options={roleOptions}
                    disabled={busy === m.email}
                    onChange={(v) => patch(m.email, { role_id: v })}
                  />
                </td>
                <td className="px-4 py-2">
                  <button
                    type="button"
                    role="switch"
                    aria-checked={m.is_admin}
                    disabled={busy === m.email}
                    onClick={() => patch(m.email, { is_admin: !m.is_admin })}
                    className={`relative h-5 w-9 rounded-full transition disabled:opacity-50 ${m.is_admin ? "bg-foreground" : "bg-sidebar-border"}`}
                    aria-label="管理者権限"
                  >
                    <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-background transition-all ${m.is_admin ? "left-[18px]" : "left-0.5"}`} />
                  </button>
                </td>
                <td className="px-4 py-2.5 text-muted-foreground text-[12px] whitespace-nowrap">
                  {m.last_sign_in_at ? new Date(m.last_sign_in_at).toLocaleDateString("ja-JP") : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── folders tab ───────────────────────────────────────────────────────────
const keyOf = (p: string[]) => JSON.stringify(p);

function FoldersTab({
  roles,
  initialFolders,
  initialRules,
}: {
  roles: Role[];
  initialFolders: Folder[] | null;
  initialRules: FolderRule[];
}) {
  const [folders, setFolders] = useState<Folder[] | null>(initialFolders);
  const [rules, setRules] = useState<FolderRule[]>(initialRules);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/org/folders");
      if (!r.ok) throw new Error();
      const d = (await r.json()) as { folders: Folder[]; rules: FolderRule[] };
      setFolders(d.folders);
      setRules(d.rules);
    } catch {
      setFolders([]);
    }
  }, []);
  useEffect(() => {
    if (initialFolders === null) void load();
  }, [initialFolders, load]);

  const ruleByKey = useMemo(() => {
    const m = new Map<string, FolderRule>();
    for (const r of rules) m.set(keyOf(r.folder_prefix), r);
    return m;
  }, [rules]);

  const levelOptions = useMemo(
    () => [
      { value: 0 as number | null, label: "制限なし（全員）" },
      ...roles.map((r) => ({ value: r.level, label: `${r.name}（Lv.${r.level}）以上` })),
    ],
    [roles],
  );

  async function setLevel(folder: Folder, level: number) {
    const k = keyOf(folder.folder_path);
    setBusy(k);
    setError(null);
    try {
      if (level === 0) {
        const rule = ruleByKey.get(k);
        if (rule) {
          const r = await fetch(`/api/org/folder-acl/${rule.id}`, { method: "DELETE" });
          if (!r.ok && r.status !== 204) throw new Error();
        }
      } else {
        const r = await fetch("/api/org/folder-acl", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folder_prefix: folder.folder_path, min_level: level }),
        });
        if (!r.ok) throw new Error();
      }
      await load();
    } catch {
      setError("アクセス設定の更新に失敗しました");
    } finally {
      setBusy(null);
    }
  }

  const filtered = (folders ?? []).filter((f) =>
    f.folder_path.join(" / ").toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <div>
      <p className="text-[12px] text-muted-foreground leading-relaxed mb-4">
        フォルダごとに閲覧に必要なレベルを設定します。「制限なし」のフォルダは全員が閲覧できます。サブフォルダはより厳しい上位の設定を継承します。
      </p>

      <div className="relative mb-3 max-w-xs">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" aria-hidden />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="フォルダ名で検索"
          className="w-full h-8 pl-8 pr-3 rounded-lg border border-sidebar-border bg-transparent text-[13px] text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition"
        />
      </div>

      {error && <div className="mb-3 text-[12px] text-red-500">{error}</div>}

      <div className="rounded-xl border border-sidebar-border overflow-hidden">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="bg-sidebar-accent/40 text-muted-foreground text-[11px] uppercase tracking-wide">
              <th className="text-left font-medium px-4 py-2.5 whitespace-nowrap">フォルダ</th>
              <th className="text-left font-medium px-4 py-2.5 w-28 whitespace-nowrap">ドキュメント</th>
              <th className="text-left font-medium px-4 py-2.5 w-56 whitespace-nowrap">必要レベル</th>
            </tr>
          </thead>
          <tbody>
            {folders === null && (
              <tr><td colSpan={3} className="px-4 py-8 text-center text-muted-foreground text-[12px]">読み込み中...</td></tr>
            )}
            {folders !== null && filtered.length === 0 && (
              <tr><td colSpan={3} className="px-4 py-8 text-center text-muted-foreground text-[12px]">該当するフォルダがありません</td></tr>
            )}
            {filtered.map((f) => {
              const k = keyOf(f.folder_path);
              const restricted = f.effective_min_level > 0;
              return (
                <tr key={k} className="border-t border-sidebar-border">
                  <td className="px-4 py-2.5 text-foreground">
                    <span className="inline-flex items-center gap-1.5">
                      {restricted && <Lock className="w-3.5 h-3.5 text-amber-500 shrink-0" aria-hidden />}
                      <span className="truncate max-w-[360px]" title={f.folder_path.join(" / ")}>
                        {f.folder_path.join(" / ")}
                      </span>
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-muted-foreground text-[12px] whitespace-nowrap">{f.n_docs}</td>
                  <td className="px-4 py-2">
                    <Dropdown
                      value={f.effective_min_level}
                      options={levelOptions}
                      disabled={busy === k}
                      className="w-56"
                      onChange={(v) => setLevel(f, v ?? 0)}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── shell ───────────────────────────────────────────────────────────────────
export function OrgView({
  adminEmail,
  initialRoles,
  initialMembers,
  initialFolders,
  initialRules,
}: {
  adminEmail: string;
  initialRoles: Role[];
  initialMembers: Member[] | null;
  initialFolders: Folder[] | null;
  initialRules: FolderRule[];
}) {
  const [tab, setTab] = useState<Tab>("members");
  const [roles, setRoles] = useState<Role[]>(initialRoles);

  // Fallback only if the server prefetch came back empty (e.g. transient error).
  useEffect(() => {
    if (initialRoles.length > 0) return;
    fetch("/api/org/roles")
      .then((r) => (r.ok ? r.json() : null))
      .then((d: { roles: Role[] } | null) => d && setRoles(d.roles))
      .catch(() => {});
  }, [initialRoles]);

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
            <ShieldCheck className="w-[18px] h-[18px]" aria-hidden />
            組織管理
          </h1>
        </div>
        <p className="ml-11 text-[12px] text-muted-foreground mb-6">{adminEmail}</p>

        <div className="ml-11 flex items-center gap-1 border-b border-sidebar-border mb-6">
          {TABS.map(({ id, label, icon: Icon }) => {
            const active = tab === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => setTab(id)}
                className={`flex items-center gap-1.5 px-3 h-9 text-[13px] -mb-px border-b-2 transition ${
                  active
                    ? "border-foreground text-foreground font-medium"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                <Icon className="w-3.5 h-3.5" aria-hidden />
                {label}
              </button>
            );
          })}
        </div>

        {/* Both tabs stay mounted (just hidden) so switching never refetches. */}
        <div className="ml-11">
          <div className={tab === "members" ? "" : "hidden"}>
            <MembersTab roles={roles} initial={initialMembers} />
          </div>
          <div className={tab === "folders" ? "" : "hidden"}>
            <FoldersTab roles={roles} initialFolders={initialFolders} initialRules={initialRules} />
          </div>
        </div>
      </div>

      <style>{`@keyframes slideUp { from { opacity: 0; transform: translateY(6px) scale(0.98) } to { opacity: 1; transform: none } }`}</style>
    </div>
  );
}

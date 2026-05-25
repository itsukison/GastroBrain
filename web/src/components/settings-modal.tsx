"use client";

import { useEffect, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Copy, User, Plug } from "lucide-react";
import type { Department, UserPreferences } from "@/types";

const MCP_INSTALL_CMD = `claude mcp add --transport http --scope user gastrobrain \\
  https://gastrobrain-rjp7bbdhta-an.a.run.app/mcp/ \\
  --header "Authorization: Bearer <YOUR_TOKEN>"`;

const DEPARTMENT_OPTIONS: { value: Department | ""; label: string }[] = [
  { value: "", label: "未設定" },
  { value: "consulting", label: "コンサルティング部" },
  { value: "sales", label: "営業部" },
  { value: "content", label: "コンテンツ制作部" },
  { value: "dev", label: "システム開発部" },
  { value: "backoffice", label: "バックオフィス" },
  { value: "other", label: "その他" },
];

type Tab = "profile" | "mcp";

const TABS: { id: Tab; label: string; icon: typeof User }[] = [
  { id: "profile", label: "プロフィール", icon: User },
  { id: "mcp", label: "MCP連携", icon: Plug },
];

function DepartmentDropdown({
  value,
  onChange,
  disabled,
}: {
  value: Department | "";
  onChange: (v: Department | "") => void;
  disabled?: boolean;
}) {
  const current = DEPARTMENT_OPTIONS.find((o) => o.value === value) ?? DEPARTMENT_OPTIONS[0];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild disabled={disabled}>
        <button
          type="button"
          className="w-full h-9 px-3 rounded-lg border border-sidebar-border bg-transparent text-sm text-foreground flex items-center justify-between hover:bg-sidebar-accent focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition disabled:opacity-50 data-[state=open]:bg-sidebar-accent"
        >
          <span className={value ? "" : "text-muted-foreground"}>{current.label}</span>
          <ChevronDown className="w-4 h-4 text-muted-foreground" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={4}
          className="z-[60] min-w-[var(--radix-dropdown-menu-trigger-width)] rounded-lg border border-sidebar-border shadow-lg p-1 text-sm"
          style={{
            backgroundColor: "var(--background)",
            animation: "slideUp 120ms cubic-bezier(0.16,1,0.3,1)",
          }}
        >
          {DEPARTMENT_OPTIONS.map((opt) => {
            const selected = opt.value === value;
            return (
              <DropdownMenu.Item
                key={opt.value}
                onSelect={() => onChange(opt.value)}
                className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-foreground hover:bg-sidebar-accent focus:bg-sidebar-accent focus:outline-none cursor-pointer transition"
              >
                <Check
                  className={`w-3.5 h-3.5 shrink-0 ${selected ? "opacity-100" : "opacity-0"}`}
                  aria-hidden
                />
                <span className="flex-1">{opt.label}</span>
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

export function SettingsModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("profile");
  const [department, setDepartment] = useState<Department | "">("");
  const [extraNote, setExtraNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mcpCopied, setMcpCopied] = useState(false);

  const NOTE_MAX = 300;
  const noteLen = extraNote.length;
  const noteOver = noteLen > NOTE_MAX;

  async function copyMcpCmd() {
    try {
      await navigator.clipboard.writeText(MCP_INSTALL_CMD);
      setMcpCopied(true);
      setTimeout(() => setMcpCopied(false), 1500);
    } catch {
      // clipboard API may be unavailable on insecure contexts; silently no-op
    }
  }

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  // Hydrate from server in the background. A failure here is not blocking —
  // user may simply have no row yet, or the API may be momentarily unreachable.
  // The form stays interactive either way; only the PUT failure is surfaced.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    setTab("profile");
    setDepartment("");
    setExtraNote("");
    fetch("/api/preferences")
      .then((r) => (r.ok ? r.json() : null))
      .then((p: UserPreferences | null) => {
        if (cancelled || !p) return;
        setDepartment(p.department ?? "");
        setExtraNote(p.extra_note ?? "");
      })
      .catch(() => {
        // swallow — empty form is the right default
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  async function save() {
    if (noteOver) {
      setError(`追加メモは${NOTE_MAX}字以内で入力してください`);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const resp = await fetch("/api/preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          department: department || null,
          extra_note: extraNote.trim() || null,
        }),
      });
      if (!resp.ok) throw new Error(`${resp.status}`);
      onClose();
    } catch {
      setError("保存に失敗しました");
    } finally {
      setSaving(false);
    }
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ animation: "fadeIn 120ms ease" }}
    >
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-[2px]"
        onClick={onClose}
      />

      <div
        className="relative z-10 w-[640px] max-w-[calc(100vw-2rem)] rounded-2xl border border-sidebar-border shadow-xl overflow-hidden flex flex-col"
        style={{
          backgroundColor: "var(--background)",
          animation: "slideUp 160ms cubic-bezier(0.16,1,0.3,1)",
        }}
      >
        <div className="flex">
          <aside className="w-[168px] shrink-0 border-r border-sidebar-border bg-sidebar-accent/20 px-3 py-5">
            <h2 className="px-2 text-[15px] font-semibold text-foreground leading-snug mb-3">
              設定
            </h2>
            <nav className="flex flex-col gap-0.5">
              {TABS.map(({ id, label, icon: Icon }) => {
                const active = tab === id;
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setTab(id)}
                    className={`flex items-center gap-2 h-8 px-2 rounded-md text-[13px] text-left transition ${
                      active
                        ? "bg-sidebar-accent text-foreground font-medium"
                        : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground"
                    }`}
                  >
                    <Icon className="w-3.5 h-3.5 shrink-0" aria-hidden />
                    <span>{label}</span>
                  </button>
                );
              })}
            </nav>
          </aside>

          <main className="flex-1 p-6 min-h-[360px]">
            {tab === "profile" ? (
              <section>
                <h3 className="text-[14px] font-semibold text-foreground leading-snug">
                  プロフィール
                </h3>
                <p className="mt-1 text-[12px] text-muted-foreground leading-relaxed">
                  所属部署を設定すると、Gastrobrainの回答が部署特性を踏まえた表現になります。
                </p>

                <div className="mt-5">
                  <div className="block text-[13px] font-medium text-foreground mb-1.5">
                    所属部署
                  </div>
                  <DepartmentDropdown
                    value={department}
                    onChange={setDepartment}
                    disabled={saving}
                  />
                </div>

                <div className="mt-4">
                  <div className="flex items-baseline justify-between mb-1.5">
                    <label
                      htmlFor="settings-extra-note"
                      className="block text-[13px] font-medium text-foreground"
                    >
                      追加メモ
                      <span className="ml-1.5 text-[11px] font-normal text-muted-foreground">
                        （任意）
                      </span>
                    </label>
                    <span
                      className={`text-[11px] ${noteOver ? "text-red-500" : "text-muted-foreground"}`}
                    >
                      {noteLen} / {NOTE_MAX}
                    </span>
                  </div>
                  <textarea
                    id="settings-extra-note"
                    value={extraNote}
                    onChange={(e) => setExtraNote(e.target.value)}
                    disabled={saving}
                    rows={4}
                    placeholder="例：楽天とAmazonの広告運用が中心。ASIN/SKUなどの商品コードは原文のままで出してほしい。"
                    className="w-full px-3 py-2 rounded-lg border border-sidebar-border bg-transparent text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition disabled:opacity-50 resize-none"
                  />
                  <p className="mt-1.5 text-[11px] text-muted-foreground leading-relaxed">
                    業務でよく扱う領域・用語・回答してほしいスタイルなどを書くと、回答のトーンや言葉選びに反映されます。
                  </p>
                </div>

                <div className="mt-5 rounded-lg bg-sidebar-accent/40 p-3 text-[11px] text-muted-foreground leading-relaxed">
                  以下の挙動は設定に関わらず常に維持されます：
                  <ul className="mt-1 ml-3 list-disc">
                    <li>出典（NotePMリンク）の付与</li>
                    <li>関連情報がない場合の「わかりません」回答</li>
                    <li>日本語での回答（質問が英語の場合のみ英語）</li>
                  </ul>
                </div>
              </section>
            ) : (
              <section>
                <h3 className="text-[14px] font-semibold text-foreground leading-snug">
                  MCP連携
                </h3>
                <p className="mt-1 text-[12px] text-muted-foreground leading-relaxed">
                  Claude Code・Cursor などの MCP 対応エージェントから Gastrobrain を直接検索できるようにします。トークンは管理者に DM で依頼してください。
                </p>

                <div className="mt-5">
                  <div className="block text-[13px] font-medium text-foreground mb-1.5">
                    Claude Code に追加
                  </div>
                  <div className="relative rounded-lg border border-sidebar-border bg-sidebar-accent/30 p-3 pr-11 font-mono text-[11px] leading-relaxed text-foreground whitespace-pre overflow-x-auto">
                    {MCP_INSTALL_CMD}
                    <button
                      type="button"
                      onClick={copyMcpCmd}
                      aria-label={mcpCopied ? "Copied" : "Copy install command"}
                      className="absolute top-2 right-2 h-7 w-7 rounded-md border border-sidebar-border bg-background flex items-center justify-center hover:bg-sidebar-accent transition"
                    >
                      {mcpCopied ? (
                        <Check className="w-3.5 h-3.5" aria-hidden />
                      ) : (
                        <Copy className="w-3.5 h-3.5 text-muted-foreground" aria-hidden />
                      )}
                    </button>
                  </div>
                  <p className="mt-1.5 text-[11px] text-muted-foreground leading-relaxed">
                    <code>&lt;YOUR_TOKEN&gt;</code> を発行されたトークンに置き換えて、ターミナルで実行してください。
                  </p>
                </div>

                <div className="mt-5 rounded-lg bg-sidebar-accent/40 p-3 text-[11px] text-muted-foreground leading-relaxed">
                  Cursor / Claude Desktop / claude.ai の設定方法は、社内ドキュメント
                  <code className="mx-1">docs/MCP.md</code>
                  を参照してください。
                </div>
              </section>
            )}
          </main>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-sidebar-border px-6 py-3 bg-sidebar-accent/20">
          <div className="text-[12px] text-red-500 min-h-[1em]">
            {error}
          </div>
          <div className="flex gap-2.5">
            <button
              onClick={onClose}
              disabled={saving}
              className="h-9 px-4 rounded-lg text-sm font-medium border border-sidebar-border bg-transparent text-foreground hover:bg-sidebar-accent transition disabled:opacity-50"
            >
              閉じる
            </button>
            {tab === "profile" && (
              <button
                onClick={save}
                disabled={saving}
                className="h-9 px-4 rounded-lg text-sm font-medium bg-foreground text-background hover:opacity-90 transition disabled:opacity-50"
              >
                {saving ? "保存中..." : "保存"}
              </button>
            )}
          </div>
        </div>
      </div>

      <style>{`
        @keyframes fadeIn  { from { opacity: 0 } to { opacity: 1 } }
        @keyframes slideUp { from { opacity: 0; transform: translateY(6px) scale(0.98) } to { opacity: 1; transform: none } }
      `}</style>
    </div>
  );
}

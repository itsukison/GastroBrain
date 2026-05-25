"use client";

import { useCallback, useEffect, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Copy,
  Plug,
  Trash2,
  User,
} from "lucide-react";
import type { Department, UserPreferences } from "@/types";

const MCP_URL = "https://gastrobrain-rjp7bbdhta-an.a.run.app/mcp/";
const TOKEN_PLACEHOLDER = "<YOUR_TOKEN>";

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

type Installer = "cc" | "claude_ai" | "desktop";

const INSTALLERS: { id: Installer; label: string }[] = [
  { id: "cc", label: "Claude Code" },
  { id: "claude_ai", label: "claude.ai" },
  { id: "desktop", label: "Claude Desktop" },
];

type TokenSummary = {
  id: string;
  label: string;
  created_at: string;
  last_used_at: string | null;
};

function ccCommand(token: string): string {
  return `claude mcp add --transport http --scope user gastrobrain \\
  ${MCP_URL} \\
  --header "Authorization: Bearer ${token}"`;
}

function desktopJson(token: string): string {
  return `{
  "mcpServers": {
    "gastrobrain": {
      "type": "streamable-http",
      "url": "${MCP_URL}",
      "headers": {
        "Authorization": "Bearer ${token}"
      }
    }
  }
}`;
}

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

function CopyBlock({ value, ariaLabel }: { value: string; ariaLabel: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore
    }
  }
  return (
    <div className="relative rounded-lg border border-sidebar-border bg-sidebar-accent/30 p-3 pr-11 font-mono text-[11px] leading-relaxed text-foreground whitespace-pre overflow-x-auto">
      {value}
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : ariaLabel}
        className="absolute top-2 right-2 h-7 w-7 rounded-md border border-sidebar-border bg-background flex items-center justify-center hover:bg-sidebar-accent transition"
      >
        {copied ? (
          <Check className="w-3.5 h-3.5" aria-hidden />
        ) : (
          <Copy className="w-3.5 h-3.5 text-muted-foreground" aria-hidden />
        )}
      </button>
    </div>
  );
}

function McpSection() {
  const [tokens, setTokens] = useState<TokenSummary[] | null>(null);
  const [newToken, setNewToken] = useState<string | null>(null);
  const [minting, setMinting] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [installer, setInstaller] = useState<Installer>("cc");

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const r = await fetch("/api/mcp/tokens");
      if (!r.ok) throw new Error(`${r.status}`);
      const data = (await r.json()) as { tokens: TokenSummary[] };
      setTokens(data.tokens);
    } catch {
      setError("トークン一覧の取得に失敗しました");
      setTokens([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function mint() {
    setMinting(true);
    setError(null);
    try {
      const r = await fetch("/api/mcp/tokens", { method: "POST" });
      if (!r.ok) throw new Error(`${r.status}`);
      const data = (await r.json()) as TokenSummary & { token: string };
      setNewToken(data.token);
      setTokens((prev) => [
        {
          id: data.id,
          label: data.label,
          created_at: data.created_at,
          last_used_at: data.last_used_at,
        },
        ...(prev ?? []),
      ]);
    } catch {
      setError("トークンの発行に失敗しました");
    } finally {
      setMinting(false);
    }
  }

  async function revoke(id: string) {
    setRevoking(id);
    setError(null);
    try {
      const r = await fetch(`/api/mcp/tokens/${id}`, { method: "DELETE" });
      if (!r.ok && r.status !== 204) throw new Error(`${r.status}`);
      setTokens((prev) => (prev ?? []).filter((t) => t.id !== id));
    } catch {
      setError("失効に失敗しました");
    } finally {
      setRevoking(null);
    }
  }

  const tokenForDisplay = newToken ?? TOKEN_PLACEHOLDER;

  return (
    <section>
      <h3 className="text-[14px] font-semibold text-foreground leading-snug">
        MCP連携
      </h3>
      <p className="mt-1 text-[12px] text-muted-foreground leading-relaxed">
        Claude Code・Cursor・claude.ai などから Gastrobrain を直接検索できます。
      </p>

      {newToken && (
        <div className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
          <div className="flex items-start gap-2 mb-2">
            <AlertTriangle className="w-3.5 h-3.5 mt-0.5 text-amber-600 dark:text-amber-400 shrink-0" aria-hidden />
            <div className="text-[12px] text-foreground leading-relaxed">
              新しいトークンを発行しました。
              <span className="font-medium">この画面を閉じると再表示できません。</span>
              必ず控えてください。
            </div>
          </div>
          <CopyBlock value={newToken} ariaLabel="Copy token" />
        </div>
      )}

      <div className="mt-4 flex items-center justify-between gap-3">
        <div className="text-[12px] text-muted-foreground">
          {tokens === null
            ? "読み込み中..."
            : tokens.length === 0
              ? "発行済みトークンはありません"
              : `発行済み ${tokens.length} 件`}
        </div>
        <button
          type="button"
          onClick={mint}
          disabled={minting}
          className="h-8 px-3 rounded-md text-[12px] font-medium bg-foreground text-background hover:opacity-90 transition disabled:opacity-50"
        >
          {minting ? "発行中..." : "新しいトークンを発行"}
        </button>
      </div>

      {tokens && tokens.length > 0 && (
        <ul className="mt-2 space-y-1">
          {tokens.map((t) => (
            <li
              key={t.id}
              className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-md border border-sidebar-border bg-sidebar-accent/20 text-[12px]"
            >
              <div className="flex flex-col min-w-0">
                <span className="font-mono text-foreground truncate">{t.label}</span>
                <span className="text-[10px] text-muted-foreground">
                  作成: {new Date(t.created_at).toLocaleDateString("ja-JP")}
                  {t.last_used_at && (
                    <> ・ 最終利用: {new Date(t.last_used_at).toLocaleDateString("ja-JP")}</>
                  )}
                </span>
              </div>
              <button
                type="button"
                onClick={() => revoke(t.id)}
                disabled={revoking === t.id}
                aria-label="Revoke token"
                className="h-7 w-7 rounded-md text-muted-foreground hover:text-red-500 hover:bg-red-500/10 flex items-center justify-center transition disabled:opacity-50"
              >
                <Trash2 className="w-3.5 h-3.5" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-5">
        <div className="flex items-center gap-1 border-b border-sidebar-border mb-3">
          {INSTALLERS.map(({ id, label }) => {
            const active = installer === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => setInstaller(id)}
                className={`px-3 h-8 text-[12px] -mb-px border-b-2 transition ${
                  active
                    ? "border-foreground text-foreground font-medium"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>

        {installer === "cc" && (
          <div>
            <p className="text-[11px] text-muted-foreground leading-relaxed mb-2">
              ターミナルで以下のコマンドを実行してください。
            </p>
            <CopyBlock value={ccCommand(tokenForDisplay)} ariaLabel="Copy CLI command" />
          </div>
        )}

        {installer === "claude_ai" && (
          <div className="space-y-2.5">
            <p className="text-[11px] text-muted-foreground leading-relaxed">
              claude.ai の <span className="font-medium text-foreground">Settings → Connectors → Add custom connector</span> から以下を貼り付けてください。
            </p>
            <div>
              <div className="text-[11px] text-muted-foreground mb-1">URL</div>
              <CopyBlock value={MCP_URL} ariaLabel="Copy URL" />
            </div>
            <div>
              <div className="text-[11px] text-muted-foreground mb-1">Bearer Token</div>
              <CopyBlock value={tokenForDisplay} ariaLabel="Copy token" />
            </div>
          </div>
        )}

        {installer === "desktop" && (
          <div>
            <p className="text-[11px] text-muted-foreground leading-relaxed mb-2">
              <code>~/Library/Application Support/Claude/claude_desktop_config.json</code>
              （macOS）に以下を追記して Claude Desktop を再起動してください。
            </p>
            <CopyBlock value={desktopJson(tokenForDisplay)} ariaLabel="Copy config" />
          </div>
        )}

        {!newToken && (
          <p className="mt-2 text-[11px] text-muted-foreground leading-relaxed">
            <code>{TOKEN_PLACEHOLDER}</code> は実際のトークンに置き換えてください。トークンをお持ちでない場合は上の「新しいトークンを発行」を押してください。
          </p>
        )}
      </div>

      {error && (
        <div className="mt-3 text-[12px] text-red-500">{error}</div>
      )}
    </section>
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

  const NOTE_MAX = 300;
  const noteLen = extraNote.length;
  const noteOver = noteLen > NOTE_MAX;

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

          <main className="flex-1 p-6 min-h-[420px] max-h-[75vh] overflow-y-auto">
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
              <McpSection />
            )}
          </main>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-sidebar-border px-6 py-3 bg-sidebar-accent/20">
          <div className="text-[12px] text-red-500 min-h-[1em]">
            {tab === "profile" ? error : null}
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

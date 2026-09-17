"use client";

import { useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { Loader2, UserPlus } from "lucide-react";

/**
 * "Share this meeting" — a popover anchored to its own trigger.
 *
 * This used to be an inline form that replaced the 共有 button inside the
 * participant chip row. Because the form is ~250px wide and 8px taller than the
 * chips beside it, opening it re-flowed the `flex-wrap` row: the row grew, chips
 * could jump to a new line, and the tabs and everything below shifted down. The
 * error message rendered in the same row, so failing shifted it again.
 *
 * A portalled popover cannot move the page at all. The only layout change left
 * is the new chip appearing on success, which is the one the user asked for.
 */
export function MeetingShare({
  meetingId,
  onShared,
}: {
  meetingId: string;
  onShared: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function share(e: React.FormEvent) {
    e.preventDefault();
    const address = email.trim();
    if (!address || busy) return;

    setError(null);
    setBusy(true);
    try {
      const resp = await fetch(`/api/meetings/${meetingId}/share`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: address }),
      });
      if (!resp.ok) {
        const body = (await resp.json().catch(() => null)) as { detail?: string } | null;
        setError(body?.detail ?? "共有できませんでした");
        return;
      }
      setEmail("");
      setOpen(false);
      onShared();
    } catch {
      setError("共有できませんでした");
    } finally {
      setBusy(false);
    }
  }

  // Closing discards a half-typed address rather than leaving it to reappear
  // later with no visible reason.
  function change(next: boolean) {
    setOpen(next);
    if (!next) {
      setEmail("");
      setError(null);
    }
  }

  return (
    <Popover.Root open={open} onOpenChange={change}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-1.5 h-8 px-2.5 shrink-0 rounded-lg text-[12px] text-muted-foreground hover:bg-sidebar-accent hover:text-foreground data-[state=open]:bg-sidebar-accent data-[state=open]:text-foreground transition"
        >
          <UserPlus className="w-3.5 h-3.5" aria-hidden />
          共有
        </button>
      </Popover.Trigger>

      <Popover.Portal>
        <Popover.Content
          side="bottom"
          align="end"
          sideOffset={6}
          className="w-72 rounded-xl bg-popover text-popover-foreground border border-border shadow-xl p-4 z-50"
        >
          <p className="text-[13px] font-medium mb-1">この会議を共有</p>
          <p className="text-[11px] text-muted-foreground mb-3">
            追加した人は文字起こしとサマリーを閲覧できます。
          </p>

          <form onSubmit={share} className="flex items-center gap-1.5">
            <input
              autoFocus
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@gastroduce-japan.co.jp"
              aria-label="共有するメールアドレス"
              className="flex-1 min-w-0 h-8 px-2.5 rounded-lg border border-sidebar-border bg-transparent text-[12px] text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-2 focus:ring-sidebar-accent transition"
            />
            <button
              type="submit"
              disabled={!email.trim() || busy}
              className="h-8 px-3 shrink-0 rounded-lg bg-primary text-primary-foreground text-[12px] font-medium disabled:opacity-40 transition"
            >
              {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden /> : "追加"}
            </button>
          </form>

          {error && <p className="mt-2 text-[11px] text-destructive">{error}</p>}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

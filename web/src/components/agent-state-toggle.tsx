"use client";

import { useState } from "react";
import { Ear, Moon } from "lucide-react";

import { cn } from "@/lib/cn";
import type { AgentState } from "@/types";

const OPTIONS: { value: AgentState; label: string; icon: typeof Moon; hint: string }[] = [
  { value: "asleep", label: "待機", icon: Moon, hint: "聞いているが発言しない" },
  { value: "open", label: "応答可", icon: Ear, hint: "名前を呼ばなくても答える" },
];

/**
 * The only in-product control over the AI during a meeting, besides Meet chat
 * (docs/MEETINGS_WEB.md §6.4).
 *
 * Writing here just stores the value: the VPS polls it every ~3 s, so a press
 * takes a moment to take effect, and the 90-second expiry back to 待機 is timed
 * on the meeting side. Deliberately no timer here — two timers on two machines
 * disagree.
 */
export function AgentStateToggle({
  meetingId,
  state,
  onChange,
}: {
  meetingId: string;
  state: AgentState;
  onChange: (next: AgentState) => void;
}) {
  const [pending, setPending] = useState(false);

  async function set(next: AgentState) {
    if (next === state || pending) return;
    setPending(true);
    const previous = state;
    onChange(next); // optimistic: the button must feel immediate mid-meeting
    try {
      const resp = await fetch(`/api/meetings/${meetingId}/state`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_state: next }),
      });
      if (!resp.ok) throw new Error(String(resp.status));
      const data = (await resp.json()) as { agent_state: AgentState };
      onChange(data.agent_state);
    } catch {
      onChange(previous);
    } finally {
      setPending(false);
    }
  }

  return (
    <div
      className="inline-flex items-center rounded-lg border border-sidebar-border p-0.5"
      role="group"
      aria-label="AIの応答状態"
    >
      {OPTIONS.map((o) => {
        const Icon = o.icon;
        const active = state === o.value;
        return (
          <button
            key={o.value}
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              e.preventDefault();
              void set(o.value);
            }}
            disabled={pending}
            title={o.hint}
            aria-pressed={active}
            className={cn(
              "inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md text-[12px] transition",
              active
                ? "bg-secondary text-secondary-foreground font-medium"
                : "text-muted-foreground hover:text-foreground",
              pending && "opacity-60",
            )}
          >
            <Icon className="w-3.5 h-3.5" aria-hidden />
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

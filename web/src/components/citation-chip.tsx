"use client";

import * as Popover from "@radix-ui/react-popover";
import { ExternalLink } from "lucide-react";
import type { Citation } from "@/types";
import { cn } from "@/lib/cn";

export function CitationChip({ citation, compact = false }: { citation: Citation; compact?: boolean }) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          className={cn(
            "inline-flex items-center justify-center align-text-top",
            "h-[18px] min-w-[22px] px-1 mx-0.5 rounded-md",
            "bg-secondary text-secondary-foreground text-[11px] font-medium leading-none",
            "hover:bg-accent border border-border transition",
            compact && "h-4 min-w-[18px] text-[10px]",
          )}
          aria-label={`出典 ${citation.n}: ${citation.doc_title}`}
        >
          {citation.n}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="top"
          sideOffset={6}
          className="w-80 rounded-xl bg-popover text-popover-foreground border border-border shadow-xl p-4 text-sm z-50"
        >
          <div className="flex items-start justify-between gap-2 mb-2">
            <div className="font-medium leading-snug">{citation.doc_title}</div>
            {citation.doc_url && (
              <a
                href={citation.doc_url}
                target="_blank"
                rel="noreferrer"
                className="shrink-0 text-muted-foreground hover:text-foreground"
                aria-label="NotePMで開く"
              >
                <ExternalLink className="w-4 h-4" />
              </a>
            )}
          </div>
          {citation.heading_path.length > 0 && (
            <div className="text-xs text-muted-foreground mb-2 truncate">
              {citation.heading_path.join(" / ")}
            </div>
          )}
          <p className="text-xs leading-relaxed text-muted-foreground line-clamp-6 whitespace-pre-wrap">
            {citation.snippet}
          </p>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** Replace `[N]` / `[N][M]` markers in `text` with hoverable citation chips. */
export function renderWithCitations(text: string, citations: Citation[]): React.ReactNode {
  if (!citations || citations.length === 0) return text;
  const byN = new Map(citations.map((c) => [c.n, c]));

  const parts: React.ReactNode[] = [];
  const re = /\[(\d+)\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const n = Number(m[1]);
    const cit = byN.get(n);
    if (!cit) continue;
    if (m.index > last) parts.push(text.slice(last, m.index));
    parts.push(<CitationChip key={`${m.index}-${n}`} citation={cit} />);
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

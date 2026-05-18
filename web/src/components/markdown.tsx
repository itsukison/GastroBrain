"use client";

/**
 * Lightweight markdown renderer scoped to the subset the model emits in
 * answer text: paragraphs, bold, bullet lists, headings, inline code. Heavy
 * markdown (tables, raw HTML) is intentionally not handled — chat answers
 * should stay readable, not become little documents.
 *
 * We render manually instead of pulling in react-markdown so citation chips
 * (`[N]`) can interleave with formatted spans without parser plug-ins.
 */
import { renderWithCitations } from "./citation-chip";
import type { Citation } from "@/types";

export function Markdown({ text, citations = [] }: { text: string; citations?: Citation[] }) {
  const blocks = splitBlocks(text);
  return (
    <div className="prose-like space-y-3 leading-relaxed">
      {blocks.map((b, i) => renderBlock(b, i, citations))}
    </div>
  );
}

type Block =
  | { kind: "heading"; level: 1 | 2 | 3; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "paragraph"; text: string };

function splitBlocks(src: string): Block[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let buf: string[] = [];
  let list: string[] | null = null;
  const flushPara = () => {
    if (buf.length) {
      blocks.push({ kind: "paragraph", text: buf.join(" ").trim() });
      buf = [];
    }
  };
  const flushList = () => {
    if (list && list.length) blocks.push({ kind: "list", items: list });
    list = null;
  };
  for (const raw of lines) {
    const line = raw;
    const trimmed = line.trim();
    const h = /^(#{1,3})\s+(.*)$/.exec(trimmed);
    const li = /^[-*]\s+(.*)$/.exec(trimmed);
    if (h) {
      flushPara();
      flushList();
      blocks.push({ kind: "heading", level: h[1].length as 1 | 2 | 3, text: h[2] });
    } else if (li) {
      flushPara();
      if (!list) list = [];
      list.push(li[1]);
    } else if (trimmed === "") {
      flushPara();
      flushList();
    } else {
      flushList();
      buf.push(trimmed);
    }
  }
  flushPara();
  flushList();
  return blocks;
}

function renderBlock(b: Block, i: number, citations: Citation[]) {
  if (b.kind === "heading") {
    const sizes = { 1: "text-xl font-semibold", 2: "text-lg font-semibold", 3: "text-base font-semibold" };
    const Tag = (`h${b.level}` as unknown) as keyof React.JSX.IntrinsicElements;
    return (
      <Tag key={i} className={sizes[b.level] + " mt-2"}>
        {renderInline(b.text, citations)}
      </Tag>
    );
  }
  if (b.kind === "list") {
    return (
      <ul key={i} className="list-disc pl-6 space-y-1">
        {b.items.map((it, j) => (
          <li key={j}>{renderInline(it, citations)}</li>
        ))}
      </ul>
    );
  }
  return (
    <p key={i} className="whitespace-pre-wrap">
      {renderInline(b.text, citations)}
    </p>
  );
}

function renderInline(text: string, citations: Citation[]): React.ReactNode {
  // Bold **x** then inline `code`. Applied in order; citations replace `[N]` last.
  const tokens: { kind: "text" | "bold" | "code"; text: string }[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) tokens.push({ kind: "text", text: text.slice(last, m.index) });
    const t = m[0];
    if (t.startsWith("**")) tokens.push({ kind: "bold", text: t.slice(2, -2) });
    else tokens.push({ kind: "code", text: t.slice(1, -1) });
    last = m.index + t.length;
  }
  if (last < text.length) tokens.push({ kind: "text", text: text.slice(last) });
  return tokens.map((tok, i) => {
    if (tok.kind === "bold") return <strong key={i}>{renderWithCitations(tok.text, citations)}</strong>;
    if (tok.kind === "code")
      return (
        <code key={i} className="px-1 py-0.5 rounded bg-muted text-[0.9em]">
          {tok.text}
        </code>
      );
    return <span key={i}>{renderWithCitations(tok.text, citations)}</span>;
  });
}

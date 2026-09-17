"use client";

import Link, { useLinkStatus } from "next/link";
import type { ComponentProps } from "react";
import { cn } from "@/lib/cn";

function PendingIndicator() {
  const { pending } = useLinkStatus();
  if (!pending) return null;
  return (
    <span role="status" className="pointer-events-none absolute inset-0 rounded-[inherit] bg-foreground/5 ring-1 ring-inset ring-foreground/20">
      <span className="absolute inset-x-0 bottom-0 h-0.5 bg-foreground/50 motion-safe:animate-pulse" aria-hidden />
      <span className="sr-only">移動中…</span>
    </span>
  );
}

/** Keep Next's prefetch, keyboard and modified-click behavior intact. */
export function NavigationLink({ children, className, ...props }: ComponentProps<typeof Link>) {
  return (
    <Link {...props} className={cn("relative", className)}>
      {children}
      <PendingIndicator />
    </Link>
  );
}

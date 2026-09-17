"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import type { ComponentProps, ReactNode } from "react";
import { NavigationLink } from "./navigation-link";

const LastChatContext = createContext("/");

/** In-memory only: a full logout/login navigation discards the previous route. */
export function NavigationProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [lastChat, setLastChat] = useState("/");
  useEffect(() => {
    if (/^\/c\/[^/]+$/.test(pathname)) setLastChat(pathname);
    else if (pathname === "/login") setLastChat("/");
  }, [pathname]);

  return <LastChatContext.Provider value={lastChat}>{children}</LastChatContext.Provider>;
}

export function BackToChatLink(props: Omit<ComponentProps<typeof NavigationLink>, "href">) {
  const href = useContext(LastChatContext);
  // The fallback can redirect to /new, which currently creates a thread.
  return <NavigationLink {...props} href={href} prefetch={href === "/" ? false : props.prefetch} />;
}

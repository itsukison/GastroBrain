import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Gastrobrain",
  description: "Gastroduce社内ナレッジQ&A",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ja" suppressHydrationWarning>
      <body className="min-h-screen bg-background text-foreground">{children}</body>
    </html>
  );
}

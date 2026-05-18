import { notFound } from "next/navigation";
import { ChatThread } from "@/components/chat-thread";
import { RuntimeProvider } from "@/components/runtime-provider";
import { backendGet } from "@/lib/server-api";
import type { MessageRow, ThreadSummary } from "@/types";

export const dynamic = "force-dynamic";

export default async function ChatPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let data: { thread: ThreadSummary; messages: MessageRow[] };
  try {
    data = await backendGet<{ thread: ThreadSummary; messages: MessageRow[] }>(
      `/v1/threads/${id}`,
    );
  } catch {
    notFound();
  }

  return (
    <RuntimeProvider key={id} conversationId={id} initialMessages={data.messages}>
      <ChatThread />
    </RuntimeProvider>
  );
}

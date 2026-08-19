import { requireUser } from "@/lib/auth-guard";
import { VoiceSession } from "@/components/voice-session";

export const dynamic = "force-dynamic";

/**
 * Voice surface. Outside the (chat) route group on purpose — a sidebar next to
 * a live conversation is noise, and the transcript needs the full column.
 * The conversation row is minted client-side when the user actually starts
 * talking, so opening this page costs nothing.
 */
export default async function VoicePage() {
  await requireUser("/voice");
  return <VoiceSession />;
}

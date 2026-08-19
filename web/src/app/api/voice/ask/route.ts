import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
// One voice turn = retrieval + a capped Sonnet answer. Well under this in
// practice; the ceiling exists so a stuck turn fails rather than hangs the
// conversation.
export const maxDuration = 60;

export async function POST(request: Request) {
  return forward(request, "/v1/voice/ask");
}

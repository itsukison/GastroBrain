import { Suspense } from "react";
import { RecallVoice } from "@/components/recall-voice";

export const dynamic = "force-dynamic";
export default function RecallPage() {
  return <Suspense><RecallVoice /></Suspense>;
}

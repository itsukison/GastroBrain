import { forward } from "@/lib/api";
export const dynamic = "force-dynamic";
export function GET(request: Request) { return forward(request, "/v1/recall/runs"); }
export function POST(request: Request) { return forward(request, "/v1/recall/runs"); }

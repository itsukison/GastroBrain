import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const qs = url.searchParams.toString();
  return forward(request, `/v1/threads${qs ? `?${qs}` : ""}`);
}

export async function POST(request: Request) {
  return forward(request, "/v1/threads");
}

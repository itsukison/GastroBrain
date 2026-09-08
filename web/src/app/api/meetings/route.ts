import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const qs = url.searchParams.toString();
  return forward(request, `/v1/meetings${qs ? `?${qs}` : ""}`);
}

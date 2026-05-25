import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return forward(request, "/v1/oauth/sessions");
}

import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return forward(request, "/v1/mcp/tokens");
}

export async function POST(request: Request) {
  return forward(request, "/v1/mcp/tokens", { method: "POST", passBody: false });
}

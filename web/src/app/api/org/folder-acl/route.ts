import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  return forward(request, "/v1/org/folder-acl", { method: "POST" });
}

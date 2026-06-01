import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function PUT(request: Request, ctx: { params: Promise<{ email: string }> }) {
  const { email } = await ctx.params;
  return forward(request, `/v1/org/members/${encodeURIComponent(email)}`, { method: "PUT" });
}

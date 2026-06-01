import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function DELETE(request: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  return forward(request, `/v1/org/folder-acl/${id}`, { method: "DELETE" });
}

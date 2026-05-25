import { forward } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return forward(request, `/v1/mcp/tokens/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

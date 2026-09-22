import { forward } from "@/lib/api";
export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) return new Response(null, { status: 400 });
  return forward(request, `/v1/recall/runs/${id}/stop`);
}

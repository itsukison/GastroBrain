import { strict as assert } from "node:assert";
import { test } from "node:test";
import type { SupabaseClient } from "@supabase/supabase-js";
import { readVerifiedSession } from "./verified-session.ts";

function client(options: { userId?: string | null; sessionId?: string; userError?: boolean; sessionError?: boolean } = {}) {
  const calls: string[] = [];
  const userId = options.userId === undefined ? "user-a" : options.userId;
  const supabase = { auth: {
    async getUser() {
      calls.push("verify");
      return { data: { user: userId ? { id: userId } : null }, error: options.userError ? new Error("rejected") : null };
    },
    async getSession() {
      // Models the cookie-hydration dependency on getUser after Slack login.
      assert.equal(calls[0], "verify");
      calls.push("session");
      return { data: { session: options.sessionError ? null : { user: { id: options.sessionId ?? userId }, access_token: `refreshed-${userId}` } }, error: null };
    },
  } } as unknown as Pick<SupabaseClient, "auth">;
  return { supabase, calls };
}

test("verifies identity before reading the refreshed session token", async () => {
  const { supabase, calls } = client();
  assert.deepEqual(await readVerifiedSession(supabase), { user: { id: "user-a" }, token: "refreshed-user-a" });
  assert.deepEqual(calls, ["verify", "session"]);
});

test("rejected or missing identity never reads a token from the cookie", async () => {
  for (const options of [{ userId: null }, { userError: true }]) {
    const { supabase, calls } = client(options);
    assert.equal(await readVerifiedSession(supabase), null);
    assert.deepEqual(calls, ["verify"]);
  }
});

test("missing or mismatched sessions fail closed", async () => {
  for (const options of [{ sessionError: true }, { sessionId: "another-user" }]) {
    assert.equal(await readVerifiedSession(client(options).supabase), null);
  }
});

test("independent users never reuse another client's session", async () => {
  const [a, b] = await Promise.all([
    readVerifiedSession(client({ userId: "a" }).supabase),
    readVerifiedSession(client({ userId: "b" }).supabase),
  ]);
  assert.equal(a?.token, "refreshed-a");
  assert.equal(b?.token, "refreshed-b");
});

// Production-mode HTTP checks with synthetic auth/data, no browser or live credentials.
// Builds in a temporary copy so the project's .next and .env files stay untouched.
// Run: node scripts/check-navigation.mjs
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { cp, mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const directory = await mkdtemp(join(tmpdir(), "gastro-navigation-"));
const counts = new Map();
const writes = [];
const now = new Date().toISOString();
let app;
let appLogs = "";

function identity(id) {
  return { id, email: `${id}@example.test`, aud: "authenticated", role: "authenticated", created_at: now, app_metadata: {}, user_metadata: {} };
}

function token(id) {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${encode({ alg: "HS256", typ: "JWT" })}.${encode({ sub: id, exp: Math.floor(Date.now() / 1000) + 3600 })}.fixture-signature`;
}

function cookie(id) {
  const session = { access_token: token(id), refresh_token: "fixture-refresh", expires_in: 3600, expires_at: Math.floor(Date.now() / 1000) + 3600, token_type: "bearer", user: identity(id) };
  return `sb-127-auth-token=base64-${Buffer.from(JSON.stringify(session)).toString("base64url")}`;
}

function thread(id) {
  return { id, title: `fixture-sidebar-${id}`, created_at: now, updated_at: now, archived_at: null };
}

function meeting(id) {
  return { id, title: `fixture-meeting-${id}`, scheduled_at: now, started_at: now, ended_at: now, status: "ended", summary_status: "ready", participant_count: 1, agent_state: "asleep", meet_url: null };
}

const upstream = createServer(async (request, response) => {
  const url = new URL(request.url, "http://fixture");
  const bearer = request.headers.authorization?.split(" ")[1];
  const id = bearer ? JSON.parse(Buffer.from(bearer.split(".")[1], "base64url")).sub : "anonymous";
  const send = (status, body) => { response.writeHead(status, { "Content-Type": "application/json" }); response.end(JSON.stringify(body)); };
  if (request.method !== "GET") {
    writes.push(url.pathname);
    return send(405, {});
  }
  if (url.pathname === "/auth/v1/user") {
    counts.set(id, (counts.get(id) ?? 0) + 1);
    return send(200, identity(id));
  }
  await delay(url.pathname === "/v1/threads" ? 900 : 500);
  if (url.pathname === "/v1/meetings") return send(200, { meetings: [meeting(id)] });
  if (url.pathname === "/v1/meetings/missing") return send(404, { detail: "meeting not found" });
  if (url.pathname === "/v1/meetings/unavailable") return send(503, { detail: "fixture outage" });
  if (url.pathname.startsWith("/v1/meetings/")) return send(200, {
    meeting: { ...meeting(id), summary: `fixture-summary-${id}`, next_actions: [] },
    participants: [], segments: [], threads: [],
  });
  if (url.pathname === "/v1/threads") return send(200, { threads: [thread(id)] });
  if (url.pathname.startsWith("/v1/threads/")) return send(200, {
    thread: thread(id), messages: [{ id: "message-1", role: "user", content: `fixture-message-${id}`, created_at: now, citations: [], query_id: null, feedback: null }],
  });
  return send(404, {});
});

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

try {
  const upstreamPort = await listen(upstream);
  await cp(root, directory, { recursive: true, filter: (source) => {
    const first = relative(root, source).split(/[\\/]/)[0];
    return !["node_modules", ".next", ".git", "tsconfig.tsbuildinfo"].includes(first) && !first.startsWith(".env");
  } });
  await symlink(join(root, "node_modules"), join(directory, "node_modules"), "dir");
  const env = {
    ...process.env, NEXT_TELEMETRY_DISABLED: "1",
    NEXT_PUBLIC_SUPABASE_URL: `http://127.0.0.1:${upstreamPort}`,
    NEXT_PUBLIC_SUPABASE_ANON_KEY: "fixture-anon-key",
    GASTROBRAIN_API_URL: `http://127.0.0.1:${upstreamPort}`,
  };
  const next = join(root, "node_modules/next/dist/bin/next");
  console.log("Building isolated production fixture…");
  const build = spawn(process.execPath, [next, "build"], { cwd: directory, env, stdio: ["ignore", "pipe", "pipe"] });
  let buildLogs = "";
  build.stdout.on("data", (chunk) => { buildLogs += chunk; });
  build.stderr.on("data", (chunk) => { buildLogs += chunk; });
  const [buildCode] = await once(build, "exit");
  assert.equal(buildCode, 0, buildLogs);

  const portProbe = createServer();
  const port = await listen(portProbe);
  await new Promise((resolve) => portProbe.close(resolve));
  app = spawn(process.execPath, [next, "start", "--hostname", "127.0.0.1", "--port", String(port)], { cwd: directory, env, stdio: ["ignore", "pipe", "pipe"] });
  app.stdout.on("data", (chunk) => { appLogs += chunk; });
  app.stderr.on("data", (chunk) => { appLogs += chunk; });
  const base = `http://127.0.0.1:${port}`;
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (app.exitCode !== null) throw new Error(appLogs);
    try { ready = (await fetch(`${base}/login`)).ok; } catch {}
    if (ready) break;
    await delay(100);
  }
  assert.ok(ready, "fixture server did not start");

  async function read(path, id, markers = []) {
    const start = performance.now();
    const response = await fetch(`${base}${path}`, { headers: { Cookie: cookie(id) } });
    const decoder = new TextDecoder();
    let body = "";
    const observed = new Map();
    for await (const chunk of response.body) {
      body += decoder.decode(chunk, { stream: true });
      for (const marker of markers) {
        if (!observed.has(marker) && body.includes(marker)) observed.set(marker, Math.round(performance.now() - start));
      }
    }
    return { body, observed };
  }

  const unauthenticated = await fetch(`${base}/meetings`, { redirect: "manual" });
  assert.equal(unauthenticated.status, 307);
  assert.ok(unauthenticated.headers.get("location").includes("/login?next=%2Fmeetings"));

  const skeleton = 'aria-busy="true"';
  const list = await read("/meetings", "alice", [skeleton, "fixture-meeting-alice"]);
  assert.ok(list.observed.has(skeleton), "list skeleton missing");
  assert.ok(list.observed.get("fixture-meeting-alice") - list.observed.get(skeleton) > 200, "list blocked on backend data");
  assert.equal(counts.get("alice"), 2, "expected middleware + one shared server-render auth verification");
  console.log("List streaming (ms):", Object.fromEntries(list.observed));

  const detail = await read("/meetings/example", "alice", [skeleton, "fixture-summary-alice"]);
  assert.ok(detail.observed.get("fixture-summary-alice") - detail.observed.get(skeleton) > 200, "detail blocked on backend data");
  assert.equal(counts.get("alice"), 4);
  console.log("Detail streaming (ms):", Object.fromEntries(detail.observed));

  const chat = await read("/c/example", "alice", ["fixture-message-alice", "fixture-sidebar-alice"]);
  assert.ok(chat.observed.get("fixture-sidebar-alice") - chat.observed.get("fixture-message-alice") > 200, "chat content blocked on sidebar");
  assert.equal(counts.get("alice"), 6, "layout, page and sidebar must share auth");
  console.log("Chat streaming (ms):", Object.fromEntries(chat.observed));

  const [alice, bob] = await Promise.all([read("/meetings", "alice"), read("/meetings", "bob")]);
  assert.ok(alice.body.includes("fixture-meeting-alice") && !alice.body.includes("fixture-meeting-bob"));
  assert.ok(bob.body.includes("fixture-meeting-bob") && !bob.body.includes("fixture-meeting-alice"));
  assert.equal(counts.get("alice"), 8);
  assert.equal(counts.get("bob"), 2);

  const missing = await read("/meetings/missing", "alice");
  const unavailable = await read("/meetings/unavailable", "alice");
  assert.ok(missing.body.includes("NEXT_HTTP_ERROR_FALLBACK;404"), "missing meeting must remain a 404");
  assert.ok(!unavailable.body.includes("NEXT_HTTP_ERROR_FALLBACK;404"), "upstream outage must not become a 404");
  assert.ok(!unavailable.body.includes("fixture-summary"));
  assert.deepEqual(writes, [], "read-only navigation created data");
  console.log("PASS: streaming, shared auth, request isolation, login redirect, 404/error distinction, no writes.");
} catch (error) {
  console.error(appLogs.slice(-4000));
  throw error;
} finally {
  if (app && app.exitCode === null) { app.kill("SIGTERM"); await once(app, "exit"); }
  await new Promise((resolve) => upstream.close(resolve));
  await rm(directory, { recursive: true, force: true });
}

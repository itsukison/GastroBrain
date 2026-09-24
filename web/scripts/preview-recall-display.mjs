// Render the real component with synthetic data, without auth, a mic, or a bot.
// node scripts/preview-recall-display.mjs [output-directory]
import { readFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(process.argv[2] ?? "/tmp/recall-display-preview");
const require = createRequire(import.meta.url);
// Compile just the presentation component and its pure helper in memory.
function load(filename) {
  const source = readFileSync(filename, "utf8");
  const module = { exports: {} };
  const localRequire = name => {
    if (name.endsWith(".css")) {
      const css = readFileSync(resolve(dirname(filename), name), "utf8");
      return Object.fromEntries([...css.matchAll(/\.([a-zA-Z][\w-]*)/g)].map(m => [m[1], m[1]]));
    }
    if (!name.startsWith(".")) return require(name);
    const path = resolve(dirname(filename), name);
    return load([path, path + ".ts", path + ".tsx"].find(existsSync));
  };
  const js = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText;
  new Function("require", "module", "exports", js)(localRequire, module, module.exports);
  return module.exports;
}
const { RecallDisplay } = load(resolve(root, "src/components/recall-display.tsx"));
const css = readFileSync(resolve(root, "src/components/recall-display.module.css"), "utf8");
const answer = { message_id: "fixture", answer: "初回の打ち合わせでは、現在の運用と課題を確認します。次回までに、必要な資料と担当者を整理してください。",
  sources: [{ n: 1, title: "初回ヒアリングの進め方" }, { n: 2, title: "案件引き継ぎ・提案準備ガイド" }] };
const fixtures = {
  connecting: { availability: "connecting" },
  asleep: { availability: "ready" },
  awake: { availability: "ready", agentState: "open" },
  searching: { availability: "ready", agentState: "open", searching: true, answering: true },
  answer: { availability: "ready", agentState: "open", answerView: { answer, failed: false } },
  quiet_playback: { availability: "ready", agentState: "asleep", answering: true, answerView: { answer, failed: false } },
  long: { availability: "ready", agentState: "open", answerView: { failed: false,
    answer: { ...answer, answer: answer.answer.repeat(5), sources: Array.from({ length: 6 }, (_, i) => ({ n: i + 1, title: "営業部門・運用チーム向けのとても長い日本語の資料名と案件引き継ぎ手順についての詳しい説明" })) } } },
  no_sources: { availability: "ready", agentState: "open", answerView: { answer: { ...answer, sources: [] }, failed: false } },
  search_failed: { availability: "ready", agentState: "open", answerView: { answer: null, failed: true } },
  reconnecting: { availability: "reconnecting", agentState: "open", answerView: { answer, failed: false } },
  failed: { availability: "failed" },
  ended: { availability: "ended" },
};
mkdirSync(output, { recursive: true });
for (const [name, props] of Object.entries(fixtures)) {
  const markup = renderToStaticMarkup(React.createElement(RecallDisplay, props));
  writeFileSync(resolve(output, `${name}.html`), `<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>商談AI — ${name}</title><style>html,body{margin:0;height:100%;}*{box-sizing:border-box;} ${css}</style><body>${markup}</body></html>`);
}
console.log(`Rendered ${Object.keys(fixtures).length} display fixtures to ${output}`);

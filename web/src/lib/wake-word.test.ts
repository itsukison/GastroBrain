import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { canonicalize, findWakeWord, matchesWakeWord } from "./wake-word.ts";

describe("canonicalize", () => {
  it("folds width and case", () => {
    assert.equal(canonicalize("商談ＡＩ"), canonicalize("商談ai"));
    assert.equal(canonicalize("GastroBrain"), canonicalize("gastrobrain"));
  });

  it("folds hiragana into katakana", () => {
    assert.equal(canonicalize("がすとろぶれいん"), canonicalize("ガストロブレイン"));
  });

  it("folds long vowels, however they are spelled", () => {
    assert.equal(canonicalize("ガストロブレイン"), canonicalize("ガストロブレーン"));
    assert.equal(canonicalize("しょうだん"), canonicalize("しょーだん"));
    assert.equal(canonicalize("エーアイ"), canonicalize("エイアイ"));
  });

  it("folds half-width katakana", () => {
    assert.equal(canonicalize("ｶﾞｽﾄﾛﾌﾞﾚｲﾝ"), canonicalize("ガストロブレイン"));
  });
});

describe("findWakeWord", () => {
  it("matches every spelling in the alias list", () => {
    for (const spelling of [
      "商談AI",
      "商談ＡＩ",
      "商談エーアイ",
      "しょうだんエーアイ",
      "ショウダンエーアイ",
      "ショーダンエイアイ",
      "ガストロブレイン",
      "ガストロブレーン",
      "がすとろぶれいん",
    ]) {
      assert.ok(matchesWakeWord(spelling), `expected a match for ${spelling}`);
    }
  });

  it("matches mid-sentence", () => {
    assert.ok(matchesWakeWord("ちょっと待ってね、商談AI、楽天の手数料は？"));
  });

  it("extracts the question that follows the name", () => {
    const match = findWakeWord("商談AI、楽天の送料無料ラインは？");
    assert.ok(match);
    assert.equal(match.question, "楽天の送料無料ラインは？");
    assert.equal(match.alias, "商談AI");
  });

  it("reports the span in the original string, not the canonical one", () => {
    const text = "えーと、ガストロブレイン、これは？";
    const match = findWakeWord(text);
    assert.ok(match);
    assert.equal(text.slice(match.start, match.end), "ガストロブレイン");
  });

  it("returns an empty question when the name comes last", () => {
    const match = findWakeWord("その件どう思う、商談AI");
    assert.ok(match);
    assert.equal(match.question, "");
  });

  it("prefers the last occurrence, which is the one the question follows", () => {
    const match = findWakeWord("商談AI、じゃなくて、商談AI、粗利は？");
    assert.ok(match);
    assert.equal(match.question, "粗利は？");
  });
});

describe("findWakeWord — false positives", () => {
  it("ignores 商談 on its own, which is said constantly in a 商談", () => {
    for (const line of [
      "次の商談はいつですか",
      "商談の内容をまとめておきます",
      "AIの導入を検討しています",
      "うちのAIツールは",
    ]) {
      assert.equal(matchesWakeWord(line), false, `unexpected match in ${line}`);
    }
  });

  it("does not join 商談 and AI across a clause boundary", () => {
    // The reason punctuation becomes a boundary sentinel instead of being deleted.
    assert.equal(matchesWakeWord("この商談、AIで自動化できませんか"), false);
    assert.equal(matchesWakeWord("先方の商談。AIの話も出ました"), false);
  });

  it("still matches when the only separator is a space", () => {
    assert.ok(matchesWakeWord("商談 AI 教えて"));
  });

  it("handles empty and whitespace-only input", () => {
    assert.equal(findWakeWord(""), null);
    assert.equal(findWakeWord("   "), null);
    assert.equal(findWakeWord("、。"), null);
  });
});

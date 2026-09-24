import { AudioLines, BookOpen, Check, CircleAlert, Moon, Radio, Search } from "lucide-react";
import { recallDisplayState, type RecallAnswerView, type RecallAvailability } from "../lib/recall-display";
import type { AgentState } from "../lib/meeting-mode";
import styles from "./recall-display.module.css";

export type RecallDisplayProps = {
  availability: RecallAvailability;
  agentState?: AgentState;
  searching?: boolean;
  answering?: boolean;
  answerView?: RecallAnswerView;
};

/** This page becomes camera pixels. Intentionally contains no interactive UI. */
export function RecallDisplay({ availability, agentState = "asleep", searching = false,
  answering = false, answerView }: RecallDisplayProps) {
  const state = recallDisplayState(availability, agentState);
  const ready = availability === "ready";
  const awake = agentState === "open";
  const answer = ready ? answerView?.answer : null;
  const Icon = !ready ? (availability === "failed" ? CircleAlert : Radio) : awake ? AudioLines : Moon;
  const activity = searching ? "資料を検索中" : answering ? "応答中" : answerView?.failed ? "資料の検索に失敗しました" : null;
  return (
    <main className={styles.display} data-tone={state.tone}>
      <header className={styles.header}>
        <div className={styles.brand}><span className={styles.brandMark}><AudioLines /></span>商談AI</div>
        <span className={styles.eyebrow}>GASTROBRAIN</span>
      </header>

      <section className={styles.status} aria-label="エージェントの状態">
        <div className={styles.statusLine}>
          <Icon className={styles.stateIcon} aria-hidden />
          <h1>{state.label}</h1>
          {ready && activity && <span className={styles.activity}>
            {searching ? <Search aria-hidden /> : !answering && answerView?.failed ? <CircleAlert aria-hidden /> : <AudioLines aria-hidden />}{activity}
          </span>}
        </div>
        <p className={styles.detail}>{state.detail}</p>
      </section>

      {ready && <div className={styles.command}>
        <span className={styles.commandLabel}>{awake ? "待機に戻す" : "呼びかける"}</span>
        <strong>「商談AI、{awake ? "静かに" : "起きて"}」</strong>
      </div>}

      {answer ? <section className={styles.answerCard} aria-label="最新の回答と参照資料">
        <div className={styles.answerBody}>
          <div className={styles.cardLabel}><Check aria-hidden />最新の回答</div>
          <p className={styles.answerText}>{answer.answer}</p>
        </div>
        {answer.sources.length > 0 && <aside className={styles.sources}>
          <div className={styles.cardLabel}><BookOpen aria-hidden />参照資料</div>
          <ol>{answer.sources.slice(0, 2).map(source => <li key={source.n}>
            <span className={styles.sourceNumber}>{source.n}</span><span>{source.title}</span>
          </li>)}</ol>
          {answer.sources.length > 2 && <p className={styles.more}>ほか {answer.sources.length - 2} 件</p>}
        </aside>}
      </section> : <div className={styles.empty}>
        {ready && <><span className={styles.emptyRule} /><p>{searching ? "社内資料を確認しています" : answering ? "回答を準備しています" : "必要なときに、会議のそばで。"}</p></>}
      </div>}

      <footer className={styles.footer}>
        {ready && <><span>音声・Meetチャットで操作できます</span><span>質問がなければ90秒で待機へ</span></>}
      </footer>
    </main>
  );
}

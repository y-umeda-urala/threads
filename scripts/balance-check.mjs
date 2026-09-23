/**
 * API がいま呼べるかを確かめて、結果をファイルに残す
 * ------------------------------------------------------------------
 * Anthropic には「クレジット残高」を返す API が無い（2026-09 時点）。
 * Usage and Cost API で取れるのは使った額で、残高ではない。しかも Admin キーが要る。
 *
 * そこで、いちばん小さいリクエストを1回だけ投げて、返ってきたもので判定する。
 *   200 … 呼べる
 *   400 かつ credit balance … 残高切れ。生成・収集・レビューが全部止まる
 *   401 … キーが違う
 * 費用は1回あたり 0.00002 ドル未満。
 *
 * **結果は state/balance.json に書く（2026-09-23 追加）。**
 * Cowork の定時タスクは Mac も GitHub API も使えないことがあるため、
 * raw.githubusercontent.com から読めるファイルに落としておく。
 * 判定はこのファイルを見るだけで済む。
 *
 * 使い方: ANTHROPIC_API_KEY=xxx node scripts/balance-check.mjs
 * 呼べないときは終了コード 1 で終わる（ファイルは書いたあとで落ちる）。
 * ------------------------------------------------------------------
 */

import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';

const キー = process.env.ANTHROPIC_API_KEY;
const 名札 = process.env.ACCOUNT_LABEL || process.env.GITHUB_REPOSITORY || 'このリポジトリ';
const 出力先 = 'state/balance.json';

function 出す(行) {
  console.log(行);
  if (process.env.GITHUB_STEP_SUMMARY) {
    try { appendFileSync(process.env.GITHUB_STEP_SUMMARY, 行 + '\n'); } catch {}
  }
}

function 日本時間() {
  const d = new Date(Date.now() + 9 * 60 * 60 * 1000);
  return d.toISOString().replace('T', ' ').slice(0, 16) + ' JST';
}

function 書く({ 状態, HTTP, 説明 }) {
  const 中身 = {
    リポジトリ: 名札,
    確認した時刻: 日本時間(),
    状態,               // ok / 残高切れ / キー不正 / レート制限 / 不明 / キー未設定
    止まるか: 状態 === 'ok' || 状態 === 'レート制限' ? 'いいえ' : 'はい',
    HTTP,
    説明
  };
  try {
    mkdirSync('state', { recursive: true });
    writeFileSync(出力先, JSON.stringify(中身, null, 2) + '\n', 'utf8');
    出す(`（${出力先} に書きました）`);
  } catch (e) {
    出す(`::warning::${出力先} に書けませんでした: ${e.message}`);
  }
}

if (!キー) {
  書く({ 状態: 'キー未設定', HTTP: 0, 説明: 'ANTHROPIC_API_KEY が設定されていません' });
  出す(`::error::${名札}: ANTHROPIC_API_KEY が設定されていません`);
  process.exit(1);
}

let res, 本文;
try {
  res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-api-key': キー, 'anthropic-version': '2023-06-01' },
    body: JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1, messages: [{ role: 'user', content: '1' }] })
  });
  本文 = await res.text();
} catch (e) {
  書く({ 状態: '不明', HTTP: 0, 説明: `通信に失敗: ${e.message}`.slice(0, 300) });
  出す(`::error::🔴 ${名札}: API に届きませんでした（${e.message}）`);
  process.exit(1);
}

if (res.ok) {
  書く({ 状態: 'ok', HTTP: res.status, 説明: 'API は呼べます' });
  出す(`✅ ${名札}: API は呼べます`);
  process.exit(0);
}

let 中身 = {};
try { 中身 = JSON.parse(本文).error ?? {}; } catch {}
const 文 = String(中身.message ?? 本文).slice(0, 300);

if (res.status === 400 && /credit balance/i.test(文)) {
  書く({ 状態: '残高切れ', HTTP: 400, 説明: 文 });
  出す(`::error::🔴 ${名札}: 残高切れです。入金するまで、生成・収集・レビューが全部止まります`);
  出す(`   ${文}`);
  process.exit(1);
}
if (res.status === 401) {
  書く({ 状態: 'キー不正', HTTP: 401, 説明: 文 });
  出す(`::error::🔴 ${名札}: APIキーが通りません（401）。Secrets の ANTHROPIC_API_KEY を確認してください`);
  出す(`   ${文}`);
  process.exit(1);
}
if (res.status === 429) {
  書く({ 状態: 'レート制限', HTTP: 429, 説明: 文 });
  出す(`::warning::🟡 ${名札}: レート制限（429）。残高の問題ではありません`);
  出す(`   ${文}`);
  process.exit(0);
}
書く({ 状態: '不明', HTTP: res.status, 説明: 文 });
出す(`::error::🔴 ${名札}: API が ${res.status} を返しました`);
出す(`   ${文}`);
process.exit(1);

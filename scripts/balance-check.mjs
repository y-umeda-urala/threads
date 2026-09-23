/**
 * API がいま呼べるかを確かめる
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
 * 使い方: ANTHROPIC_API_KEY=xxx node scripts/balance-check.mjs
 * 呼べないときは終了コード 1 で終わる。
 * ------------------------------------------------------------------
 */

const キー = process.env.ANTHROPIC_API_KEY;
const 名札 = process.env.ACCOUNT_LABEL || process.env.GITHUB_REPOSITORY || 'このリポジトリ';

function 出す(行) {
  console.log(行);
  if (process.env.GITHUB_STEP_SUMMARY) {
    try {
      require('node:fs').appendFileSync(process.env.GITHUB_STEP_SUMMARY, 行 + '\n');
    } catch {}
  }
}

if (!キー) {
  出す(`::error::${名札}: ANTHROPIC_API_KEY が設定されていません`);
  process.exit(1);
}

const res = await fetch('https://api.anthropic.com/v1/messages', {
  method: 'POST',
  headers: { 'content-type': 'application/json', 'x-api-key': キー, 'anthropic-version': '2023-06-01' },
  body: JSON.stringify({ model: 'claude-haiku-4-5', max_tokens: 1, messages: [{ role: 'user', content: '1' }] })
});
const 本文 = await res.text();

if (res.ok) {
  出す(`✅ ${名札}: API は呼べます`);
  process.exit(0);
}

let 中身 = {};
try { 中身 = JSON.parse(本文).error ?? {}; } catch {}
const 文 = String(中身.message ?? 本文).slice(0, 300);

if (res.status === 400 && /credit balance/i.test(文)) {
  出す(`::error::🔴 ${名札}: 残高切れです。入金するまで、生成・収集・レビューが全部止まります`);
  出す(`   ${文}`);
  process.exit(1);
}
if (res.status === 401) {
  出す(`::error::🔴 ${名札}: APIキーが通りません（401）。Secrets の ANTHROPIC_API_KEY を確認してください`);
  出す(`   ${文}`);
  process.exit(1);
}
if (res.status === 429) {
  出す(`::warning::🟡 ${名札}: レート制限（429）。残高の問題ではありません`);
  出す(`   ${文}`);
  process.exit(0);
}
出す(`::error::🔴 ${名札}: API が ${res.status} を返しました`);
出す(`   ${文}`);
process.exit(1);

/**
 * ネタ帳を自動で埋める（1リポジトリ＝1アカウント）
 * ------------------------------------------------------------------
 * 何をするか:
 *   neta/設定.json の「通す質問」でWeb検索し、出典URLの辿れる出来事だけを
 *   neta/ネタ帳.md の「書き足す場所」の下に追記して、コミットする。
 *   投稿文は書かない。事実と、括弧で「誰に何が起きるか」だけ。
 *
 * 動かし方:
 *   ANTHROPIC_API_KEY=xxx node scripts/neta-collect.mjs
 *   環境変数 DRY_RUN=1 を付けるとファイルを書かずに結果だけ出す。
 *
 * 何度実行しても壊れない:
 *   すでにネタ帳にある行（記号と空白のゆれを無視して同じもの）は入れない。
 *   新しい行が0件ならコミットもしない。
 *
 * 依存なし（Node 20以上の fetch をそのまま使う）。
 * ------------------------------------------------------------------
 */

import { readFileSync, writeFileSync, existsSync } from 'node:fs';

const 設定パス = 'neta/設定.json';
const ネタ帳パス = 'neta/ネタ帳.md';
const 見出し = '## 書き足す場所';
const 使わない見出し = '## 使ってほしくないネタ';

const APIキー = process.env.ANTHROPIC_API_KEY;
const 書かない = process.env.DRY_RUN === '1';

if (!APIキー) {
  console.error('ANTHROPIC_API_KEY がありません');
  process.exit(1);
}
if (!existsSync(設定パス)) {
  console.error(`${設定パス} がありません`);
  process.exit(1);
}

const 設定 = JSON.parse(readFileSync(設定パス, 'utf8'));
const 今日 = new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Tokyo' }); // yyyy-mm-dd

const ネタ帳 = existsSync(ネタ帳パス) ? readFileSync(ネタ帳パス, 'utf8') : 初期ネタ帳();

// ---- 「使ってほしくないネタ」を読み取って、指示に混ぜる ----
const 使わない = 節を取り出す(ネタ帳, 使わない見出し)
  .split('\n')
  .map((s) => s.replace(/^[-*・]\s*/, '').trim())
  .filter((s) => s && !s.startsWith('（') && !s.startsWith('('));

// ---- すでに入っている行（重複よけ） ----
const 既存 = new Set(
  ネタ帳
    .split('\n')
    .map((s) => 整える(s))
    .filter(Boolean)
);

// ==================================================================
//  指示文
// ==================================================================
const 指示 = `あなたは編集部の情報収集担当です。**ネタ帳を埋めるのが仕事で、投稿文は書きません。**

## 通す質問

${設定.通す質問}

答えが出ない出来事は入れないでください。

## 集めるもの

${(設定.集めるもの ?? []).map((s) => `- ${s}`).join('\n')}

## 集めないもの

${(設定.集めないもの ?? []).map((s) => `- ${s}`).join('\n')}
- 自社の作業手順、社内の習慣、お客様に言われたこと（実測で表示6〜15。読まれません）
- 出典が辿れない「〜だそうです」
- 災害・事件・病気など、人の被害が絡む出来事
- クライアントの未公開情報
${使わない.length ? `\n## このネタ帳で使ってほしくないもの\n\n${使わない.map((s) => `- ${s}`).join('\n')}` : ''}

## 書き方

1件1行、事実だけ。括弧で「誰に何が起きるか」を一言。例：

- 9/19-20 鯖江でめがねのお祭り。産地の工場が開く（→ ふだん見えない工程を見せる日）
- 県内に お堀の風景が見えるレストランができた（→ 立地そのものが説明になっている）

括弧が思いつかなければ空でかまいません。**入れないより入れるほうがよいです。**
感想や書き出し案は付けないでください（それは制作担当の仕事です）。

## 越えてはいけない線

- 投稿文を書かない。集めた事実を置くだけ
- **確認できない情報を入れない。出典URLが辿れるものだけ**
- ネタ帳にすでにある出来事を、言い換えて入れ直さない

## すでにネタ帳に入っているもの（これと同じ出来事は入れない）

${[...既存].filter((s) => s.startsWith('- ') && !/^- https?:\/\//.test(s)).slice(0, 120).join('\n') || '（まだありません）'}

## 今日

${今日}（日本時間）。前日ぶんを中心に、当日以降に使えるものを ${設定.件数 ?? '3〜5'} 件。

## 返し方

最後に、次の形の JSON だけを \`\`\`json のコードブロックで出してください。
説明は要りません。source は必ず実在する、辿れるURLにしてください。

\`\`\`json
{"items":[{"line":"- 本文（→ 誰に何が起きるか）","source":"https://…"}]}
\`\`\``;

// ==================================================================
//  API を叩く
// ==================================================================
const 本文 = await claudeに聞く(指示);
const 取れた = JSONを取り出す(本文);

if (!取れた || !Array.isArray(取れた.items)) {
  console.error('JSON を取り出せませんでした。返ってきた本文の先頭を出します:');
  console.error(本文.slice(0, 1500));
  process.exit(1);
}

// ==================================================================
//  重複をよけて、追記する
// ==================================================================
const 入れる = [];
const とばした = [];

for (const item of 取れた.items) {
  const line = 整形(item?.line);
  const source = String(item?.source ?? '').trim();
  if (!line) continue;
  if (!/^https?:\/\//.test(source)) {
    とばした.push(`${line}（出典URLが無い）`);
    continue;
  }
  if (既存.has(整える(line)) || 既存.has(整える(印を取る(line)))) {
    とばした.push(`${line}（すでにある）`);
    continue;
  }
  既存.add(整える(line));
  入れる.push({ line, source });
}

console.log(`集まった: ${取れた.items.length}件 ／ 入れる: ${入れる.length}件 ／ とばした: ${とばした.length}件`);
for (const s of とばした) console.log(`  とばした: ${s}`);

if (入れる.length === 0) {
  console.log('新しい行がないので、ファイルは変更しません。');
  出力('added', '0');
  process.exit(0);
}

const 塊 = [
  `### ${今日}`,
  '',
  ...入れる.map((x) => x.line),
  '',
  '<details><summary>出典</summary>',
  '',
  ...入れる.map((x) => `- ${x.source}`),
  '',
  '</details>',
  ''
].join('\n');

const 新しいネタ帳 = 見出しの下に入れる(ネタ帳, 見出し, 塊);

if (書かない) {
  console.log('--- DRY_RUN なので書きません。入るのはこれです ---');
  console.log(塊);
} else {
  writeFileSync(ネタ帳パス, 新しいネタ帳, 'utf8');
  console.log(`${ネタ帳パス} に ${入れる.length}件を追記しました。`);
}
出力('added', String(入れる.length));


// ==================================================================
//  小物
// ==================================================================

async function claudeに聞く(prompt) {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-api-key': APIキー,
      'anthropic-version': '2023-06-01'
    },
    body: JSON.stringify({
      model: 設定.モデル ?? 'claude-sonnet-5',
      max_tokens: 4000,
      tools: [{ type: 'web_search_20250305', name: 'web_search', max_uses: 設定.検索回数 ?? 8 }],
      messages: [{ role: 'user', content: prompt }]
    })
  });

  if (!res.ok) {
    throw new Error(`Anthropic API が ${res.status} を返しました: ${(await res.text()).slice(0, 500)}`);
  }
  const data = await res.json();
  return (data.content ?? [])
    .filter((b) => b.type === 'text')
    .map((b) => b.text)
    .join('\n');
}

function JSONを取り出す(text) {
  const m = text.match(/```json\s*([\s\S]*?)```/);
  const 素 = m ? m[1] : text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1);
  try {
    return JSON.parse(素);
  } catch {
    return null;
  }
}

function 節を取り出す(md, 見出し文字) {
  const i = md.indexOf(見出し文字);
  if (i === -1) return '';
  const 後ろ = md.slice(i + 見出し文字.length);
  const j = 後ろ.search(/\n##\s/);
  return j === -1 ? 後ろ : 後ろ.slice(0, j);
}

function 見出しの下に入れる(md, 見出し文字, 塊) {
  const i = md.indexOf(見出し文字);
  if (i === -1) {
    // 見出しが無いときは、黙って別の場所に入れず、末尾に見出しごと作る
    return `${md.trimEnd()}\n\n${見出し文字}\n\n${塊}`;
  }
  const 改行 = md.indexOf('\n', i);
  const 前 = md.slice(0, 改行 + 1);
  const 後 = md.slice(改行 + 1);
  return `${前}\n${塊}${後.startsWith('\n') ? 後 : '\n' + 後}`;
}

function 整形(s) {
  const t = String(s ?? '').replace(/\r/g, '').trim();
  if (!t) return '';
  return t.startsWith('- ') ? t : `- ${印を取る(t)}`;
}

function 印を取る(s) {
  return String(s).replace(/^[-*・•]\s*/, '');
}

function 整える(s) {
  return String(s ?? '')
    .replace(/ /g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function 出力(key, value) {
  if (process.env.GITHUB_OUTPUT) {
    writeFileSync(process.env.GITHUB_OUTPUT, `${key}=${value}\n`, { flag: 'a' });
  }
}

function 初期ネタ帳() {
  return `# ネタ帳\n\n${使わない見出し}\n\n（ここに書いたものは集めません）\n\n${見出し}\n`;
}

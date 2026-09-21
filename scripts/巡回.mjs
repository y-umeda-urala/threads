/**
 * 巡回先を直接見に行って、候補を集める
 * ------------------------------------------------------------------
 * neta/巡回先.json に書いた RSS と HTML 一覧を取りに行き、
 * 「名前・タイトル・URL・日付」の並びにして返す。
 * 判断はしない。集めるだけ。選ぶのは neta-collect.mjs（Claude）の仕事。
 *
 * 依存なし（Node 20 以上の fetch をそのまま使う）。
 * ------------------------------------------------------------------
 */

const 待ち時間 = 20000;
const ユーザーエージェント =
  'Mozilla/5.0 (compatible; neta-collect/1.0; +https://github.com/y-umeda-urala/threads)';

export async function 巡回(巡回先) {
  const 何日前まで = 巡回先.何日前まで ?? 10;
  const 上限 = 巡回先['1つの巡回先から渡す最大件数'] ?? 6;
  const 境目 = Date.now() - 何日前まで * 24 * 60 * 60 * 1000;

  const 仕事 = [
    ...(巡回先.RSS ?? []).map((s) => RSSを読む(s, 境目, 上限)),
    ...(巡回先.HTML ?? []).map((s) => HTMLを読む(s))
  ];
  const 結果 = await Promise.all(仕事);

  const 候補 = [];
  const 取れた = [];
  const 取れなかった = [];
  for (const r of 結果) {
    if (r.error) 取れなかった.push(`${r.名前}: ${r.error}`);
    else 取れた.push(`${r.名前}: ${r.items.length}件`);
    候補.push(...r.items);
  }
  return { 候補, 取れた, 取れなかった };
}

// ------------------------------------------------------------------ RSS

async function RSSを読む(先, 境目, 上限) {
  let xml;
  try {
    xml = await 取る(先.url);
  } catch (e) {
    return { 名前: 先.名前, items: [], error: String(e.message ?? e).slice(0, 80) };
  }

  const 塊 = [...xml.matchAll(/<(item|entry)\b[\s\S]*?<\/\1>/gi)].map((m) => m[0]);
  const items = [];
  for (const b of 塊) {
    const タイトル = 中身(b, 'title');
    const url = リンク(b);
    const 日付 = 日付を読む(b);
    if (!タイトル || !url) continue;
    // 日付が読めないものは、古いかどうか判断できないので残す
    if (日付 && 日付.getTime() < 境目) continue;
    items.push({
      名前: 先.名前,
      区分: 先.区分 ?? '',
      タイトル: タイトル.slice(0, 120),
      url,
      日付: 日付 ? 日付.toISOString().slice(0, 10) : ''
    });
  }
  items.sort((a, b) => (b.日付 || '').localeCompare(a.日付 || ''));
  return { 名前: 先.名前, items: items.slice(0, 上限) };
}

function 中身(塊, タグ) {
  const m = 塊.match(new RegExp(`<${タグ}\\b[^>]*>([\\s\\S]*?)</${タグ}>`, 'i'));
  return m ? ほぐす(m[1]) : '';
}

function リンク(塊) {
  // Atom は <link href="..."/>。alternate を優先する
  const atom = [...塊.matchAll(/<link\b([^>]*)\/?>/gi)]
    .map((m) => m[1])
    .filter((a) => /href\s*=/.test(a));
  if (atom.length) {
    const 本命 = atom.find((a) => /rel\s*=\s*["']?alternate/i.test(a)) ?? atom.find((a) => !/rel\s*=/.test(a)) ?? atom[0];
    const m = 本命.match(/href\s*=\s*["']([^"']+)["']/i);
    if (m) return m[1].trim();
  }
  const text = 中身(塊, 'link');
  if (text && /^https?:\/\//.test(text)) return text;
  // RSS 1.0（RDF）は item の rdf:about に入っていることがある
  const about = 塊.match(/rdf:about\s*=\s*["']([^"']+)["']/i);
  if (about) return about[1].trim();
  const guid = 中身(塊, 'guid');
  return /^https?:\/\//.test(guid) ? guid : '';
}

function 日付を読む(塊) {
  for (const タグ of ['pubDate', 'dc:date', 'updated', 'published']) {
    const s = 中身(塊, タグ);
    if (!s) continue;
    const d = new Date(s);
    if (!Number.isNaN(d.getTime())) return d;
  }
  return null;
}

// ------------------------------------------------------------------ HTML

async function HTMLを読む(先) {
  let html;
  try {
    html = await 取る(先.url);
  } catch (e) {
    return { 名前: 先.名前, items: [], error: String(e.message ?? e).slice(0, 80) };
  }
  const 形 = new RegExp(先['リンクの形'] ?? '.');
  const 元 = new URL(先.url);
  const 見た = new Set();
  const items = [];
  for (const m of html.matchAll(/<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)) {
    let url;
    try {
      url = new URL(m[1], 元).toString();
    } catch {
      continue;
    }
    if (!形.test(url) || 見た.has(url)) continue;
    const タイトル = ほぐす(m[2].replace(/<[^>]+>/g, ' '));
    if (タイトル.length < 4) continue;
    見た.add(url);
    items.push({ 名前: 先.名前, 区分: 先.区分 ?? '', タイトル: タイトル.slice(0, 120), url, 日付: '' });
    if (items.length >= (先.最大件数 ?? 20)) break;
  }
  return { 名前: 先.名前, items };
}

// ------------------------------------------------------------------ 小物

async function 取る(url) {
  const 中止 = AbortSignal.timeout ? AbortSignal.timeout(待ち時間) : undefined;
  const res = await fetch(url, {
    headers: { 'user-agent': ユーザーエージェント, accept: '*/*' },
    signal: 中止,
    redirect: 'follow'
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.text();
}

function ほぐす(s) {
  return String(s)
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;|&apos;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

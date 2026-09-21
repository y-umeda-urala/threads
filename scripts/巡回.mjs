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
// そっけない UA だと弾く（あるいは握手で切る）サイトがあるため、ふつうのブラウザに合わせる
const ユーザーエージェント =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';

export async function 巡回(巡回先) {
  const 何日前まで = 巡回先.何日前まで ?? 10;
  const 上限 = 巡回先['1つの巡回先から渡す最大件数'] ?? 6;
  const 境目 = Date.now() - 何日前まで * 24 * 60 * 60 * 1000;
  const 捨てる語 = 巡回先['見出しに入っていたら捨てる'] ?? [];

  const 仕事 = [
    ...(巡回先.RSS ?? []).map((s) => RSSを読む(s, 境目, 上限, 捨てる語)),
    ...(巡回先.HTML ?? []).map((s) => HTMLを読む(s, 捨てる語))
  ];
  const 結果 = await Promise.all(仕事);

  const 候補 = [];
  const 取れた = [];
  const 取れなかった = [];
  let 捨てた = 0;
  for (const r of 結果) {
    if (r.error) 取れなかった.push(`${r.名前}: ${r.error}`);
    else 取れた.push(`${r.名前}: ${r.items.length}件`);
    捨てた += r.捨てた ?? 0;
    候補.push(...r.items);
  }
  return { 候補, 取れた, 取れなかった, 捨てた };
}

/** 行政のお知らせや、終わった催しを落とす */
function 捨てるか(タイトル, 捨てる語) {
  return 捨てる語.some((w) => タイトル.includes(w));
}

// ------------------------------------------------------------------ RSS

async function RSSを読む(先, 境目, 上限, 捨てる語 = []) {
  let xml;
  try {
    xml = await 取る(先.url);
  } catch (e) {
    return { 名前: 先.名前, items: [], error: String(e.message ?? e).slice(0, 160) };
  }

  const 塊 = [...xml.matchAll(/<(item|entry)\b[\s\S]*?<\/\1>/gi)].map((m) => m[0]);
  const items = [];
  let 捨てた = 0;
  for (const b of 塊) {
    const タイトル = 中身(b, 'title');
    const url = リンク(b);
    const 日付 = 日付を読む(b);
    if (!タイトル || !url) continue;
    if (捨てるか(タイトル, 捨てる語)) {
      捨てた += 1;
      continue;
    }
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
  return { 名前: 先.名前, items: items.slice(0, 上限), 捨てた };
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

async function HTMLを読む(先, 捨てる語 = []) {
  let html;
  try {
    html = await 取る(先.url);
  } catch (e) {
    return { 名前: 先.名前, items: [], error: String(e.message ?? e).slice(0, 160) };
  }
  const 形 = new RegExp(先['リンクの形'] ?? '.');
  // 見出しがこの形に合うものだけ拾う（ふーぽ新店速報の「【カフェ】」など）
  const 見出しの形 = 先['見出しの形'] ? new RegExp(先['見出しの形']) : null;
  // リンク先が Instagram など、出典に使えない一覧のときは、一覧ページ自体を出典にする
  const 出典固定 = 先['出典を一覧ページにする'] === true;
  const 元 = new URL(先.url);
  const 見た = new Set();
  const items = [];
  let 捨てた = 0;
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
    if (見出しの形 && !見出しの形.test(タイトル)) continue;
    見た.add(url);
    if (捨てるか(タイトル, 捨てる語)) {
      捨てた += 1;
      continue;
    }
    items.push({
      名前: 先.名前,
      区分: 先.区分 ?? '',
      タイトル: タイトル.slice(0, 120),
      url: 出典固定 ? 先.url : url,
      日付: '',
      出典固定
    });
    if (items.length >= (先.最大件数 ?? 20)) break;
  }
  return { 名前: 先.名前, items, 捨てた };
}

// ------------------------------------------------------------------ 小物

async function 取る(url) {
  let 最後のエラー;
  // 1回目で切られることがあるので、少し待って2回まで試す
  for (let 回 = 1; 回 <= 3; 回 += 1) {
    try {
      const 中止 = AbortSignal.timeout ? AbortSignal.timeout(待ち時間) : undefined;
      const res = await fetch(url, {
        headers: {
          'user-agent': ユーザーエージェント,
          accept: 'application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5',
          'accept-language': 'ja,en;q=0.8',
          'accept-encoding': 'gzip, deflate',
          connection: 'close'
        },
        signal: 中止,
        redirect: 'follow'
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.text();
    } catch (e) {
      最後のエラー = e;
      if (回 < 3) await new Promise((r) => setTimeout(r, 800 * 回));
    }
  }
  throw new Error(理由(最後のエラー));
}

/** Node の fetch は何でも「fetch failed」にするので、中の理由まで出す */
function 理由(e) {
  const 元 = e?.cause;
  const 断片 = [e?.message];
  if (元) 断片.push(元.code ?? '', 元.message ?? '');
  if (元?.cause) 断片.push(元.cause.code ?? '', 元.cause.message ?? '');
  return [...new Set(断片.filter(Boolean))].join(' / ');
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

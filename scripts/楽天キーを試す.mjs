/**
 * 楽天ウェブサービスのキーが使えるかどうかだけを確かめる。
 * ------------------------------------------------------------------
 * 何も書かない。何も投稿しない。1回だけ検索して結果を出すだけ。
 * このアカウントで商品紹介をやると決めたときに、
 * scripts/商品を取る.mjs（X・yu にあるもの）を持ってくればよい。
 * ------------------------------------------------------------------
 */
const エンドポイント = 'https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701';

const アプリID = process.env.RAKUTEN_APP_ID;
const アクセスキー = process.env.RAKUTEN_ACCESS_KEY;
const アフィリエイトID = process.env.RAKUTEN_AFFILIATE_ID;
const リファラー = (process.env.RAKUTEN_REFERER ?? '').trim();

function 止まる(文) {
  console.error(`::error::${文}`);
  process.exit(1);
}

const 足りないもの = [
  ['RAKUTEN_APP_ID', アプリID],
  ['RAKUTEN_ACCESS_KEY', アクセスキー],
  ['RAKUTEN_AFFILIATE_ID', アフィリエイトID],
  ['RAKUTEN_REFERER', リファラー]
].filter(([, v]) => !v).map(([k]) => k);

if (足りないもの.length) 止まる(`Secrets が足りません: ${足りないもの.join('、')}`);

let オリジン = '';
try {
  オリジン = new URL(リファラー).origin;
} catch {
  止まる('RAKUTEN_REFERER が URL の形になっていません。https:// から書いてください');
}

const q = new URLSearchParams({
  applicationId: アプリID,
  accessKey: アクセスキー,
  affiliateId: アフィリエイトID,
  keyword: '越前がに',
  hits: '3',
  sort: '-reviewCount',
  imageFlag: '1',
  format: 'json'
});

const res = await fetch(`${エンドポイント}?${q}`, {
  headers: { accept: 'application/json', referer: リファラー, origin: オリジン }
});

if (!res.ok) {
  止まる(`HTTP ${res.status} ${(await res.text()).replace(/\s+/g, ' ').slice(0, 300)}`);
}

const data = await res.json();
const items = (data.Items ?? []).map((w) => w.Item ?? w);
console.log(`OK。${items.length}件かえってきました。`);
for (const x of items) {
  const ついた = x.affiliateUrl ? 'アフィリエイトリンクあり' : '::warning::アフィリエイトリンクが付いていません';
  console.log(`- ${x.itemName.slice(0, 50)} ／ ${x.itemPrice}円・レビュー${x.reviewCount}件 ／ ${ついた}`);
}
if (!items.length) console.log('::warning::キーは通りましたが、商品が0件でした。');

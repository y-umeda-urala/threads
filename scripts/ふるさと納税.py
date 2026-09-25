"""その日に紹介するふるさと納税の返礼品と、切り口を決める。3アカウントで同じ結果になる。

なぜ同じ結果になるのか
----------------------
  - リストは 1 つだけ（X のリポジトリの neta/ふるさと納税.jsonl）。
    X は手元のファイルを読み、福井と yu は同じファイルを https で読む。
  - 選び方は日付から計算する。乱数も、どこまで使ったかの記録も使わない。

税の話の大前提（compose.py の決まりでも機械で止める）
----------------------------------------------------
  **「節税」と書かない。** 節税ではなく控除。税が減るわけではない。
  **控除の上限額を断定しない。** 年収と家族構成で変わる。
  **「実質2,000円」を無条件に書かない。** 総務省が明記している通り、
    上限を超えると実質の負担は2,000円を超える。
  **ワンストップ特例の条件を省かない。** 確定申告が不要な給与所得者等で、
    寄付先が5自治体以内。
  出典: https://www.soumu.go.jp/main_sosiki/jichi_zeisei/czaisei/czaisei_seido/
        furusato/mechanism/deduction.html
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

置き場 = Path("neta/ふるさと納税.jsonl")
リストのURL = (
    "https://raw.githubusercontent.com/yasu29fr/x-yu__fukui-bot/main/"
    "neta/%E3%81%B5%E3%82%8B%E3%81%95%E3%81%A8%E7%B4%8D%E7%A8%8E.jsonl"
)
起点 = date(2026, 1, 1)

# 自治体ごとの絵文字。土地の目印にする（宿と同じ考え方）。
自治体の絵文字 = {
    "福井市": "🏙", "あわら市": "♨️", "坂井市": "🏯", "永平寺町": "🛕",
    "勝山市": "🦕", "大野市": "⛰", "鯖江市": "👓", "越前市": "🖌",
    "越前町": "🌊", "南越前町": "🚃", "敦賀市": "⚓", "美浜町": "🏖",
    "若狭町": "🦆", "小浜市": "🐟", "おおい町": "🌲", "高浜町": "🏝",
    "池田町": "🌱", "今立郡": "🖌",
}
丸数字 = "①②③④⑤⑥⑦⑧⑨⑩"

# 切り口。返礼品の名前と寄付額から機械で振り分ける。
# 「探す語」が名前に入っていれば当てはまる。「金額」があれば寄付額で絞る。
切り口たち = [
    {"名": "お米", "探す語": ["コシヒカリ", "あきさかり", "華越前", "無洗米", " 米 ", "お米"],
     "問い": "福井のお米"},
    {"名": "海のもの", "探す語": ["かに", "カニ", "がに", "鯖", "さば", "マグロ", "魚", "干物", "うなぎ"],
     "問い": "福井の海のもの"},
    {"名": "甘いもの", "探す語": ["スイーツ", "ケーキ", "クッキー", "羽二重", "お菓子", "バターサンド", "デザート"],
     "問い": "福井の甘いもの"},
    {"名": "1万円まで", "金額": (0, 10000), "問い": "1万円までで選べるもの"},
    {"名": "1万円台", "金額": (10000, 20000), "問い": "1万円台で選べるもの"},
    {"名": "ちょっと奮発", "金額": (20000, 10**9), "問い": "2万円以上のもの"},
]


def _行を読む() -> list[dict]:
    if 置き場.exists():
        text = 置き場.read_text(encoding="utf-8")
    else:
        try:
            req = urllib.request.Request(リストのURL, headers={"User-Agent": "compose"})
            with urllib.request.urlopen(req, timeout=30) as res:
                text = res.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"::warning::ふるさと納税のリストを読めませんでした（{exc}）。今日は紹介しません。")
            return []
    出 = []
    for 行 in text.splitlines():
        行 = 行.strip()
        if not 行 or 行.startswith("#"):
            continue
        try:
            x = json.loads(行)
        except json.JSONDecodeError:
            continue
        if x.get("url") and x.get("名"):
            出.append(x)
    return 出


読む = _行を読む


def 当てはまる(切り口: dict, 品: dict) -> bool:
    if "金額" in 切り口:
        下, 上 = 切り口["金額"]
        額 = 品.get("寄付額")
        return bool(額) and 下 <= int(額) < 上
    名 = str(品.get("名") or "")
    return any(w in 名 for w in 切り口["探す語"])


def 見せる名(品: dict, 上限: int = 18) -> str:
    """返礼品の名前を短く整える。

    楽天のふるさと納税の商品名は検索語の羅列で、そのままでは読めない。
      「バターサンド クッキー 洋菓子 チーズ / 羽二重バターチーズサンド」
      「《 着日指定可能 》敦賀の豪華…」
    頭に付く《》【】の宣伝文句を落としてから、先頭の意味のある部分だけ残す。
    「/」は「5kg/10kg」のような数量のことが多いので、そこで切る。

    読む人に要るのは、何のカテゴリかと、自治体と、寄付額。
    商品名を正確に出すことではない。だから短く切る。
    """
    名 = str(品.get("名") or "")
    # 《…》【…】の宣伝文句は中身ごと落とす
    名 = re.sub(r"^[\s]*[《【\[][^》】\]]*[》】\]]", " ", 名)
    名 = re.sub(r"[《》【】\[\]]", " ", 名)
    名 = re.split(r"[/｜|]", 名)[0]
    名 = re.sub(r"\s+", " ", 名).strip(" 　・、")
    名 = _とじる(名)
    if len(名) <= 上限:
        return 名
    切 = 名[:上限]
    区 = max(切.rfind(" "), 切.rfind("　"))
    return _とじる((切[:区] if 区 > 8 else 切).strip(" 　・、"))


def _とじる(s: str) -> str:
    """切った拍子に開いたままになった括弧を落とす。"""
    for 開, 閉 in (("（", "）"), ("(", ")"), ("「", "」")):
        while s.count(開) > s.count(閉):
            i = s.rfind(開)
            if i < 0:
                break
            s = (s[:i] + s[i + 1:]).strip(" 　・、")
        while s.count(閉) > s.count(開):
            i = s.rfind(閉)
            if i < 0:
                break
            s = (s[:i] + s[i + 1:]).strip(" 　・、")
    return s


def 今日のまとめ(対象日: date, 品たち: list[dict] | None = None,
              いくつ: int = 7) -> dict | None:
    """その日の切り口と返礼品を返す。当てはまりが少ない切り口は飛ばす。"""
    品たち = 読む() if 品たち is None else 品たち
    if not 品たち:
        return None
    日数 = (対象日 - 起点).days
    最低 = min(4, いくつ)
    for i in range(len(切り口たち)):
        き = 切り口たち[(日数 + i) % len(切り口たち)]
        合う = [p for p in sorted(品たち, key=lambda x: str(x.get("itemCode")))
                if 当てはまる(き, p)]
        if len(合う) < 最低:
            continue
        ずらし = 日数 % len(合う)
        並べ直し = 合う[ずらし:] + 合う[:ずらし]
        # 自治体がばらけるように、まず違う自治体から1件ずつ
        選ぶ, 見た = [], set()
        for p in 並べ直し:
            if p.get("自治体") in 見た:
                continue
            選ぶ.append(p)
            見た.add(p.get("自治体"))
            if len(選ぶ) >= いくつ:
                break
        for p in 並べ直し:
            if len(選ぶ) >= いくつ:
                break
            if p not in 選ぶ:
                選ぶ.append(p)
        return {"切り口": き, "品": 選ぶ}
    return None


def 一覧の行(まとめ: dict) -> list[str]:
    """本文に並べる一覧。リンクは入れない。"""
    出 = []
    for i, p in enumerate(まとめ["品"]):
        市 = str(p.get("自治体") or "福井")
        絵 = 自治体の絵文字.get(市, "📍")
        額 = f"{int(p['寄付額']):,}円" if p.get("寄付額") else ""
        出.append(f"{丸数字[i]} {絵} {見せる名(p)}（{市}・{額}）")
    return 出


def 返信の行(まとめ: dict) -> list[str]:
    """返信に並べる、返礼品ごとの一行とリンク。1件で1つ。"""
    出 = []
    for i, p in enumerate(まとめ["品"]):
        市 = str(p.get("自治体") or "福井")
        額 = f"寄付{int(p['寄付額']):,}円" if p.get("寄付額") else ""
        件 = f"レビュー{p['レビュー数']}件" if p.get("レビュー数") else ""
        かけら = [x for x in (市, 額, 件) if x]
        出.append(f"{丸数字[i]} {見せる名(p, 40)}（{'・'.join(かけら)}）\n{p['url']}")
    return 出
